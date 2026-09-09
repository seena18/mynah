"""HTTP API and static host.

Deliberately boring: JSON in, JSON out, and the whole UI state for one project
comes back from a single `/api/state?p=<id>` call that the page polls. No
websockets, no client-side store to drift out of sync with the server's.

Project-scoped routes live under `/api/projects/{pid}/…`. Voices, takes and the
render queue are global: a voice is not a property of a script, and a take is
named by content, not by who asked for it.
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
    store.PROJECTS.mkdir(parents=True, exist_ok=True)
    migrated = store.migrate_legacy()
    if migrated:
        RENDER.adopt(migrated)
        RENDER.note(f"migrated the existing project as {migrated.title!r}")
    # Start loading immediately: the first generation should not also be the
    # first three minutes of model load.
    ENGINE.warm()
    yield


app = FastAPI(title="mynah", lifespan=lifespan)


def _project(project_id: str) -> store.Project:
    """Resolve a project id from a route, as a 404 rather than a KeyError."""
    try:
        return RENDER.get(project_id)
    except KeyError:
        raise HTTPException(404, "no such project") from None


# ---- state and projects --------------------------------------------------

@app.get("/api/state")
def get_state(p: str = "") -> dict:
    """One project's full view. Without `p`, the most recently touched one."""
    if p:
        _project(p)
    else:
        p = RENDER.pick_default()
    return RENDER.snapshot(p)


class NewProjectBody(BaseModel):
    title: str = "Untitled"


@app.post("/api/projects")
def create_project(body: NewProjectBody) -> dict:
    project = RENDER.adopt(store.create_project(body.title))
    RENDER.note(f"new project {project.title!r}")
    return RENDER.snapshot(project.id)


class ProjectBody(BaseModel):
    title: str | None = None
    voice_id: str | None = None
    params: dict | None = None


@app.post("/api/projects/{pid}")
def update_project(pid: str, body: ProjectBody) -> dict:
    with RENDER.lock:
        project = _project(pid)
        if body.title is not None:
            project.title = body.title.strip() or "Untitled"
        if body.voice_id is not None:
            if body.voice_id and not store.voice_dir(body.voice_id).is_dir():
                raise HTTPException(404, "no such voice")
            project.voice_id = body.voice_id
        if body.params is not None:
            project.params.update(body.params)
        project.save()
    return RENDER.snapshot(pid)


@app.delete("/api/projects/{pid}")
def delete_project(pid: str) -> dict:
    """Remove a project; answer with whichever project should be shown next.

    Its takes stay — another project may share them — until `tidy`.
    """
    with RENDER.lock:
        project = _project(pid)
        title = project.title
        RENDER.forget(pid)
        store.delete_project(pid)
    RENDER.note(f"deleted project {title!r}")
    return RENDER.snapshot(RENDER.pick_default())


# ---- script and chunks ---------------------------------------------------

class SplitBody(BaseModel):
    text: str
    max_chars: int = store.DEFAULT_CHUNK_CHARS


@app.post("/api/projects/{pid}/split")
def split(pid: str, body: SplitBody) -> dict:
    """Replace the chunk list from a pasted script.

    Existing takes survive this: a chunk is keyed by its fingerprint, so any
    line whose text comes back identical is still rendered.
    """
    pieces = store.split_script(body.text, body.max_chars)
    if not pieces:
        raise HTTPException(400, "nothing to split")
    with RENDER.lock:
        project = _project(pid)
        project.chunks = [store.Chunk(text=text) for text in pieces]
        project.save()
    RENDER.note(f"split into {len(pieces)} chunks")
    return RENDER.snapshot(pid)


class ChunkBody(BaseModel):
    text: str | None = None
    pause_after: float | None = None


@app.post("/api/projects/{pid}/chunks")
def add_chunk(pid: str, body: ChunkBody) -> dict:
    with RENDER.lock:
        project = _project(pid)
        project.chunks.append(store.Chunk(text=body.text or ""))
        project.save()
    return RENDER.snapshot(pid)


class ChunkOrderBody(BaseModel):
    ids: list[str]


@app.put("/api/projects/{pid}/chunks/order")
def reorder_chunks(pid: str, body: ChunkOrderBody) -> dict:
    with RENDER.lock:
        project = _project(pid)
        try:
            project.reorder(body.ids)
        except ValueError as error:
            raise HTTPException(409, str(error)) from None
        project.save()
    return RENDER.snapshot(pid)


@app.put("/api/projects/{pid}/chunks/{chunk_id}")
def edit_chunk(pid: str, chunk_id: str, body: ChunkBody) -> dict:
    with RENDER.lock:
        project = _project(pid)
        chunk = project.find(chunk_id)
        if chunk is None:
            raise HTTPException(404, "no such chunk")
        if body.text is not None:
            chunk.text = body.text
        if body.pause_after is not None:
            chunk.pause_after = max(0.0, min(10.0, body.pause_after))
        RENDER.errors.pop(chunk_id, None)
        project.save()
    return RENDER.snapshot(pid)


@app.delete("/api/projects/{pid}/chunks/{chunk_id}")
def delete_chunk(pid: str, chunk_id: str) -> dict:
    with RENDER.lock:
        project = _project(pid)
        project.chunks = [c for c in project.chunks if c.id != chunk_id]
        project.save()
    return RENDER.snapshot(pid)


@app.post("/api/projects/{pid}/chunks/{chunk_id}/generate")
def generate_chunk(pid: str, chunk_id: str) -> dict:
    _project(pid)
    if not RENDER.submit(pid, [chunk_id]):
        raise HTTPException(400, "chunk is unknown or already queued")
    return RENDER.snapshot(pid)


@app.post("/api/projects/{pid}/generate")
def generate_all(pid: str) -> dict:
    with RENDER.lock:
        project = _project(pid)
        stale = [c.id for c in project.chunks if project.status(c) == "stale"]
    queued = RENDER.submit(pid, stale)
    RENDER.note(f"queued {queued} chunk(s)")
    return RENDER.snapshot(pid)


@app.get("/api/projects/{pid}/takes/{chunk_id}.wav")
def take(pid: str, chunk_id: str):
    with RENDER.lock:
        project = _project(pid)
        chunk = project.find(chunk_id)
        path = project.take_path(chunk) if chunk else None
    if path is None or not path.exists():
        raise HTTPException(404, "no take for this chunk yet")
    # A re-roll replaces the take without changing text, voice or parameters.
    return FileResponse(path, media_type="audio/wav",
                        headers={"Cache-Control": "no-store"})


def _gather_pieces(project: store.Project) -> list[tuple[Path, float]]:
    """Ready takes for a project's lines, in order, with their trailing pause.

    Raises 409 if any non-empty line still needs generating — shared by every
    route that produces a stitched mix, so export, preview and the timeline
    can never disagree about whether the project is ready to render.
    """
    pieces, missing = [], 0
    for chunk in project.chunks:
        if project.status(chunk) == "ready":
            pieces.append((project.take_path(chunk), chunk.pause_after))
        elif chunk.text.strip():
            missing += 1
    if missing:
        raise HTTPException(409, f"{missing} chunk(s) still need generating")
    return pieces


@app.get("/api/projects/{pid}/export.wav")
def export(pid: str):
    with RENDER.lock:
        project = _project(pid)
        pieces = _gather_pieces(project)
        title, target = project.title, project.directory / "export.wav"
    audio.stitch(pieces, target, ENGINE.sample_rate)
    safe = "".join(c if c.isalnum() or c in "-_ " else "_" for c in title).strip() or "mynah"
    return FileResponse(target, media_type="audio/wav", filename=f"{safe}.wav")


@app.get("/api/projects/{pid}/preview.wav")
def preview(pid: str):
    """The same stitched mix as export, for <audio src=...> rather than a
    download — no filename means no Content-Disposition, and the response is
    never cached, so a line regenerated after the last preview is heard."""
    with RENDER.lock:
        project = _project(pid)
        pieces = _gather_pieces(project)
        target = project.directory / "export.wav"
    audio.stitch(pieces, target, ENGINE.sample_rate)
    response = FileResponse(target, media_type="audio/wav")
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/projects/{pid}/timeline")
def timeline(pid: str) -> dict:
    """Where each line lands in the stitched mix, so the page can highlight
    the one currently playing. Stitches independently of `preview.wav` —
    a second pass over a handful of short WAV files is cheap, and it keeps
    this endpoint correct on its own rather than depending on call order."""
    with RENDER.lock:
        project = _project(pid)
        pieces = _gather_pieces(project)
        ready_ids = [c.id for c in project.chunks if project.status(c) == "ready"]
        target = project.directory / "export.wav"
    segments = audio.stitch(pieces, target, ENGINE.sample_rate)
    duration = segments[-1]["end"] if segments else 0.0
    return {
        "duration": duration,
        "segments": [{"id": cid, **seg} for cid, seg in zip(ready_ids, segments)],
    }


# ---- global: queue, tidy ---------------------------------------------------

@app.post("/api/queue/clear")
def clear_queue(p: str = "") -> dict:
    dropped, stopping = RENDER.clear()
    RENDER.note(("stopping the current line; " if stopping else "") + f"cleared {dropped} queued")
    return RENDER.snapshot(p or RENDER.pick_default())


@app.post("/api/tidy")
def tidy(p: str = "") -> dict:
    """Delete takes no chunk in any project points at any more."""
    with RENDER.lock:
        orphans = store.unused_takes()
    freed = sum(path.stat().st_size for path in orphans)
    for path in orphans:
        path.unlink(missing_ok=True)
    RENDER.note(f"removed {len(orphans)} unused take(s), {freed / 1e6:.1f} MB")
    return RENDER.snapshot(p or RENDER.pick_default())


# ---- voices --------------------------------------------------------------

def _compile_voice(voice_id: str, source: Path, name: str,
                   project_id: str, previous: str) -> None:
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
        # The upload selected this voice; a project must not stay pointed at
        # one that never came to exist.
        with RENDER.lock:
            try:
                project = RENDER.get(project_id)
            except KeyError:
                project = None
            if project is not None and project.voice_id == voice_id:
                project.voice_id = previous
                project.save()
    finally:
        source.unlink(missing_ok=True)


@app.post("/api/voices")
async def add_voice(file: UploadFile, name: str = "", p: str = "") -> dict:
    """Accept an upload or a browser recording and compile it in the background.

    Compilation needs the model, which may still be loading on a cold start, so
    this returns immediately and the UI watches for the voice to turn ready.
    The voice is selected into project `p` straight away.
    """
    project_id = p or RENDER.pick_default()
    voice_id = store.new_id()
    directory = store.voice_dir(voice_id)
    directory.mkdir(parents=True, exist_ok=True)
    suffix = Path(file.filename or "clip.webm").suffix or ".webm"
    source = directory / f"upload{suffix}"
    with source.open("wb") as handle:
        shutil.copyfileobj(file.file, handle)
    label = name.strip() or Path(file.filename or "voice").stem or "voice"
    with RENDER.lock:
        project = _project(project_id)
        previous = project.voice_id
        project.voice_id = voice_id
        project.save()
    threading.Thread(target=_compile_voice,
                     args=(voice_id, source, label, project_id, previous),
                     daemon=True).start()
    return RENDER.snapshot(project_id)


class VoiceBody(BaseModel):
    name: str


@app.put("/api/voices/{voice_id}")
def rename_voice(voice_id: str, body: VoiceBody, p: str = "") -> dict:
    try:
        store.rename_voice(voice_id, body.name)
    except KeyError:
        raise HTTPException(404, "no such voice") from None
    return RENDER.snapshot(p or RENDER.pick_default())


@app.delete("/api/voices/{voice_id}")
def delete_voice(voice_id: str, p: str = "") -> dict:
    directory = store.voice_dir(voice_id)
    ENGINE.forget_voice(directory / "voice.pt")
    shutil.rmtree(directory, ignore_errors=True)
    # Any project that pointed at it now points at nothing, on disk too.
    with RENDER.lock:
        for project in RENDER.all_projects():
            if project.voice_id == voice_id:
                RENDER.adopt(project).voice_id = ""
                project.save()
    return RENDER.snapshot(p or RENDER.pick_default())


@app.get("/api/voices/{voice_id}/reference.wav")
def voice_reference(voice_id: str):
    path = store.voice_dir(voice_id) / "reference.wav"
    if not path.exists():
        raise HTTPException(404, "no reference audio")
    return FileResponse(path, media_type="audio/wav")


@app.exception_handler(RuntimeError)
def _runtime_error(_request, error: RuntimeError):
    return JSONResponse({"detail": str(error)}, status_code=400)


@app.exception_handler(store.InvalidID)
def _invalid_id(_request, error: store.InvalidID):
    return JSONResponse({"detail": str(error)}, status_code=400)


app.mount("/", StaticFiles(directory=WEB, html=True), name="web")
