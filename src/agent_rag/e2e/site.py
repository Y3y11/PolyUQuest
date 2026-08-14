"""Versioned in-process HTTP origin used by the business E2E scenario."""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import httpx

from agent_rag.e2e.fixture_content import fixture_html
from agent_rag.tools.fetch import FetchedDocument
from agent_rag.tools.schemas import FetchInput


class _SiteState:
    def __init__(self, token: str, path: str):
        self.token = token
        self.path = path
        self.version = 1
        self.requests = 0
        self._lock = threading.Lock()

    def snapshot(self) -> tuple[int, int]:
        with self._lock:
            return self.version, self.requests

    def record_request(self) -> tuple[int, int]:
        with self._lock:
            self.requests += 1
            return self.version, self.requests

    def set_version(self, version: int) -> None:
        if version not in {1, 2}:
            raise ValueError("Fixture version must be 1 or 2")
        with self._lock:
            self.version = version


class VersionedFixtureSite:
    def __init__(self, token: str, canonical_host: str, path: str):
        self.token = token
        self.canonical_host = canonical_host
        self.path = path
        self.state = _SiteState(token, path)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def canonical_url(self) -> str:
        return f"http://{self.canonical_host}{self.path}"

    @property
    def origin(self) -> str:
        if self._server is None:
            raise RuntimeError("Fixture site has not started")
        return f"http://127.0.0.1:{self._server.server_port}"

    @property
    def request_count(self) -> int:
        return self.state.snapshot()[1]

    def set_version(self, version: int) -> None:
        self.state.set_version(version)

    def start(self) -> VersionedFixtureSite:
        state = self.state

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
                parsed = urlsplit(self.path)
                if parsed.path != state.path:
                    self.send_error(404)
                    return
                version, _ = state.record_request()
                etag = f'"{state.token}-v{version}"'
                if self.headers.get("If-None-Match") == etag:
                    self.send_response(304)
                    self.send_header("ETag", etag)
                    self.end_headers()
                    return
                payload = fixture_html(state.token, version).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("ETag", etag)
                self.send_header("Last-Modified", "Fri, 14 Aug 2026 00:00:00 GMT")
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, _format: str, *_args: object) -> None:
                return None

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="business-e2e-origin",
            daemon=True,
        )
        self._thread.start()
        return self

    def close(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=3)
        self._server = None
        self._thread = None

    def __enter__(self) -> VersionedFixtureSite:
        return self.start()

    def __exit__(self, *_args: object) -> None:
        self.close()


class MappedFixtureFetcher:
    """Fetch the canonical test domain through its loopback fixture origin."""

    def __init__(self, site: VersionedFixtureSite):
        self.site = site

    async def __call__(self, tool_input: FetchInput) -> FetchedDocument:
        requested = str(tool_input.url)
        parsed = urlsplit(requested)
        if parsed.hostname != self.site.canonical_host or parsed.path != self.site.path:
            raise ValueError("E2E fetch adapter received an unknown canonical URL")
        headers: dict[str, str] = {"Accept": "text/html"}
        if tool_input.if_none_match:
            headers["If-None-Match"] = tool_input.if_none_match
        if tool_input.if_modified_since:
            headers["If-Modified-Since"] = tool_input.if_modified_since
        target = f"{self.site.origin}{parsed.path}"
        async with httpx.AsyncClient(trust_env=False, timeout=tool_input.timeout_seconds) as client:
            response = await client.get(target, headers=headers)
        if response.status_code == 304:
            return FetchedDocument(
                requested_url=requested,
                final_url=requested,
                status_code=304,
                html="",
                headers=dict(response.headers),
                not_modified=True,
            )
        response.raise_for_status()
        return FetchedDocument(
            requested_url=requested,
            final_url=requested,
            status_code=response.status_code,
            html=response.text,
            headers=dict(response.headers),
        )
