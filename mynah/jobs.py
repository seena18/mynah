"""Application state and the render queue.

One worker thread does all the generating. Everything that mutates the project
takes the same lock, so an HTTP handler editing a chunk cannot race the worker
writing a take into it.
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
        self.project = store.Project.load()
        self.lock = threading.RLock()
        self.wake = threading.Condition(self.lock)
        self.pending: deque[str] = deque()
        self.current: str = ""
        self.errors: dict[str, str] = {}
        self.log: deque[str] = deque(maxlen=MAX_LOG)
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    # ---- queue -----------------------------------------------------------

    def submit(self, chunk_ids: list[str]) -> int:
        with self.lock:
            queued = 0
            for chunk_id in chunk_ids:
                if chunk_id in self.pending or chunk_id == self.current:
                    continue
                if self.project.find(chunk_id) is None:
                    continue
                self.pending.append(chunk_id)
                self.errors.pop(chunk_id, None)
                queued += 1
            self.wake.notify_all()
            return queued

    def clear(self) -> int:
        with self.lock:
            dropped = len(self.pending)
            self.pending.clear()
            return dropped

    def note(self, message: str) -> None:
        self.log.append(f"{time.strftime('%H:%M:%S')}  {message}")

    # ---- worker ----------------------------------------------------------

    def _run(self) -> None:
        while True:
            with self.wake:
                while not self.pending:
                    self.wake.wait(timeout=1.0)
                chunk_id = self.pending.popleft()
                self.current = chunk_id
                chunk = self.project.find(chunk_id)
                voice_id = self.project.voice_id
                params = Params.from_dict(self.project.params)
                text = chunk.text.strip() if chunk else ""
                wanted = self.project.fingerprint(chunk) if chunk else ""
            try:
                if not chunk or not text:
                    raise RuntimeError("chunk is empty")
                if not voice_id:
                    raise RuntimeError("no voice selected")
                voice_pt = store.voice_dir(voice_id) / "voice.pt"
                if not voice_pt.exists():
                    raise RuntimeError(f"voice {voice_id!r} has not been compiled")

                if ENGINE.state != "ready":
                    self.note("waiting for the model (first run downloads ~3 GB)")
                    ENGINE.wait_ready()

                self.note(f"generating: {text[:56]}")
                started = time.time()
                wav = ENGINE.speak(text, voice_pt, params)
                audio.save(wav, store.TAKES / f"{wanted}.wav", ENGINE.sample_rate)
                took = time.time() - started

                # Nothing to record: the take is named after the fingerprint it
                # satisfies, so if the text changed mid-render this file simply
                # becomes an orphan and the chunk stays stale. Self-correcting.
                self.note(f"done in {took:.1f}s")
            except Exception as error:  # noqa: BLE001 - shown in the UI
                message = f"{type(error).__name__}: {error}"
                with self.lock:
                    self.errors[chunk_id] = message
                self.note(f"failed: {message[:120]}")
            finally:
                with self.lock:
                    self.current = ""

    # ---- view ------------------------------------------------------------

    def snapshot(self) -> dict:
        with self.lock:
            project = self.project.to_dict()
            for chunk in project["chunks"]:
                if chunk["id"] == self.current:
                    chunk["status"] = "rendering"
                elif chunk["id"] in self.pending:
                    chunk["status"] = "queued"
                if chunk["id"] in self.errors:
                    chunk["error"] = self.errors[chunk["id"]]
            return {
                "project": project,
                "voices": store.list_voices(),
                "engine": {
                    "state": ENGINE.state,
                    "device": ENGINE.device,
                    "error": ENGINE.error,
                },
                "queue": {"current": self.current, "pending": len(self.pending)},
                "log": list(self.log),
            }


RENDER = Renderer()
