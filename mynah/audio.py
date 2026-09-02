"""Audio plumbing: get uploads into a shape the model accepts, and glue the
finished chunks back together.

ffmpeg comes from imageio-ffmpeg, which ships its own binary, so a clone of this
repo does not also need a system ffmpeg on PATH.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import soundfile as sf

if TYPE_CHECKING:            # torch is only a type here; the tests run without it
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


def save(wav: "torch.Tensor", path: Path, sample_rate: int) -> Path:
    """Write a (1, samples) tensor as a WAV. Duck-typed on purpose, so this
    module — and the test suite — never imports torch."""
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), wav.squeeze(0).cpu().numpy(), sample_rate)
    return path


# Stitch-time conditioning. The model leaves ~0.1 s of silence before speech
# and ~0.2 s after, so without trimming a "0.4 s pause" is really ~0.7 s and
# the setting lies. Takes from one voice also land a couple of dB apart from
# each other, which is audible exactly at the joins.
TRIM_DB = -50.0        # below this counts as silence
EDGE_MARGIN = 0.04     # seconds of the model's own silence kept at each end
FADE = 0.008           # seconds; kills clicks where takes butt together


def _trim(data, rate: int):
    import numpy as np

    idx = np.flatnonzero(np.abs(data) > 10 ** (TRIM_DB / 20))
    if idx.size == 0:
        return data
    margin = int(EDGE_MARGIN * rate)
    return data[max(0, idx[0] - margin): min(len(data), idx[-1] + 1 + margin)]


def _fade(data, rate: int):
    import numpy as np

    n = min(int(FADE * rate), len(data) // 2)
    if n <= 0:
        return data
    ramp = np.linspace(0.0, 1.0, n, dtype="float32")
    data = data.copy()
    data[:n] *= ramp
    data[-n:] *= ramp[::-1]
    return data


def _match_loudness(pieces: list, rate: int) -> list:
    """Bring every take to the median loudness of the set.

    The median, not a fixed target, so one odd take moves and the rest stay
    where the reference put them. Takes too short to measure are left alone.
    """
    import numpy as np
    import pyloudnorm

    meter = pyloudnorm.Meter(rate)
    levels = []
    for data in pieces:
        try:
            level = meter.integrated_loudness(data.astype("float64"))
        except Exception:  # noqa: BLE001 - shorter than one 400 ms block
            level = float("nan")
        levels.append(level if np.isfinite(level) else float("nan"))
    finite = [l for l in levels if np.isfinite(l)]
    if len(finite) < 2:
        return pieces
    target = float(np.median(finite))
    out = []
    for data, level in zip(pieces, levels):
        if np.isfinite(level):
            data = data * (10 ** ((target - level) / 20))
        out.append(data.astype("float32"))
    return out


def stitch(pieces: list[tuple[Path, float]], target: Path, sample_rate: int) -> list[dict]:
    """Concatenate rendered chunks, inserting each one's trailing pause.

    Pauses are silence written here rather than asked of the model. A pause
    spoken by the model costs a separate generation, and every generation break
    restarts the prosody — which is what makes stitched narration sound choppy.

    Returns each piece's (start, end) in the mix, in seconds, in the same order
    as `pieces` — the UI uses this to highlight the line currently playing
    during stitched playback. It comes from here, not a separate estimate,
    because trimming above already changed each take's length and only this
    function knows by how much.
    """
    import numpy as np

    takes, pauses = [], []
    for path, pause in pieces:
        data, rate = sf.read(str(path), dtype="float32")
        if rate != sample_rate:
            raise RuntimeError(f"{path.name} is {rate} Hz, expected {sample_rate}")
        takes.append(_trim(data, rate))
        pauses.append(max(0.0, pause))
    if not takes:
        raise RuntimeError("nothing to stitch: no chunks have been generated yet")

    takes = [_fade(t, sample_rate) for t in _match_loudness(takes, sample_rate)]
    parts: list = []
    segments: list[dict] = []
    offset = 0.0
    for data, pause in zip(takes, pauses):
        parts.append(data)
        end = offset + len(data) / sample_rate
        segments.append({"start": round(offset, 3), "end": round(end, 3)})
        offset = end
        if pause > 0:
            parts.append(np.zeros(int(pause * sample_rate), dtype="float32"))
            offset += pause
    mixed = np.concatenate(parts)
    peak = float(np.max(np.abs(mixed))) if mixed.size else 0.0
    if peak > 0.99:
        mixed *= 0.99 / peak
    target.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(target), mixed, sample_rate)
    return segments
