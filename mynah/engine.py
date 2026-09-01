"""The TTS model, loaded once and held.

Loading Chatterbox Turbo takes minutes and ~3 GB, so the whole app shares one
instance behind a lock. Generation is serialised: a Metal or CUDA device has one
queue anyway, and running two generations at once mostly produces two slow ones.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path

import torch

REPO_ID = "ResembleAI/chatterbox-turbo"
# The files Turbo's loader actually opens. Upstream's from_pretrained fetches
# every *.safetensors in the repo, which includes the 1 GB s3gen.safetensors
# that the Turbo class never reads — a quarter of the first-run download for
# nothing. Listing the files means a new user downloads ~2.85 GB, not ~3.85.
MODEL_FILES = [
    "ve.safetensors", "t3_turbo_v1.safetensors", "s3gen_meanflow.safetensors",
    "conds.pt", "tokenizer_config.json", "vocab.json", "merges.txt",
    "special_tokens_map.json", "added_tokens.json",
]


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


class Engine:
    def __init__(self) -> None:
        self.device = pick_device()
        self.state = "cold"          # cold | loading | ready | error
        self.error = ""
        self.loader = ""             # "local" (narrow download) or "upstream"
        self._model = None
        self._conds_cls = None
        self._lock = threading.RLock()
        self._voice_cache: dict[str, object] = {}

    # ---- lifecycle -------------------------------------------------------

    def load(self) -> None:
        """Bring the model up. Safe to call repeatedly; only the first works."""
        with self._lock:
            if self.state in ("ready", "loading"):
                return
            self.state = "loading"
        try:
            from chatterbox.tts_turbo import ChatterboxTurboTTS, Conditionals
            from huggingface_hub import snapshot_download

            try:
                checkpoint = Path(snapshot_download(
                    repo_id=REPO_ID, allow_patterns=MODEL_FILES))
                model = ChatterboxTurboTTS.from_local(checkpoint, self.device)
                loader = "local"
            except Exception:  # noqa: BLE001 - see below
                # If upstream renames a file, the explicit list above goes
                # stale before this code does. Fall back to their loader
                # rather than fail on a filename.
                model = ChatterboxTurboTTS.from_pretrained(device=self.device)
                loader = "upstream"
            with self._lock:
                self._model, self._conds_cls = model, Conditionals
                self.loader = loader
                self.state = "ready"
        except Exception as error:  # noqa: BLE001 - surfaced to the UI verbatim
            with self._lock:
                self.state, self.error = "error", f"{type(error).__name__}: {error}"

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

    def speak(self, text: str, voice_pt: Path, params: Params) -> torch.Tensor:
        """One chunk of text to a (1, samples) waveform on the CPU."""
        with self._lock:
            model = self._require()
            self._use_voice(voice_pt)
            return model.generate(
                text,
                temperature=params.temperature,
                top_p=params.top_p,
                top_k=params.top_k,
                repetition_penalty=params.repetition_penalty,
                audio_prompt_path=None,
            )


ENGINE = Engine()
