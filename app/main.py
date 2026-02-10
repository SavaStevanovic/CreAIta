"""
FastAPI application — serves the web UI, REST API, and HLS segments.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .stream_manager import STREAMS_DIR, StreamManager

logger = logging.getLogger(__name__)

# ── Lifespan ────────────────────────────────────────────────────────
manager: StreamManager | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global manager
    manager = StreamManager()
    logger.info("StreamManager initialised – %d streams restored", len(manager.list_streams()))
    yield
    logger.info("Shutting down – stopping all streams")
    manager.stop_all()


app = FastAPI(title="CreAIta – Stream Viewer", lifespan=lifespan)

# ── Serve HLS segments ─────────────────────────────────────────────
STREAMS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/streams", StaticFiles(directory=str(STREAMS_DIR)), name="hls_streams")

# ── Serve static frontend assets ───────────────────────────────────
STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ── API models ──────────────────────────────────────────────────────
class AddStreamRequest(BaseModel):
    source_url: str
    name: str | None = None  # Optional - will be derived from stream if not provided


class StreamResponse(BaseModel):
    id: str
    name: str
    source_url: str
    status: str
    playlist_url: str
    error_message: str = ""


# ── REST endpoints ──────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index():
    return (STATIC_DIR / "index.html").read_text()


@app.get("/api/streams")
async def list_streams() -> list[dict]:
    return manager.list_streams()


@app.post("/api/streams", status_code=201)
async def add_stream(req: AddStreamRequest) -> dict:
    info = await manager.add_stream_async(source_url=req.source_url, name=req.name)
    managed = manager.get_stream(info.id)
    return {
        "id": info.id,
        "name": info.name,
        "source_url": info.source_url,
        "status": info.status,
        "playlist_url": managed.playlist_url,
    }


@app.delete("/api/streams/{stream_id}")
async def remove_stream(stream_id: str):
    if not manager.remove_stream(stream_id):
        raise HTTPException(status_code=404, detail="Stream not found")
    return {"detail": "Stream removed"}
