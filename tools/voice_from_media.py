#!/usr/bin/env python3
"""Pull the best voice sample out of a long recording and add it as a voice.

The model reads only the first 10 seconds of a reference (15 for the
tokenizer) and throws the rest away — see ChatterboxTurboTTS.prepare_conditionals.
Hand it an hour-long file and the clone is built from whatever happens to be at
the very start: an intro sting, a breath, silence. So this finds a stretch of
continuous, clean, unclipped speech and uploads only that.

    uv run tools/voice_from_media.py recording.m4a --name "Me"

Any format ffmpeg reads works, video included — the audio track is taken.
Clone your own voice, or one you have permission to use.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import tempfile
import urllib.parse
import urllib.request
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]

SAMPLE_RATE = 24000
FRAME = 0.02          # seconds per analysis frame
FLOOR_DB = -50.0      # below this a frame is silence, matching audio.TRIM_DB
CLIP = 0.98           # samples at or above this count as clipped


def ffmpeg() -> str:
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def decode(source: pathlib.Path, target: pathlib.Path) -> "tuple":
    """Whole file to mono float32 at the model's rate."""
    import soundfile as sf

    result = subprocess.run(
        [ffmpeg(), "-y", "-v", "error", "-i", str(source), "-ac", "1",
         "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le", str(target)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise SystemExit(f"could not decode {source.name}:\n  {result.stderr.strip()[:400]}")
    data, rate = sf.read(str(target), dtype="float32")
    return data, rate


def pick_window(data, rate: int, seconds: float) -> tuple[float, dict]:
    """Best start offset for a `seconds` sample of speech.

    Scored on how much of the window is actually voiced, penalising clipping
    hard: a clipped reference bakes distortion into every line the voice ever
    says. The winner is then nudged to begin just after a pause so the sample
    does not open mid-syllable.
    """
    import numpy as np

    hop = int(FRAME * rate)
    usable = len(data) // hop * hop
    if usable < seconds * rate:
        return 0.0, {"note": "file is shorter than the requested window"}
    frames = data[:usable].reshape(-1, hop)

    rms = np.sqrt((frames.astype("float64") ** 2).mean(axis=1)) + 1e-12
    db = 20 * np.log10(rms)
    voiced = db > max(FLOOR_DB, np.percentile(db, 95) - 28.0)
    clipped = (np.abs(frames) >= CLIP).any(axis=1)

    span = int(seconds / FRAME)
    step = max(1, int(0.25 / FRAME))
    best, best_score, best_stats = 0, -1e9, {}
    for start in range(0, len(frames) - span + 1, step):
        window = slice(start, start + span)
        voiced_fraction = float(voiced[window].mean())
        clip_fraction = float(clipped[window].mean())
        score = voiced_fraction - 6.0 * clip_fraction
        if score > best_score:
            best, best_score = start, score
            best_stats = {"voiced": round(voiced_fraction, 3),
                          "clipped": round(clip_fraction, 4),
                          "median_db": round(float(np.median(db[window])), 1)}

    # Nudge onto a pause within half a second either way, so the sample does
    # not begin mid-word.
    reach = int(0.5 / FRAME)
    low = max(0, best - reach)
    high = min(len(frames) - span, best + reach)
    if high > low:
        quietest = low + int(np.argmin(db[low:high]))
        if voiced[quietest:quietest + span].mean() >= best_stats.get("voiced", 0) - 0.08:
            best = quietest
    return best * FRAME, best_stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("media", type=pathlib.Path, help="audio or video file")
    parser.add_argument("--name", default="", help="name for the voice")
    parser.add_argument("--seconds", type=float, default=16.0,
                        help="sample length; the model reads 15 at most, "
                             "so more than ~18 only slows the upload")
    parser.add_argument("--url", default="http://localhost:8765")
    parser.add_argument("--write", type=pathlib.Path,
                        help="save the clip here instead of uploading it")
    args = parser.parse_args()

    if not args.media.exists():
        raise SystemExit(f"no such file: {args.media}")
    if args.seconds < 6:
        raise SystemExit("the model refuses a reference under 5s; use at least 6")

    import soundfile as sf

    with tempfile.TemporaryDirectory() as tmp:
        whole = pathlib.Path(tmp) / "whole.wav"
        data, rate = decode(args.media, whole)
        length = len(data) / rate
        print(f"{args.media.name}: {length:.1f}s")
        if length < 6:
            raise SystemExit("that file is under 6 seconds of audio")

        start, stats = pick_window(data, rate, min(args.seconds, length))
        end = min(length, start + args.seconds)
        print(f"best {end - start:.1f}s starts at {start:.1f}s  "
              + "  ".join(f"{k}={v}" for k, v in stats.items()))

        clip = data[int(start * rate):int(end * rate)]
        target = args.write or (pathlib.Path(tmp) / "clip.wav")
        sf.write(str(target), clip, rate, subtype="FLOAT")
        if args.write:
            print(f"wrote {target}")
            return 0

        name = args.name or args.media.stem
        boundary = uuid.uuid4().hex
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{name}.wav"\r\n'
            "Content-Type: audio/wav\r\n\r\n"
        ).encode() + target.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        request = urllib.request.Request(
            f"{args.url}/api/voices?name={urllib.parse.quote(name)}",
            data=body, method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                json.load(response)
        except Exception as error:  # noqa: BLE001
            raise SystemExit(f"upload to {args.url} failed — is the server running?\n  {error}")
    print(f"uploaded as {name!r}; it compiles in the Voices drawer")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
