"""Projects on disk: chunks, voices, and what has actually been rendered.

The centre of this file is the fingerprint. A chunk's audio is named after a
hash of everything that affects how it sounds — its text, the voice, the
sampling parameters — so a chunk is stale exactly when that hash no longer
matches the file on disk. That is what lets you re-render one line and keep the
other forty, and it also means editing a line back to what it was restores the
old take for free instead of paying for it twice.

Takes and voices are shared across projects for the same reason: the hash does
not know which project asked, so two projects that speak the same line in the
same voice share one file.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
TAKES = DATA / "takes"
VOICES = DATA / "voices"
PROJECTS = DATA / "projects"
LEGACY_PROJECT_FILE = DATA / "project.json"     # single-project layout, pre-0.2

DEFAULT_CHUNK_CHARS = 280


def new_id() -> str:
    return uuid.uuid4().hex[:10]


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


# ---- splitting -----------------------------------------------------------

def _pack(pieces: list[str], max_chars: int) -> list[str]:
    """Greedily fill chunks up to the limit, never splitting a piece."""
    packed, current = [], ""
    for piece in pieces:
        if current and len(current) + 1 + len(piece) > max_chars:
            packed.append(current)
            current = piece
        else:
            current = f"{current} {piece}".strip()
    if current:
        packed.append(current)
    return packed


def split_script(text: str, max_chars: int = DEFAULT_CHUNK_CHARS) -> list[str]:
    """Break a script into speakable chunks.

    Blank lines win, because a writer who left one meant a break there. Only an
    over-long paragraph is broken further: first at sentence ends, and then, if
    a single "sentence" is still too long — unpunctuated prose, a list, a
    transcript — at word boundaries, so the limit always holds.

    Packing is greedy rather than even because longer chunks sound better:
    prosody restarts at every chunk boundary, so fewer boundaries is smoother.
    """
    chunks: list[str] = []
    for paragraph in re.split(r"\n\s*\n", text.strip()):
        paragraph = " ".join(paragraph.split())
        if not paragraph:
            continue
        if len(paragraph) <= max_chars:
            chunks.append(paragraph)
            continue
        sentences = [s.strip() for s in re.findall(r"[^.!?]+[.!?]*\s*", paragraph)]
        units: list[str] = []
        for sentence in filter(None, sentences):
            if len(sentence) <= max_chars:
                units.append(sentence)
            else:
                units.extend(_pack(sentence.split(), max_chars))
        chunks.extend(_pack(units, max_chars))
    return chunks


# ---- model ---------------------------------------------------------------

@dataclass
class Chunk:
    """A line of the script.

    Note what is *not* here: any record of what has been rendered. A chunk's
    audio lives at a path derived from its fingerprint, so "is this rendered?"
    is answered by looking on disk, and reverting an edit restores the earlier
    take for free rather than merely being allowed to.
    """

    id: str = field(default_factory=new_id)
    text: str = ""
    pause_after: float = 0.4


@dataclass
class Project:
    id: str = field(default_factory=new_id)
    title: str = "Untitled"
    voice_id: str = ""
    params: dict = field(default_factory=lambda: {
        "temperature": 0.8, "top_p": 0.95, "top_k": 1000, "repetition_penalty": 1.2,
    })
    chunks: list[Chunk] = field(default_factory=list)
    created: str = field(default_factory=now)
    updated: str = field(default_factory=now)

    # -- fingerprinting --

    def fingerprint(self, chunk: Chunk) -> str:
        """Everything that changes the audio, and nothing that does not.

        `pause_after` is excluded on purpose: silence is added at stitch time,
        so changing a pause must not invalidate a perfectly good take. The
        project id is excluded too, so identical lines share takes.
        """
        payload = json.dumps(
            {"text": chunk.text.strip(), "voice": self.voice_id, "params": self.params},
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:20]

    def take_path(self, chunk: Chunk) -> Path:
        return TAKES / f"{self.fingerprint(chunk)}.wav"

    def status(self, chunk: Chunk) -> str:
        if not chunk.text.strip():
            return "empty"
        if not self.voice_id or not (voice_dir(self.voice_id) / "voice.pt").exists():
            # Either nothing is selected, or the selected voice is still
            # compiling. Both mean this chunk cannot be rendered yet.
            return "no-voice"
        return "ready" if self.take_path(chunk).exists() else "stale"

    def find(self, chunk_id: str) -> Chunk | None:
        return next((c for c in self.chunks if c.id == chunk_id), None)

    def live_fingerprints(self) -> set[str]:
        return {self.fingerprint(c) for c in self.chunks if c.text.strip()}

    # -- persistence --

    @property
    def directory(self) -> Path:
        return PROJECTS / self.id

    def to_dict(self) -> dict:
        data = asdict(self)
        data["chunks"] = [
            {**asdict(c), "status": self.status(c), "fingerprint": self.fingerprint(c)}
            for c in self.chunks
        ]
        return data

    def summary(self) -> dict:
        """What a project list needs, without the chunk bodies."""
        counts: dict[str, int] = {}
        for chunk in self.chunks:
            state = self.status(chunk)
            counts[state] = counts.get(state, 0) + 1
        return {"id": self.id, "title": self.title, "updated": self.updated,
                "created": self.created, "chunks": len(self.chunks), "counts": counts}

    def save(self) -> None:
        self.updated = now()
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / "project.json").write_text(json.dumps(asdict(self), indent=2) + "\n")

    @classmethod
    def from_dict(cls, raw: dict) -> "Project":
        fields = Chunk.__dataclass_fields__
        chunks = [Chunk(**{k: v for k, v in c.items() if k in fields})
                  for c in raw.get("chunks", [])]
        known = {k: v for k, v in raw.items()
                 if k in cls.__dataclass_fields__ and k != "chunks"}
        return cls(**known, chunks=chunks)

    @classmethod
    def load(cls, project_id: str) -> "Project":
        path = PROJECTS / project_id / "project.json"
        if not path.exists():
            raise KeyError(project_id)
        project = cls.from_dict(json.loads(path.read_text()))
        project.id = project_id            # the directory name is authoritative
        return project


# ---- projects ------------------------------------------------------------

def list_projects() -> list[Project]:
    """Every project on disk, most recently touched first."""
    if not PROJECTS.exists():
        return []
    found = []
    for directory in PROJECTS.iterdir():
        if directory.is_dir() and (directory / "project.json").exists():
            try:
                found.append(Project.load(directory.name))
            except (OSError, ValueError, TypeError):
                continue      # a half-written file must not take the app down
    return sorted(found, key=lambda p: p.updated, reverse=True)


def create_project(title: str = "Untitled") -> Project:
    project = Project(title=title.strip() or "Untitled")
    project.save()
    return project


def delete_project(project_id: str) -> None:
    shutil.rmtree(PROJECTS / project_id, ignore_errors=True)


def migrate_legacy() -> Project | None:
    """Adopt a pre-projects `data/project.json` as the first project.

    Runs at startup, once: the old file is renamed rather than deleted, so a
    rollback still has it. Nothing happens if projects already exist.
    """
    if not LEGACY_PROJECT_FILE.exists() or list_projects():
        return None
    raw = json.loads(LEGACY_PROJECT_FILE.read_text())
    project = Project.from_dict(raw)
    project.id = new_id()
    project.save()
    LEGACY_PROJECT_FILE.rename(LEGACY_PROJECT_FILE.with_suffix(".json.migrated"))
    return project


# ---- voices --------------------------------------------------------------

def voice_dir(voice_id: str) -> Path:
    return VOICES / voice_id


def list_voices() -> list[dict]:
    if not VOICES.exists():
        return []
    voices = []
    for directory in sorted(VOICES.iterdir()):
        meta = directory / "meta.json"
        if directory.is_dir() and meta.exists():
            entry = json.loads(meta.read_text())
            entry["ready"] = (directory / "voice.pt").exists()
            voices.append(entry)
    return sorted(voices, key=lambda v: v.get("created", ""), reverse=True)


def unused_takes() -> list[Path]:
    """Takes no chunk in any project points at — old versions of edited lines."""
    if not TAKES.exists():
        return []
    live: set[str] = set()
    for project in list_projects():
        live |= project.live_fingerprints()
    return [p for p in TAKES.glob("*.wav") if p.stem not in live]
