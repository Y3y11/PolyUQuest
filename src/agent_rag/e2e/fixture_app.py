"""Isolated versioned HTTP fixture service for topology E2E containers."""

from __future__ import annotations

import os
import threading

from fastapi import FastAPI, Header, HTTPException, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from agent_rag.e2e.site import fixture_html


class FixtureState(BaseModel):
    token: str
    version: int
    requests: int


class _State:
    def __init__(self, token: str):
        self.token = token
        self.version = 1
        self.requests = 0
        self._lock = threading.Lock()

    def read(self) -> FixtureState:
        with self._lock:
            return FixtureState(
                token=self.token,
                version=self.version,
                requests=self.requests,
            )

    def fetch(self) -> FixtureState:
        with self._lock:
            self.requests += 1
            return FixtureState(
                token=self.token,
                version=self.version,
                requests=self.requests,
            )

    def set_version(self, version: int) -> FixtureState:
        if version not in {1, 2}:
            raise ValueError("Fixture version must be 1 or 2")
        with self._lock:
            self.version = version
            return FixtureState(
                token=self.token,
                version=self.version,
                requests=self.requests,
            )


state = _State(os.getenv("BUSINESS_E2E_TOKEN", "topology-test"))
app = FastAPI(title="PolyUQuest topology E2E fixture", version="1")


@app.get("/__control/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/__control/state", response_model=FixtureState)
def get_state() -> FixtureState:
    return state.read()


@app.post("/__control/version/{version}", response_model=FixtureState)
def set_version(version: int) -> FixtureState:
    try:
        return state.set_version(version)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/access/{token}/", response_class=HTMLResponse)
def get_fixture_page(
    token: str,
    if_none_match: str | None = Header(default=None),
) -> Response:
    snapshot = state.fetch()
    if token != snapshot.token:
        raise HTTPException(status_code=404, detail="Unknown fixture token")
    etag = f'"{snapshot.token}-v{snapshot.version}"'
    headers = {
        "ETag": etag,
        "Last-Modified": "Fri, 14 Aug 2026 00:00:00 GMT",
        "Cache-Control": "no-store",
    }
    if if_none_match == etag:
        return Response(status_code=304, headers=headers)
    return HTMLResponse(
        fixture_html(snapshot.token, snapshot.version),
        headers=headers,
    )


def start() -> None:
    import uvicorn

    uvicorn.run(
        "agent_rag.e2e.fixture_app:app",
        host="0.0.0.0",
        port=int(os.getenv("FIXTURE_PORT", "8080")),
        reload=False,
    )
