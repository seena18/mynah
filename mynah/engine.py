"""The TTS model, loaded once and held.

Loading Chatterbox Turbo takes a while and ~4.4 GB on the GPU, so the whole app
shares one instance behind a lock. Generation is serialised: a Metal or CUDA
device has one queue anyway, and running two generations at once mostly
produces two slow ones.

Weights come from the Hugging Face Hub anonymously — the repo is public and
MIT-licensed — or from a folder named by MYNAH_MODEL_DIR for machines that
cannot reach the Hub. Upstream's own `from_pretrained` is never called: it
passes `token=True`, which makes huggingface_hub demand a login for a repo
that needs none.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import torch

REPO_ID = "ResembleAI/chatterbox-turbo"
# The checkpoint validated on Metal, native Windows CUDA and WSL2 CUDA.
# Pin every Hub operation so an upstream update cannot change a fresh install.
MODEL_REVISION = "749d1c1a46eb10492095d68fbcf55691ccf137cd"
MODEL_DIR_ENV = "MYNAH_MODEL_DIR"
# The files Turbo's loader actually opens. Upstream's loader fetches every
# *.safetensors in the repo, which includes the 1 GB s3gen.safetensors that the
# Turbo class never reads — a quarter of the first-run download for nothing.
MODEL_FILES = [
    "ve.safetensors", "t3_turbo_v1.safetensors", "s3gen_meanflow.safetensors",
    "conds.pt", "tokenizer_config.json", "vocab.json", "merges.txt",
    "special_tokens_map.json", "added_tokens.json",
]
# Sum of MODEL_FILES on the Hub at the pinned revision; used for the progress
# readout when the Hub cannot be asked for exact sizes.
EXPECTED_BYTES = 2_987_680_596
# What upstream's loader would fetch. An incomplete snapshot can retry this
# broader set, always at the same pinned revision.
UPSTREAM_PATTERNS = ["*.safetensors", "*.json", "*.txt", "*.pt", "*.model"]


class Cancelled(Exception):
    """Raised inside the model's decode loop when the current line was stopped."""


def guard(forward, is_cancelled):
    """Wrap a per-step call so a pending stop is honoured at the next step.

    Upstream's decode loop is a plain Python loop that calls the transformer
    once per token with no hook for interruption. Checking a flag before each
    call costs nothing measurable and makes Stop land within one step — ~6 ms
    on a 4080, ~100 ms on Metal — instead of after the whole line.
    """
    def guarded(*args, **kwargs):
        if is_cancelled():
            raise Cancelled()
        return forward(*args, **kwargs)
    return guarded


def pick_device() -> str:
    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


@dataclass
class Params:
    """The knobs Turbo actually reads.

    The base Chatterbox model also takes `exaggeration` and `cfg_weight`; Turbo
    logs a warning and ignores them, so they are deliberately absent here rather
    than exposed as controls that do nothing.
    """

    temperature: float = 0.8
    top_p: float = 0.95
    top_k: int = 1000
    repetition_penalty: float = 1.2

    @classmethod
    def from_dict(cls, data: dict) -> "Params":
        known = {f: data[f] for f in cls.__dataclass_fields__ if f in data}
        return cls(**known)


def _complete(directory: Path) -> bool:
    return all((directory / name).is_file() for name in MODEL_FILES)


def _dir_bytes(directory: Path) -> int:
    return sum(p.stat().st_size for p in directory.rglob("*") if p.is_file())


class Engine:
    def __init__(self) -> None:
        self.device = pick_device()
        self.state = "cold"          # cold | loading | ready | error
        self.error = ""
        self.loader = ""             # "hub" | "manual" | "hub-broad"
        self.progress = ""           # human-readable download progress, or ""
        self._model = None
        self._conds_cls = None
        self._lock = threading.RLock()
        self._voice_cache: dict[str, object] = {}
        self._downloading = False
        self._cancel = False

    # ---- weights ---------------------------------------------------------

    @staticmethod
    def manual_dir() -> Path | None:
        """A folder of weights named by MYNAH_MODEL_DIR, if set and complete."""
        raw = os.environ.get(MODEL_DIR_ENV, "").strip()
        if not raw:
            return None
        directory = Path(raw).expanduser()
        return directory if _complete(directory) else None

    @staticmethod
    def cached() -> tuple[Path, int] | None:
        """Where the weights already are and how big, without touching the
        network — or None if a download is still ahead."""
        manual = Engine.manual_dir()
        if manual:
            return manual, sum((manual / n).stat().st_size for n in MODEL_FILES)
        from huggingface_hub import try_to_load_from_cache

        paths = [try_to_load_from_cache(REPO_ID, name, revision=MODEL_REVISION)
                 for name in MODEL_FILES]
        if not all(isinstance(p, str) for p in paths):
            return None
        return Path(paths[0]).parent, sum(Path(p).stat().st_size for p in paths)

    def _expected_bytes(self) -> int:
        try:
            from huggingface_hub import HfApi

            info = HfApi().model_info(REPO_ID, revision=MODEL_REVISION,
                                     files_metadata=True, token=False)
            wanted = {s.rfilename: (s.size or 0) for s in info.siblings}
            total = sum(wanted.get(name, 0) for name in MODEL_FILES)
            return total or EXPECTED_BYTES
        except Exception:  # noqa: BLE001 - offline, rate-limited: use the constant
            return EXPECTED_BYTES

    def _watch_download(self, expected: int, log) -> None:
        """Report bytes landed in the Hub cache while snapshot_download runs.

        huggingface_hub writes blobs as `<hash>.incomplete` and renames them
        when done, so the sum of the blobs folder is the honest progress.
        """
        from huggingface_hub.constants import HF_HUB_CACHE

        blobs = Path(HF_HUB_CACHE) / f"models--{REPO_ID.replace('/', '--')}" / "blobs"
        last_said = 0.0
        while self._downloading:
            done = _dir_bytes(blobs) if blobs.exists() else 0
            self.progress = (f"downloading weights {min(done, expected) / 1e9:.2f} / "
                             f"{expected / 1e9:.2f} GB")
            if log and time.monotonic() - last_said > 5:
                log(self.progress)
                last_said = time.monotonic()
            time.sleep(1.0)

    def download(self, log=None, patterns: list[str] | None = None) -> Path:
        """Fetch the weights if they are not already here; return their folder.

        Anonymous: the repo is public. A watcher thread fills `self.progress`
        for the UI, since huggingface_hub's own progress bar is a terminal
        thing and this app's user is looking at a browser.
        """
        manual = self.manual_dir()
        if manual:
            return manual
        from huggingface_hub import snapshot_download

        already = self.cached()
        if already and patterns is None:
            return already[0]
        expected = self._expected_bytes()
        self._downloading = True
        threading.Thread(target=self._watch_download, args=(expected, log),
                         daemon=True).start()
        try:
            path = snapshot_download(repo_id=REPO_ID,
                                     revision=MODEL_REVISION, token=False,
                                     allow_patterns=patterns or MODEL_FILES)
        finally:
            self._downloading = False
            self.progress = ""
        return Path(path)

    # ---- lifecycle -------------------------------------------------------

    def load(self, log=None) -> None:
        """Bring the model up. Safe to call repeatedly; only the first works."""
        with self._lock:
            if self.state in ("ready", "loading"):
                return
            self.state = "loading"
        try:
            from chatterbox.tts_turbo import ChatterboxTurboTTS, Conditionals

            checkpoint = self.download(log=log)
            loader = "manual" if self.manual_dir() else "hub"
            if not _complete(checkpoint):
                # Retry an incomplete snapshot with the upstream file patterns,
                # retaining the release's pinned revision and anonymous access.
                checkpoint = self.download(log=log, patterns=UPSTREAM_PATTERNS)
                loader = "hub-broad"
            model = ChatterboxTurboTTS.from_local(checkpoint, self.device)
            model.t3.tfmr.forward = guard(model.t3.tfmr.forward, lambda: self._cancel)
            with self._lock:
                self._model, self._conds_cls = model, Conditionals
                self.loader = loader
                self.progress = ""
                self.state = "ready"
        except Exception as error:  # noqa: BLE001 - surfaced to the UI verbatim
            # Name the innermost frame: "'NoneType' object is not callable" on
            # its own sent a whole debugging session into the wrong library.
            import traceback

            frame = traceback.extract_tb(error.__traceback__)[-1]
            where = f"{Path(frame.filename).name}:{frame.lineno} in {frame.name}"
            with self._lock:
                self.progress = ""
                self.state = "error"
                self.error = f"{type(error).__name__}: {error} ({where})"

    def warm(self) -> None:
        threading.Thread(target=self.load, daemon=True).start()

    def wait_ready(self, timeout: float = 1800.0) -> None:
        """Block until the model is usable, or say why it will never be.

        `load()` returns straight away when another thread is already loading,
        so a caller that only calls `load()` can still reach a half-built model
        and fail. Anything that needs the model waits here instead.
        """
        self.load()                       # no-op if a warm-up got there first
        deadline = time.monotonic() + timeout
        while self.state == "loading":
            if time.monotonic() > deadline:
                raise RuntimeError(f"model still loading after {timeout:.0f}s")
            time.sleep(0.25)
        if self.state != "ready":
            raise RuntimeError(f"model failed to load: {self.error}")

    @property
    def sample_rate(self) -> int:
        return getattr(self._model, "sr", 24000)

    def _require(self):
        if self.state != "ready":
            raise RuntimeError(f"model is {self.state}: {self.error or 'not loaded yet'}")
        return self._model

    # ---- voices ----------------------------------------------------------

    def compile_voice(self, reference_wav: Path, out_path: Path) -> Path:
        """Turn a reference recording into reusable conditionals.

        This is the step that makes per-chunk regeneration cheap: the speaker
        embedding is computed once here, not on every line of the script.

        `norm_loudness` is off because the model's version returns float64,
        which Metal cannot take. `audio.to_wav` has already applied the same
        normalisation in float32.
        """
        with self._lock:
            model = self._require()
            model.prepare_conditionals(str(reference_wav), norm_loudness=False)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            model.conds.save(str(out_path))
        return out_path

    def _use_voice(self, voice_pt: Path) -> None:
        model = self._require()
        key = str(voice_pt)
        conds = self._voice_cache.get(key)
        if conds is None:
            conds = self._conds_cls.load(str(voice_pt), map_location=self.device)
            conds.to(self.device)
            self._voice_cache[key] = conds
        model.conds = conds

    def forget_voice(self, voice_pt: Path) -> None:
        self._voice_cache.pop(str(voice_pt), None)

    # ---- generation ------------------------------------------------------

    def cancel(self) -> None:
        """Stop the line being generated. Deliberately lock-free: the worker
        holds the engine lock for the whole generation, and this is called from
        the request thread while it does."""
        self._cancel = True

    def speak(self, text: str, voice_pt: Path, params: Params) -> torch.Tensor:
        """One chunk of text to a (1, samples) waveform on the CPU.

        Raises RuntimeError("stopped") if cancel() was called while it ran; a
        half-generated line is not worth keeping, so nothing is returned.
        """
        with self._lock:
            model = self._require()
            self._use_voice(voice_pt)
            try:
                return model.generate(
                    text,
                    temperature=params.temperature,
                    top_p=params.top_p,
                    top_k=params.top_k,
                    repetition_penalty=params.repetition_penalty,
                    audio_prompt_path=None,
                )
            except Cancelled:
                raise RuntimeError("stopped") from None
            finally:
                self._cancel = False


ENGINE = Engine()
