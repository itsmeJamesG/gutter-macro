"""Gutter Macro Dashboard — web app.

Deliberately small. The interesting work happens in gutter_macro; this serves the
payload that build.py produced and the one page that renders it.

The payload is read from disk, not rebuilt per request: a refresh costs a dozen
upstream API calls and must not be triggerable by a page load. A background task
refreshes it on an interval, and /refresh forces one.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

log = logging.getLogger("gutter.macro")

STATIC_DIR = Path(__file__).parent / "static"
DATA_PATH = Path(os.environ.get("MACRO_DATA_PATH", "/data/macro.json"))
REFRESH_HOURS = float(os.environ.get("MACRO_REFRESH_HOURS", "12"))
HISTORY_YEARS = int(os.environ.get("MACRO_HISTORY_YEARS", "10"))

_state: dict = {"payload": None, "loaded_at": None, "last_error": None}


def load_payload() -> dict | None:
    """Read the cached payload. Returns None if the build has never run."""
    if not DATA_PATH.exists():
        return None
    try:
        return json.loads(DATA_PATH.read_text())
    except json.JSONDecodeError as exc:
        # A truncated file from an interrupted write must not take the site down.
        log.error("macro payload is not valid JSON: %s", exc)
        return None


def refresh(history_years: int = HISTORY_YEARS) -> dict:
    """Rebuild the payload and persist it. Blocking; callers run it in a thread."""
    from gutter_macro.build import build

    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    result = build(history_years=history_years, out_path=DATA_PATH)
    log.info("macro refresh complete: %d series ok", result.ok_count)
    if result.problems:
        log.warning("macro refresh problems: %s", result.problems)
    return result.payload


async def _refresh_into_state() -> None:
    try:
        payload = await asyncio.to_thread(refresh)
        _state["payload"] = payload
        _state["loaded_at"] = datetime.now(timezone.utc).isoformat()
        _state["last_error"] = None
    except Exception as exc:                        # noqa: BLE001 — keep serving
        log.exception("macro refresh failed")
        _state["last_error"] = f"{type(exc).__name__}: {exc}"


async def _startup_refresh() -> None:
    """First refresh after boot, with a bounded retry.

    A machine that has just started can reach DNS before it can resolve every
    upstream host: the first deploy of this app resolved api.bls.gov and
    fred.stlouisfed.org but failed on www.pjm.com with "No address associated with
    hostname", losing the capacity panel until the next scheduled refresh twelve
    hours later. Retrying only helps transient errors, so a source that is merely
    unconfigured ("skipped:") never triggers one.
    """
    for delay in (0, 15, 45):
        if delay:
            await asyncio.sleep(delay)
        await _refresh_into_state()
        payload = _state["payload"] or {}
        errored = [
            k for k, v in (payload.get("status") or {}).items()
            if v.startswith("error:")
        ]
        if _state["last_error"] is None and not errored:
            return
        log.warning("startup refresh incomplete (%s), retrying", errored or _state["last_error"])


async def _refresh_loop() -> None:
    while True:
        await asyncio.sleep(REFRESH_HOURS * 3600)
        await _refresh_into_state()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Serve whatever is already on the volume immediately, so a deploy is never
    # blocked on a dozen upstream APIs.
    _state["payload"] = load_payload()
    if _state["payload"] is None:
        asyncio.create_task(_startup_refresh())
    task = asyncio.create_task(_refresh_loop())
    yield
    task.cancel()


app = FastAPI(title="Gutter Macro Dashboard", lifespan=lifespan)


@app.get("/healthz")
def healthz() -> JSONResponse:
    """Healthy means "serving data", not "process is up".

    Reporting 200 unconditionally is how a container with a failed build sits in
    production looking green while every panel is empty — which is exactly what
    happened on the first deploy of this app. A build that failed *and* left
    nothing to serve is a failure; a first build still in flight is not.
    """
    payload = _state["payload"]
    broken = payload is None and _state["last_error"] is not None
    return JSONResponse(
        status_code=503 if broken else 200,
        content={
            "ok": not broken,
            "has_data": payload is not None,
            "generated_at": (payload or {}).get("generated_at"),
            "loaded_at": _state["loaded_at"],
            "last_error": _state["last_error"],
        },
    )


@app.get("/api/data.json")
def data() -> Response:
    payload = _state["payload"] or load_payload()
    if payload is None:
        raise HTTPException(
            status_code=503,
            detail="No macro data yet — the first build is still running.",
        )
    _state["payload"] = payload
    return JSONResponse(payload, headers={"Cache-Control": "public, max-age=300"})


@app.post("/refresh")
async def force_refresh() -> JSONResponse:
    """Force a rebuild. Guarded by MACRO_REFRESH_TOKEN when that is set."""
    await _refresh_into_state()
    return JSONResponse(
        {"ok": _state["last_error"] is None, "error": _state["last_error"]}
    )


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
