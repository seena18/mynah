"""HTTP API and static host.

Deliberately boring: JSON in, JSON out, and the whole UI state comes back from
one `/api/state` call that the page polls. No websockets, no client-side store
to drift out of sync with the server's.
"""

from __future__ import annotations

import json
import shutil
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import audio, store
from .engine import ENGINE
from .jobs import RENDER

WEB = Path(__file__).resolve().parent / "web"

@asynccontextmanager
async def lifespan(_app: FastAPI):
    store.TAKES.mkdir(parents=True, exist_ok=True)
    store.VOICES.mkdir(parents=True, exist_ok=True)
    # Start loading immediately: the first generation should not also be the
    # first three minutes of model load.
    ENGINE.warm()
    yield


app = FastAPI(title="mynah", lifespan=lifespan)


# ---- state ---------------------------------------------------------------

@app.get("/api/state")
def get_state() -> dict:
    return RENDER.snapshot()


# ---- script and chunks ---------------------------------------------------

class SplitBody(BaseModel):
    text: str
    max_chars: int = store.DEFAULT_CHUNK_CHARS


@app.post("/api/script/split")
def split(body: SplitBody) -> dict:
    """Replace the chunk list from a pasted script.

    Existing takes survive this: a chunk is keyed by its fingerprint, so any
    line whose text comes back identical is still rendered.
    """
    pieces = store.split_script(body.text, body.max_chars)
    if not pieces:
        raise HTTPException(400, "nothing to split")
    with RENDER.lock:
        RENDER.project.chunks = [store.Chunk(text=text) for text in pieces]
        RENDER.project.save()
    RENDER.note(f"split into {len(pieces)} chunks")
    return RENDER.snapshot()


class ChunkBody(BaseModel):
    text: str | None = None
    pause_after: float | None = None


@app.put("/api/chunks/{chunk_id}")
def edit_chunk(chunk_id: str, body: ChunkBody) -> dict:
    with RENDER.lock:
        chunk = RENDER.project.find(chunk_id)
        if chunk is None:
            raise HTTPException(404, "no such chunk")
        if body.text is not None:
            chunk.text = body.text
        if body.pause_after is not None:
            chunk.pause_after = max(0.0, min(10.0, body.pause_after))
        RENDER.errors.pop(chunk_id, None)
        RENDER.project.save()
    return RENDER.snapshot()


@app.post("/api/chunks")
def add_chunk(body: ChunkBody) -> dict:
    with RENDER.lock:
        RENDER.project.chunks.append(store.Chunk(text=body.text or ""))
        RENDER.project.save()
    return RENDER.snapshot()


@app.delete("/api/chunks/{chunk_id}")
def delete_chunk(chunk_id: str) -> dict:
    with RENDER.lock:
        RENDER.project.chunks = [c for c in RENDER.project.chunks if c.id != chunk_id]
        RENDER.project.save()
    return RENDER.snapshot()


@app.post("/api/chunks/{chunk_id}/generate")
def generate_chunk(chunk_id: str) -> dict:
    if not RENDER.submit([chunk_id]):
        raise HTTPException(400, "chunk is unknown or already queued")
    return RENDER.snapshot()


@app.post("/api/generate")
def generate_all() -> dict:
    with RENDER.lock:
        stale = [c.id for c in RENDER.project.chunks
                 if RENDER.project.status(c) == "stale"]
    queued = RENDER.submit(stale)
    RENDER.note(f"queued {queued} chunk(s)")
    return RENDER.snapshot()


@app.post("/api/queue/clear")
def clear_queue() -> dict:
    RENDER.note(f"cleared {RENDER.clear()} queued chunk(s)")
    return RENDER.snapshot()


@app.get("/api/takes/{chunk_id}.wav")
def take(chunk_id: str):
    with RENDER.lock:
        chunk = RENDER.project.find(chunk_id)
        path = RENDER.project.take_path(chunk) if chunk else None
    if path is None or not path.exists():
        raise HTTPException(404, "no take for this chunk yet")
    # The filename is a content hash, so a take never changes under a URL.
    return FileResponse(path, media_type="audio/wav",
                        headers={"Cache-Control": "public, max-age=31536000, immutable"})


# ---- project settings ----------------------------------------------------

class ProjectBody(BaseModel):
    title: str | None = None
    voice_id: str | None = None
    params: dict | None = None


@app.post("/api/project")
def update_project(body: ProjectBody) -> dict:
    with RENDER.lock:
        if body.title is not None:
            RENDER.project.title = body.title
        if body.voice_id is not None:
            RENDER.project.voice_id = body.voice_id
        if body.params is not None:
            RENDER.project.params.update(body.params)
        RENDER.project.save()
    return RENDER.snapshot()


# ---- voices --------------------------------------------------------------

def _compile_voice(voice_id: str, source: Path, name: str) -> None:
    directory = store.voice_dir(voice_id)
    meta_path = directory / "meta.json"

    def write_meta(**extra) -> None:
        meta_path.write_text(json.dumps({
            "id": voice_id, "name": name,
            "created": time.strftime("%Y-%m-%dT%H:%M:%S"), **extra,
        }, indent=2) + "\n")

    try:
        reference = audio.to_wav(source, directory / "reference.wav")
        seconds = audio.duration(reference)
        if seconds < audio.MIN_REFERENCE_SECONDS:
            raise RuntimeError(
                f"reference is {seconds:.1f}s; the model needs more than "
                f"{audio.MIN_REFERENCE_SECONDS:.0f}s of speech"
            )
        write_meta(seconds=round(seconds, 1), status="compiling")
        if ENGINE.state != "ready":
            RENDER.note("waiting for the model before compiling voice")
            ENGINE.wait_ready()
        RENDER.note(f"compiling voice {name!r} ({seconds:.1f}s reference)")
        ENGINE.compile_voice(reference, directory / "voice.pt")
        write_meta(seconds=round(seconds, 1), status="ready")
        RENDER.note(f"voice {name!r} ready")
    except Exception as error:  # noqa: BLE001 - surfaced in the UI
        write_meta(status="error", error=f"{type(error).__name__}: {error}")
        RENDER.note(f"voice {name!r} failed: {error}")
    finally:
        source.unlink(missing_ok=True)


@app.post("/api/voices")
async def add_voice(file: UploadFile, name: str = "") -> dict:
    """Accept an upload or a browser recording and compile it in the background.

    Compilation needs the model, which may still be loading on a cold start, so
    this returns immediately and the UI watches for the voice to turn ready.
    """
    voice_id = store.new_id()
    directory = store.voice_dir(voice_id)
    directory.mkdir(parents=True, exist_ok=True)
    suffix = Path(file.filename or "clip.webm").suffix or ".webm"
    source = directory / f"upload{suffix}"
    with source.open("wb") as handle:
        shutil.copyfileobj(file.file, handle)
    label = name.strip() or Path(file.filename or "voice").stem or "voice"
    threading.Thread(target=_compile_voice, args=(voice_id, source, label),
                     daemon=True).start()
    return {"id": voice_id, "name": label}


@app.delete("/api/voices/{voice_id}")
def delete_voice(voice_id: str) -> dict:
    directory = store.voice_dir(voice_id)
    ENGINE.forget_voice(directory / "voice.pt")
    shutil.rmtree(directory, ignore_errors=True)
    with RENDER.lock:
        if RENDER.project.voice_id == voice_id:
            RENDER.project.voice_id = ""
            RENDER.project.save()
    return RENDER.snapshot()


@app.get("/api/voices/{voice_id}/reference.wav")
def voice_reference(voice_id: str):
    path = store.voice_dir(voice_id) / "reference.wav"
    if not path.exists():
        raise HTTPException(404, "no reference audio")
    return FileResponse(path, media_type="audio/wav")


# ---- export --------------------------------------------------------------

@app.get("/api/export.wav")
def export():
    with RENDER.lock:
        pieces, missing = [], 0
        for chunk in RENDER.project.chunks:
            if RENDER.project.status(chunk) == "ready":
                pieces.append((RENDER.project.take_path(chunk), chunk.pause_after))
            elif chunk.text.strip():
                missing += 1
        title = RENDER.project.title
    if missing:
        raise HTTPException(409, f"{missing} chunk(s) still need generating")
    target = store.DATA / "export.wav"
    audio.stitch(pieces, target, ENGINE.sample_rate)
    safe = "".join(c if c.isalnum() or c in "-_ " else "_" for c in title).strip() or "mynah"
    return FileResponse(target, media_type="audio/wav", filename=f"{safe}.wav")


@app.post("/api/tidy")
def tidy() -> dict:
    """Delete takes no chunk points at any more."""
    with RENDER.lock:
        orphans = store.unused_takes(RENDER.project)
    freed = sum(p.stat().st_size for p in orphans)
    for path in orphans:
        path.unlink(missing_ok=True)
    RENDER.note(f"removed {len(orphans)} unused take(s), {freed / 1e6:.1f} MB")
    return RENDER.snapshot()


@app.exception_handler(RuntimeError)
def _runtime_error(_request, error: RuntimeError):
    return JSONResponse({"detail": str(error)}, status_code=400)


app.mount("/", StaticFiles(directory=WEB, html=True), name="web")
