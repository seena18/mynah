"""Audio plumbing: get uploads into a shape the model accepts, and glue the
finished chunks back together.

ffmpeg comes from imageio-ffmpeg, which ships its own binary, so a clone of this
repo does not also need a system ffmpeg on PATH.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import soundfile as sf
import torch

# prepare_conditionals asserts the reference is longer than five seconds. Catch
# it here instead, where there is a person to tell.
MIN_REFERENCE_SECONDS = 5.0


def ffmpeg_exe() -> str:
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


# The loudness the model's own normaliser targets. Matched here so that doing
# it ourselves changes nothing but the dtype.
TARGET_LUFS = -27.0


def to_wav(source: Path, target: Path, sample_rate: int = 24000) -> Path:
    """Normalise any upload or browser recording to mono float32 at one loudness.

    Browsers record WebM/Opus, which soundfile cannot read, so everything goes
    through ffmpeg rather than only the formats that happen to need it.

    The loudness pass is done here, in float32, because the model's built-in one
    runs through pyloudnorm and hands back float64 — which Metal refuses,
    failing voice compilation on Apple silicon. Doing it first lets the model's
    own pass be switched off without losing the normalisation.
    """
    import numpy as np
    import pyloudnorm

    target.parent.mkdir(parents=True, exist_ok=True)
    decoded = target.with_suffix(".decoded.wav")
    result = subprocess.run(
        [ffmpeg_exe(), "-y", "-v", "error", "-i", str(source),
         "-ac", "1", "-ar", str(sample_rate), "-c:a", "pcm_s16le", str(decoded)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        decoded.unlink(missing_ok=True)
        raise RuntimeError(f"could not decode audio: {result.stderr.strip()[:300]}")

    data, rate = sf.read(str(decoded), dtype="float32")
    decoded.unlink(missing_ok=True)
    if data.size == 0:
        raise RuntimeError("the recording is empty")
    try:
        measured = pyloudnorm.Meter(rate).integrated_loudness(data.astype("float64"))
        if np.isfinite(measured):
            gain = 10.0 ** ((TARGET_LUFS - measured) / 20.0)
            data = (data * gain).astype("float32")
    except Exception:  # noqa: BLE001 - too short to measure; leave the level alone
        pass
    peak = float(np.max(np.abs(data))) if data.size else 0.0
    if peak > 0.99:
        data = (data * (0.99 / peak)).astype("float32")
    sf.write(str(target), data, rate, subtype="FLOAT")
    return target


def duration(path: Path) -> float:
    info = sf.info(str(path))
    return info.frames / float(info.samplerate)


def save(wav: torch.Tensor, path: Path, sample_rate: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), wav.squeeze(0).cpu().numpy(), sample_rate)
    return path


def stitch(pieces: list[tuple[Path, float]], target: Path, sample_rate: int) -> Path:
    """Concatenate rendered chunks, inserting each one's trailing pause.

    Pauses are silence written here rather than asked of the model. A pause
    spoken by the model costs a separate generation, and every generation break
    restarts the prosody — which is what makes stitched narration sound choppy.
    """
    import numpy as np

    parts: list = []
    for path, pause in pieces:
        data, rate = sf.read(str(path), dtype="float32")
        if rate != sample_rate:
            raise RuntimeError(f"{path.name} is {rate} Hz, expected {sample_rate}")
        parts.append(data)
        if pause > 0:
            parts.append(np.zeros(int(pause * sample_rate), dtype="float32"))
    if not parts:
        raise RuntimeError("nothing to stitch: no chunks have been generated yet")
    target.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(target), np.concatenate(parts), sample_rate)
    return target
