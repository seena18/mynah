"""Application state and the render queue.

One worker thread does all the generating, across every project. Everything
that mutates a project takes the same lock, so an HTTP handler editing a chunk
cannot race the worker writing a take for it.

Projects are cached here once loaded; since there is one process, the cache is
the truth and disk is where it is written down.
"""

from __future__ import annotations

import threading
import time
from collections import deque

from . import audio, store
from .engine import ENGINE, Params

MAX_LOG = 60


class Renderer:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.wake = threading.Condition(self.lock)
        self.projects: dict[str, store.Project] = {}
        self.pending: deque[tuple[str, str]] = deque()     # (project id, chunk id)
        self.current: tuple[str, str] | None = None
        self.errors: dict[str, str] = {}                   # chunk id -> message
        self.log: deque[str] = deque(maxlen=MAX_LOG)
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    # ---- projects --------------------------------------------------------

    def get(self, project_id: str) -> store.Project:
        """The project, loading it on first touch. KeyError if it does not exist."""
        with self.lock:
            project = self.projects.get(project_id)
            if project is None:
                project = store.Project.load(project_id)
                self.projects[project_id] = project
            return project

    def adopt(self, project: store.Project) -> store.Project:
        with self.lock:
            self.projects[project.id] = project
            return project

    def forget(self, project_id: str) -> None:
        """Drop a project from memory and the queue; the caller deletes the files."""
        with self.lock:
            self.projects.pop(project_id, None)
            self.pending = deque(item for item in self.pending if item[0] != project_id)

    def pick_default(self) -> str:
        """The project to open when none was asked for: the most recently
        touched, or a fresh one if there are none at all."""
        listed = store.list_projects()
        if listed:
            return listed[0].id
        return self.adopt(store.create_project()).id

    def all_projects(self) -> list[store.Project]:
        """Every project, preferring the in-memory copy where one exists."""
        with self.lock:
            listed = store.list_projects()
            return [self.projects.get(p.id, p) for p in listed]

    # ---- queue -----------------------------------------------------------

    def submit(self, project_id: str, chunk_ids: list[str]) -> int:
        with self.lock:
            project = self.get(project_id)
            queued = 0
            for chunk_id in chunk_ids:
                key = (project_id, chunk_id)
                if key in self.pending or key == self.current:
                    continue
                if project.find(chunk_id) is None:
                    continue
                self.pending.append(key)
                self.errors.pop(chunk_id, None)
                queued += 1
            self.wake.notify_all()
            return queued

    def clear(self) -> tuple[int, bool]:
        """Drop everything queued and stop the line in flight.

        Returns (queued lines dropped, whether a running one was stopped)."""
        with self.lock:
            dropped = len(self.pending)
            self.pending.clear()
            stopping = self.current is not None
            if stopping:
                ENGINE.cancel()
            return dropped, stopping

    def note(self, message: str) -> None:
        self.log.append(f"{time.strftime('%H:%M:%S')}  {message}")

    # ---- worker ----------------------------------------------------------

    def _run(self) -> None:
        while True:
            with self.wake:
                while not self.pending:
                    self.wake.wait(timeout=1.0)
                key = self.pending.popleft()
                self.current = key
                project_id, chunk_id = key
                try:
                    project = self.get(project_id)
                    chunk = project.find(chunk_id)
                except KeyError:
                    project, chunk = None, None
                voice_id = project.voice_id if project else ""
                params = Params.from_dict(project.params) if project else Params()
                text = chunk.text.strip() if chunk else ""
                wanted = project.fingerprint(chunk) if (project and chunk) else ""
            try:
                if project is None:
                    raise RuntimeError("project was deleted")
                if not chunk or not text:
                    raise RuntimeError("chunk is empty")
                if not voice_id:
                    raise RuntimeError("no voice selected")
                voice_pt = store.voice_dir(voice_id) / "voice.pt"
                if not voice_pt.exists():
                    raise RuntimeError(f"voice {voice_id!r} has not been compiled")

                if ENGINE.state != "ready":
                    self.note("waiting for the model (first run downloads ~2.85 GB)")
                    ENGINE.wait_ready()

                self.note(f"generating: {text[:56]}")
                started = time.time()
                wav = ENGINE.speak(text, voice_pt, params)
                audio.save(wav, store.TAKES / f"{wanted}.wav", ENGINE.sample_rate)
                # Nothing to record: the take is named after the fingerprint it
                # satisfies, so if the text changed mid-render this file simply
                # becomes an orphan and the chunk stays stale. Self-correcting.
                self.note(f"done in {time.time() - started:.1f}s")
            except RuntimeError as error:
                if str(error) == "stopped":
                    # Not a failure: the user asked. The line simply stays stale.
                    self.note(f"stopped: {text[:56]}")
                else:
                    with self.lock:
                        self.errors[chunk_id] = f"RuntimeError: {error}"
                    self.note(f"failed: {error}"[:130])
            except Exception as error:  # noqa: BLE001 - shown in the UI
                message = f"{type(error).__name__}: {error}"
                with self.lock:
                    self.errors[chunk_id] = message
                self.note(f"failed: {message[:120]}")
            finally:
                with self.lock:
                    self.current = None

    # ---- view ------------------------------------------------------------

    def snapshot(self, project_id: str) -> dict:
        with self.lock:
            project = self.get(project_id)
            view = project.to_dict()
            queued_here = {cid for pid, cid in self.pending if pid == project_id}
            for chunk in view["chunks"]:
                if self.current == (project_id, chunk["id"]):
                    chunk["status"] = "rendering"
                elif chunk["id"] in queued_here:
                    chunk["status"] = "queued"
                if chunk["id"] in self.errors:
                    chunk["error"] = self.errors[chunk["id"]]
            return {
                "project": view,
                "projects": [p.summary() for p in self.all_projects()],
                "voices": store.list_voices(),
                "engine": {
                    "state": ENGINE.state,
                    "device": ENGINE.device,
                    "loader": ENGINE.loader,
                    "progress": ENGINE.progress,
                    "error": ENGINE.error,
                },
                "queue": {
                    "current": self.current[1] if self.current else "",
                    "pending": len(self.pending),
                },
                "log": list(self.log),
            }


RENDER = Renderer()
