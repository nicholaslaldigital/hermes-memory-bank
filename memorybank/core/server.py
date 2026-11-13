"""Standalone viewer: a localhost-only HTTP server for the network view.

Used by ``hermes memorybank ui`` and ``run.py ui``. The Hermes desktop app and
dashboard do not need it - they reach the same payloads through Hermes' own
authenticated plugin API.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import socket
import sys
import threading
import webbrowser
from html import escape as _escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from . import api
from .config import db_path, load_config
from .store import Store

UI_DIR = Path(__file__).resolve().parent.parent / "ui"
TOKEN_HEADER = "X-MemoryBank-Token"
_STATIC = {
    "/engine.js": ("engine.js", "application/javascript; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
}
_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'none'"
)


class _Server(ThreadingHTTPServer):
    """One viewer per port, also on Windows.

    The standard library lets a listening socket be re-bound; on Windows that
    means a second viewer started on a port another one already serves would
    bind it "successfully" and then never receive a connection - its browser
    tab would show, and edit, the *other* profile's bank. Asking for the port
    exclusively makes the second bind fail, and the next port is tried.
    """

    daemon_threads = True
    allow_reuse_address = os.name != "nt"

    def server_bind(self) -> None:
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if os.name == "nt" and exclusive is not None:
            self.socket.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        super().server_bind()


class _BadRequest(ValueError):
    """A query value that is not what the route needs (answered with 400)."""


def _int(query: Dict[str, str], name: str, default: int) -> int:
    raw = query.get(name)
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except ValueError:
        raise _BadRequest(f"'{name}' must be a whole number") from None


def _handler(store: Store, cfg: Dict[str, Any], token: str, title: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "MemoryBank/1.3"
        protocol_version = "HTTP/1.1"
        timeout = 10  # a client that stops sending frees its thread

        def log_message(self, fmt: str, *args: Any) -> None:  # keep the console quiet
            return

        # -- helpers --------------------------------------------------------

        def _send(self, status: int, body: bytes, content_type: str, extra: Optional[Dict[str, str]] = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload: Any, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self._send(status, body, "application/json; charset=utf-8")

        def _host_ok(self) -> bool:
            # Refuse requests addressed to any other name: blocks DNS rebinding.
            host = (self.headers.get("Host") or "").strip().lower()
            port = self.server.server_address[1]
            return host in (f"127.0.0.1:{port}", f"localhost:{port}")

        def _authed(self) -> bool:
            given = self.headers.get(TOKEN_HEADER) or ""
            return hmac.compare_digest(given.encode("utf-8"), token.encode("utf-8"))

        # -- routes ---------------------------------------------------------

        def do_GET(self) -> None:  # noqa: N802
            if not self._host_ok():
                return self._json({"error": "forbidden host"}, 403)
            url = urlparse(self.path)
            path = url.path
            if path in ("/", "/index.html"):
                try:
                    html = (UI_DIR / "index.html").read_text(encoding="utf-8")
                except OSError:
                    return self._json({"error": "viewer files missing"}, 500)
                # The page itself carries no key: the key travels in the link's
                # fragment, which only the person who started the viewer has.
                html = html.replace("__MB_TITLE__", _escape(title, quote=True))   # a folder name is not markup
                return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8",
                                  {"Content-Security-Policy": _CSP, "X-Frame-Options": "DENY"})
            if path in _STATIC:
                name, content_type = _STATIC[path]
                try:
                    return self._send(200, (UI_DIR / name).read_bytes(), content_type)
                except OSError:
                    return self._json({"error": "not found"}, 404)
            if not path.startswith("/api/"):
                return self._json({"error": "not found"}, 404)
            if not self._authed():
                return self._json({"error": "unauthorized"}, 401)
            query = {k: v[-1] for k, v in parse_qs(url.query).items()}
            try:
                return self._api_get(path[4:], query)
            except _BadRequest as exc:
                return self._json({"error": str(exc)}, 400)
            except Exception:  # a bad request must not kill the server thread
                return self._json({"error": "internal error"}, 500)

        def _api_get(self, path: str, query: Dict[str, str]) -> None:
            if path == "/graph":
                return self._json(api.graph(
                    store,
                    limit=_int(query, "limit", int(cfg.get("ui", {}).get("graph_limit", 4000))),
                    min_entity_degree=_int(query, "min_degree", 2),
                    include_archived=query.get("archived") in ("1", "true"),
                ))
            if path == "/stats":
                return self._json(api.stats(store))
            if path == "/events":
                return self._json(api.events(store, _int(query, "after", 0)))
            if path == "/search":
                return self._json(api.search(store, cfg, query.get("q", ""), limit=_int(query, "limit", 20)))
            if path.startswith("/node/"):
                tail = path[6:]
                if tail.isdigit() and len(tail) < 19:
                    item = api.node(store, int(tail))
                    return self._json(item, 200) if item else self._json({"error": "not found"}, 404)
            return self._json({"error": "not found"}, 404)

        def do_POST(self) -> None:  # noqa: N802
            # No route takes a body, so none is ever read; the connection is
            # closed afterwards so that unread bytes are never parsed as the
            # next request, and a client that promises a body and never sends
            # it cannot hold a thread.
            self.close_connection = True
            if not self._host_ok():
                return self._json({"error": "forbidden host"}, 403)
            if not self._authed():
                return self._json({"error": "unauthorized"}, 401)
            parts = urlparse(self.path).path.strip("/").split("/")
            if len(parts) == 4 and parts[0] == "api" and parts[1] == "node" and parts[2].isdigit() \
                    and len(parts[2]) < 19 and parts[3] in ("forget", "restore"):
                try:
                    result = api.mutate(store, parts[3], int(parts[2]))
                except Exception:
                    return self._json({"error": "internal error"}, 500)
                if not result.get("error"):
                    return self._json(result, 200)
                return self._json(result, 404 if str(result["error"]).startswith("no item") else 409)
            return self._json({"error": "not found"}, 404)

    return Handler


def make_server(hermes_home: Any, *, port: int = 8765, title: str = "") -> Tuple[ThreadingHTTPServer, Store]:
    """Bind to 127.0.0.1 on ``port`` (or the next free one) and return the
    server; its ``token`` attribute is the key the viewer page must present."""
    store = Store(db_path(hermes_home))
    cfg = load_config(hermes_home)
    token = secrets.token_urlsafe(24)
    handler = _handler(store, cfg, token, title or Path(hermes_home).name)
    last_error: Optional[OSError] = None
    for candidate in [port] + [port + i for i in range(1, 20)] + [0]:
        try:
            server = _Server(("127.0.0.1", candidate), handler)
            server.token = token  # type: ignore[attr-defined]
            return server, store
        except OSError as exc:
            last_error = exc
    store.close()
    raise last_error or OSError("could not bind a local port")


def serve(hermes_home: Any, *, port: int = 8765, open_browser: bool = True, title: str = "",
          force_open: bool = False) -> int:
    server, store = make_server(hermes_home, port=port, title=title)
    # The key rides in the fragment: it is never sent to the server, never
    # written into the page, and the page removes it from the address bar.
    url = f"http://127.0.0.1:{server.server_address[1]}/#{server.token}"  # type: ignore[attr-defined]
    print(f"Memory Bank viewer: {url}   (Ctrl+C to stop)", flush=True)
    if open_browser and sys.platform.startswith("linux") and not force_open:
        # Linux hands the link to the browser on its command line, where other users of the machine can read it
        # (and a console browser may be started on a headless terminal): there the link is printed, not opened.
        print("(not opened automatically on Linux; open the link yourself, or run with --open)", flush=True)
    elif open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        store.close()
    return 0
