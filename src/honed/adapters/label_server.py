"""A localhost server for the human-labeling page (`honed human-labels serve`): browsers may refuse `fetch` from a
page opened as a `file://` URL, so the page, the blind sample and a little metadata are served over HTTP instead.

Only three paths answer: `/` (the page), `/sample.json` (the committed sample) and `/meta.json` (the honed version the
labels file records, and how many lines of context to show around the flagged ones). Nothing else under the project
is reachable. The page itself fetches code from raw.githubusercontent.com; this server makes no outbound request.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


@dataclass(frozen=True)
class PageFiles:
    page: Path  # tools/human-labels/index.html
    sample: Path  # yardstick/human_labels/sample.json
    meta: Mapping[str, Any]  # served as /meta.json


def _handler(files: PageFiles) -> type[BaseHTTPRequestHandler]:
    routes: dict[str, tuple[Callable[[], bytes], str]] = {
        "/": (files.page.read_bytes, "text/html; charset=utf-8"),
        "/index.html": (files.page.read_bytes, "text/html; charset=utf-8"),
        "/sample.json": (files.sample.read_bytes, "application/json"),
        "/meta.json": (lambda: json.dumps(dict(files.meta)).encode(), "application/json"),
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            route = routes.get(urlsplit(self.path).path)
            if route is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            read, content_type = route
            try:
                body = read()
            except OSError:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:  # quiet: the terminal shows only the URL
            return

    return Handler


class LabelServer:
    """The page's server on 127.0.0.1 (port 0 picks a free one); `serve_forever` until `shutdown`."""

    def __init__(self, files: PageFiles, port: int = 0) -> None:
        self._server = ThreadingHTTPServer(("127.0.0.1", port), _handler(files))

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/"

    def serve_forever(self) -> None:
        self._server.serve_forever()

    def start(self) -> threading.Thread:
        """Serve on a background thread (tests)."""
        thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        thread.start()
        return thread

    def shutdown(self) -> None:
        self._server.shutdown()
        self._server.server_close()
