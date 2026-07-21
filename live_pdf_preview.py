#!/usr/bin/env python3
"""Local PDF preview with browser auto-refresh.

This avoids Chrome/Safari PDF caching by serving a small HTML wrapper that
reloads the embedded PDF with a cache-busting URL whenever the file changes.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import socket
import subprocess
import threading
import time
import urllib.parse
import webbrowser
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer


DEFAULT_PDF = "root.pdf"
DEFAULT_TEX = "root.tex"
POLL_SECONDS = 1.0
WATCH_SUFFIXES = {".tex", ".bib", ".cls", ".sty", ".bst", ".png", ".jpg", ".jpeg", ".pdf"}


VIEWER_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Live PDF Preview</title>
  <style>
    :root {
      color-scheme: light dark;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --text: #15171a;
      --muted: #626a73;
      --border: #d8dde4;
      --accent: #0f766e;
      --error: #b42318;
      --shadow: 0 1px 2px rgba(15, 23, 42, .12);
    }

    @media (prefers-color-scheme: dark) {
      :root {
        --bg: #101214;
        --panel: #191c20;
        --text: #f1f4f8;
        --muted: #a7b0bb;
        --border: #333941;
        --accent: #2dd4bf;
        --error: #ff8a80;
        --shadow: none;
      }
    }

    * {
      box-sizing: border-box;
    }

    html,
    body {
      height: 100%;
      margin: 0;
    }

    body {
      display: grid;
      grid-template-rows: auto 1fr;
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      overflow: hidden;
    }

    .toolbar {
      display: flex;
      align-items: center;
      gap: 10px;
      min-height: 48px;
      padding: 8px 12px;
      background: var(--panel);
      border-bottom: 1px solid var(--border);
      box-shadow: var(--shadow);
      white-space: nowrap;
    }

    .title {
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      font-size: 14px;
      font-weight: 650;
    }

    .status,
    .page-state {
      display: flex;
      align-items: center;
      gap: 7px;
      min-width: 0;
      color: var(--muted);
      font-size: 13px;
    }

    .dot {
      width: 8px;
      height: 8px;
      flex: 0 0 auto;
      border-radius: 999px;
      background: var(--accent);
    }

    .dot.error {
      background: var(--error);
    }

    .spacer {
      flex: 1 1 auto;
    }

    .page-state {
      flex: 0 0 auto;
    }

    button,
    a.button {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 32px;
      padding: 0 10px;
      border: 1px solid var(--border);
      border-radius: 6px;
      background: var(--panel);
      color: var(--text);
      font: inherit;
      font-size: 13px;
      text-decoration: none;
      cursor: pointer;
    }

    button:hover,
    a.button:hover {
      border-color: var(--accent);
    }

    main {
      width: 100%;
      height: 100%;
      overflow: auto;
      overscroll-behavior: contain;
    }

    .pages {
      display: grid;
      gap: 18px;
      justify-items: center;
      padding: 18px 16px 36px;
    }

    .page {
      position: relative;
      max-width: 100%;
      background: white;
      border: 1px solid var(--border);
      box-shadow: 0 2px 8px rgba(15, 23, 42, .14);
    }

    .page canvas {
      display: block;
      max-width: 100%;
      background: white;
    }

    .page-label {
      position: absolute;
      top: 8px;
      left: 8px;
      padding: 3px 6px;
      border-radius: 5px;
      background: rgba(17, 24, 39, .76);
      color: white;
      font-size: 11px;
      line-height: 1;
      pointer-events: none;
      opacity: 0;
      transition: opacity .15s ease;
    }

    .page:hover .page-label {
      opacity: 1;
    }

    .empty {
      padding: 48px 18px;
      color: var(--muted);
      font-size: 14px;
      text-align: center;
    }
  </style>
</head>
<body>
  <header class="toolbar">
    <div class="title" id="title"></div>
    <div class="status" id="status">
      <span class="dot" id="dot"></span>
      <span id="statusText">Waiting for PDF</span>
    </div>
    <div class="page-state" id="pageState"></div>
    <div class="spacer"></div>
    <button type="button" id="reload">Update</button>
    <a class="button" id="openPdf" target="_blank" rel="noreferrer">Open PDF</a>
  </header>
  <main id="viewer" aria-label="PDF preview">
    <div class="pages" id="pages">
      <div class="empty">Waiting for PDF</div>
    </div>
  </main>

  <script type="module">
    import * as pdfjsLib from "/viewer_assets/pdf.mjs";

    pdfjsLib.GlobalWorkerOptions.workerSrc = "/viewer_assets/pdf.worker.mjs";

    const pdfName = __PDF_NAME__;
    const title = document.getElementById("title");
    const viewer = document.getElementById("viewer");
    const pages = document.getElementById("pages");
    const dot = document.getElementById("dot");
    const statusText = document.getElementById("statusText");
    const pageState = document.getElementById("pageState");
    const reloadButton = document.getElementById("reload");
    const openPdf = document.getElementById("openPdf");
    let currentToken = "";
    let lastState = null;
    let lastBuildStamp = 0;
    let renderGeneration = 0;
    let rendering = false;
    let queuedState = null;
    let currentPageCount = 0;

    title.textContent = pdfName;

    function pdfUrl(token) {
      const liveToken = token || "current";
      return `/${encodeURIComponent(pdfName)}?live=${encodeURIComponent(liveToken)}`;
    }

    function setStatus(text, isError = false) {
      statusText.textContent = text;
      dot.classList.toggle("error", isError);
    }

    function getPageElements() {
      return Array.from(pages.querySelectorAll(".page"));
    }

    function captureAnchor() {
      const pageElements = getPageElements();
      if (!pageElements.length) return { page: 1, ratio: 0 };

      const anchorY = viewer.scrollTop + viewer.clientHeight * 0.35;
      for (const pageElement of pageElements) {
        const top = pageElement.offsetTop;
        const height = Math.max(1, pageElement.offsetHeight);
        if (anchorY >= top && anchorY <= top + height) {
          return {
            page: Number(pageElement.dataset.page || "1"),
            ratio: Math.min(1, Math.max(0, (anchorY - top) / height)),
          };
        }
      }

      const lastPage = pageElements[pageElements.length - 1];
      if (anchorY > lastPage.offsetTop) {
        return { page: Number(lastPage.dataset.page || "1"), ratio: 1 };
      }
      return { page: 1, ratio: 0 };
    }

    function restoreAnchor(anchor) {
      const targetPage = Math.min(Math.max(1, anchor.page), currentPageCount || anchor.page);
      const pageElement = pages.querySelector(`.page[data-page="${targetPage}"]`);
      if (!pageElement) return;

      const anchorY = pageElement.offsetTop + pageElement.offsetHeight * anchor.ratio;
      viewer.scrollTop = Math.max(0, anchorY - viewer.clientHeight * 0.35);
      updatePageState();
    }

    function updatePageState() {
      const pageElements = getPageElements();
      if (!pageElements.length) {
        pageState.textContent = "";
        return;
      }

      const marker = viewer.scrollTop + viewer.clientHeight * 0.5;
      let current = 1;
      for (const pageElement of pageElements) {
        if (marker >= pageElement.offsetTop - 1) {
          current = Number(pageElement.dataset.page || "1");
        }
      }
      pageState.textContent = `Page ${current} / ${currentPageCount || pageElements.length}`;
    }

    function ensurePageShells(count) {
      pages.querySelector(".empty")?.remove();
      while (pages.children.length > count) {
        pages.lastElementChild?.remove();
      }

      for (let index = pages.children.length + 1; index <= count; index += 1) {
        const pageElement = document.createElement("section");
        pageElement.className = "page";
        pageElement.dataset.page = String(index);

        const canvas = document.createElement("canvas");
        const label = document.createElement("div");
        label.className = "page-label";
        label.textContent = String(index);

        pageElement.append(canvas, label);
        pages.append(pageElement);
      }
    }

    function fitScale(page) {
      const viewport = page.getViewport({ scale: 1 });
      const availableWidth = Math.max(320, viewer.clientWidth - 46);
      return Math.min(2.2, availableWidth / viewport.width);
    }

    async function renderPdf(state, force = false) {
      if (!force && state.token === currentToken) return;

      const generation = ++renderGeneration;
      const anchor = captureAnchor();
      setStatus("Updating PDF...");

      try {
        const task = pdfjsLib.getDocument({ url: pdfUrl(state.token) });
        const pdf = await task.promise;
        if (generation !== renderGeneration) return;

        currentPageCount = pdf.numPages;
        ensurePageShells(pdf.numPages);

        const prepared = [];
        const outputScale = Math.max(1, window.devicePixelRatio || 1);
        for (let pageNumber = 1; pageNumber <= pdf.numPages; pageNumber += 1) {
          const page = await pdf.getPage(pageNumber);
          if (generation !== renderGeneration) return;

          const scale = fitScale(page);
          const viewport = page.getViewport({ scale });
          const pageElement = pages.querySelector(`.page[data-page="${pageNumber}"]`);
          const canvas = pageElement.querySelector("canvas");

          canvas.width = Math.floor(viewport.width * outputScale);
          canvas.height = Math.floor(viewport.height * outputScale);
          canvas.style.width = `${Math.floor(viewport.width)}px`;
          canvas.style.height = `${Math.floor(viewport.height)}px`;
          pageElement.style.width = canvas.style.width;
          pageElement.style.height = canvas.style.height;
          prepared.push({ page, viewport, canvas, outputScale });
        }

        restoreAnchor(anchor);

        for (const item of prepared) {
          if (generation !== renderGeneration) return;
          const context = item.canvas.getContext("2d");
          context.setTransform(item.outputScale, 0, 0, item.outputScale, 0, 0);
          await item.page.render({ canvasContext: context, viewport: item.viewport }).promise;
        }

        if (generation !== renderGeneration) return;
        currentToken = state.token;
        lastState = state;
        openPdf.href = `${pdfUrl(state.token)}#page=${captureAnchor().page}`;
        setStatus(`Updated ${new Date(state.mtimeMs).toLocaleTimeString()}`);
        updatePageState();
      } catch (error) {
        setStatus("PDF render failed; retrying", true);
      }
    }

    async function queueRender(state, force = false) {
      if (rendering) {
        queuedState = state;
        return;
      }

      rendering = true;
      try {
        let nextState = state;
        let nextForce = force;
        while (nextState) {
          queuedState = null;
          await renderPdf(nextState, nextForce);
          nextState = queuedState;
          nextForce = false;
        }
      } finally {
        rendering = false;
      }
    }

    async function refreshPdf(force = false) {
      try {
        const response = await fetch(`/__pdf_state?at=${Date.now()}`, { cache: "no-store" });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const state = await response.json();

        if (!state.exists) {
          setStatus("PDF not found", true);
          return;
        }

        if (force || state.token !== currentToken) {
          queueRender(state, force);
        }
      } catch (error) {
        setStatus("Preview connection lost", true);
      }
    }

    async function refreshBuildState() {
      try {
        const response = await fetch(`/__build_state?at=${Date.now()}`, { cache: "no-store" });
        if (!response.ok) return;
        const state = await response.json();
        if (!state.enabled || !state.updatedAt || state.updatedAt === lastBuildStamp) return;
        lastBuildStamp = state.updatedAt;

        if (state.status === "running") {
          setStatus("Building...");
        } else if (state.status === "failed") {
          setStatus("Build failed; showing last PDF", true);
        } else if (state.status === "ok") {
          setStatus("Build complete");
        }
      } catch (_) {
        // PDF polling reports connection errors; keep this endpoint quiet.
      }
    }

    reloadButton.addEventListener("click", () => refreshPdf(true));
    viewer.addEventListener("scroll", updatePageState, { passive: true });
    window.addEventListener("resize", () => {
      clearTimeout(window.__livePdfResizeTimer);
      window.__livePdfResizeTimer = setTimeout(() => {
        if (lastState) queueRender(lastState, true);
      }, 150);
    });
    setInterval(refreshPdf, 1000);
    setInterval(refreshBuildState, 1000);
    refreshPdf(true);
    refreshBuildState();
  </script>
</body>
</html>
"""


class BuildState:
    def __init__(self, enabled: bool) -> None:
        self._lock = threading.Lock()
        self._state = {
            "enabled": enabled,
            "status": "idle",
            "message": "",
            "updatedAt": 0,
            "returnCode": None,
        }

    def update(self, **changes: object) -> None:
        with self._lock:
            self._state.update(changes)
            self._state["updatedAt"] = time.time()

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return dict(self._state)


def pdf_state(pdf_path: pathlib.Path) -> dict[str, object]:
    try:
        stat = pdf_path.stat()
    except FileNotFoundError:
        return {
            "exists": False,
            "name": pdf_path.name,
            "token": "missing",
            "mtimeMs": 0,
            "size": 0,
        }

    return {
        "exists": True,
        "name": pdf_path.name,
        "token": f"{stat.st_mtime_ns}-{stat.st_size}",
        "mtimeMs": stat.st_mtime_ns // 1_000_000,
        "size": stat.st_size,
    }


def make_handler(directory: pathlib.Path, pdf_path: pathlib.Path, build_state: BuildState):
    pdf_name_json = json.dumps(pdf_path.name)

    class LivePdfHandler(SimpleHTTPRequestHandler):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, directory=str(directory), **kwargs)

        def end_headers(self) -> None:
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            super().end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return

        def do_GET(self) -> None:
            path = urllib.parse.urlparse(self.path).path
            if path in {"/", "/index.html"}:
                self._send_viewer()
            elif path == "/__pdf_state":
                self._send_json(pdf_state(pdf_path))
            elif path == "/__build_state":
                self._send_json(build_state.snapshot())
            else:
                super().do_GET()

        def _send_viewer(self) -> None:
            body = VIEWER_HTML.replace("__PDF_NAME__", pdf_name_json).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, payload: dict[str, object]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return LivePdfHandler


def find_free_port(host: str, preferred_port: int) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, preferred_port))
            return preferred_port
        except OSError:
            sock.bind((host, 0))
            return int(sock.getsockname()[1])


def source_fingerprint(directory: pathlib.Path, pdf_path: pathlib.Path) -> tuple[tuple[str, int, int], ...]:
    records: list[tuple[str, int, int]] = []
    for path in directory.rglob("*"):
        if not path.is_file():
            continue
        if path == pdf_path:
            continue
        if path.suffix.lower() not in WATCH_SUFFIXES:
            continue
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        records.append((str(path.relative_to(directory)), stat.st_mtime_ns, stat.st_size))
    return tuple(sorted(records))


def run_latexmk(directory: pathlib.Path, tex_file: str, build_state: BuildState) -> None:
    build_state.update(status="running", message="latexmk running", returnCode=None)
    command = ["latexmk", "-pdf", "-interaction=nonstopmode", "-synctex=1", tex_file]
    try:
        completed = subprocess.run(
            command,
            cwd=directory,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        build_state.update(
            status="failed",
            message="latexmk was not found on PATH",
            returnCode=127,
        )
        return

    output_tail = "\n".join(completed.stdout.splitlines()[-30:])
    build_state.update(
        status="ok" if completed.returncode == 0 else "failed",
        message=output_tail,
        returnCode=completed.returncode,
    )


def build_watcher(directory: pathlib.Path, tex_file: str, pdf_path: pathlib.Path, build_state: BuildState) -> None:
    previous = source_fingerprint(directory, pdf_path)
    run_latexmk(directory, tex_file, build_state)
    previous = source_fingerprint(directory, pdf_path)

    while True:
        time.sleep(POLL_SECONDS)
        current = source_fingerprint(directory, pdf_path)
        if current == previous:
            continue

        time.sleep(0.25)
        current = source_fingerprint(directory, pdf_path)
        previous = current
        run_latexmk(directory, tex_file, build_state)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve a live-refreshing browser preview for a PDF.")
    parser.add_argument("--pdf", default=DEFAULT_PDF, help="PDF file to preview.")
    parser.add_argument("--tex", default=DEFAULT_TEX, help="TeX file to build when --watch-build is set.")
    parser.add_argument("--host", default="127.0.0.1", help="Host interface to bind.")
    parser.add_argument("--port", type=int, default=8000, help="Preferred port; another is used if busy.")
    parser.add_argument("--watch-build", action="store_true", help="Run latexmk at startup and after source changes.")
    parser.add_argument("--no-open", action="store_true", help="Do not open the browser automatically.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    directory = pathlib.Path(__file__).resolve().parent
    pdf_path = (directory / args.pdf).resolve()
    tex_path = directory / args.tex

    if args.watch_build and not tex_path.exists():
        raise SystemExit(f"Cannot watch-build because {tex_path.name} does not exist.")

    build_state = BuildState(enabled=args.watch_build)
    if args.watch_build:
        thread = threading.Thread(
            target=build_watcher,
            args=(directory, tex_path.name, pdf_path, build_state),
            daemon=True,
        )
        thread.start()

    port = find_free_port(args.host, args.port)
    handler = make_handler(directory, pdf_path, build_state)
    server = ThreadingHTTPServer((args.host, port), handler)
    url = f"http://{args.host}:{port}/"
    print(f"Live PDF preview: {url}", flush=True)
    print(f"Serving {pdf_path.name} from {directory}", flush=True)
    if args.watch_build:
        print(f"Watching sources and building {tex_path.name} with latexmk", flush=True)
    if not args.no_open:
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
