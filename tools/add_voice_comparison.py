#!/usr/bin/env python3
"""Append a two-panel, sequential voice comparison to an existing demo.

Pass recordings of the same words. Audio receives edge trimming and matched
loudness only; pitch, speed and the pauses inside each recording are retained.
The input video is preserved. The output may not overwrite any input.

    uv run tools/add_voice_comparison.py --video demo-base.mp4 \
        --real original.wav --generated generated.wav \
        --text "The same words in both recordings." --out demo.mp4
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import subprocess
import tempfile

import imageio_ffmpeg
import numpy as np
import pyloudnorm
import soundfile as sf
from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT, FPS, RATE = 1280, 800, 25, 24000
BG = "#0c0f11"
PANEL = "#111619"
TEXT = "#e8edf0"
MUTED = "#8f9da4"
ACCENT = "#5ad1c8"
LINE = "#2b363b"


def font(size, bold=False):
    candidates = [
        pathlib.Path("/System/Library/Fonts/Supplemental") / ("Arial Bold.ttf" if bold else "Arial.ttf"),
        pathlib.Path("C:/Windows/Fonts") / ("arialbd.ttf" if bold else "arial.ttf"),
        pathlib.Path("/usr/share/fonts/truetype/dejavu") / ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"),
    ]
    for path in candidates:
        if path.exists():
            return ImageFont.truetype(str(path), size)
    raise RuntimeError("No supported font found; install Arial or DejaVu Sans")


def prepare(path, target, ffmpeg):
    subprocess.run([ffmpeg, "-y", "-v", "error", "-i", str(path),
                    "-ac", "1", "-ar", str(RATE), str(target)], check=True)
    data, _ = sf.read(str(target), dtype="float32")
    loud = np.flatnonzero(np.abs(data) > 10 ** (-55 / 20))
    if not loud.size:
        raise ValueError(f"No audible speech in {path}")
    margin = int(0.05 * RATE)
    data = data[max(0, loud[0] - margin):min(len(data), loud[-1] + margin)]
    # Match the level of the existing demo's speech, without changing prosody.
    measured = pyloudnorm.Meter(RATE).integrated_loudness(data)
    if not np.isfinite(measured):
        raise ValueError(f"Cannot measure speech loudness in {path}")
    return data * 10 ** ((-26.6 - measured) / 20)


def envelope(data, buckets=110):
    peaks = np.array([np.max(np.abs(part)) if part.size else 0
                      for part in np.array_split(data, buckets)])
    return (peaks / max(float(peaks.max()), 1e-9)) ** 0.7


def centered(draw, text, y, face, fill=TEXT):
    draw.text((WIDTH / 2, y), text, font=face, fill=fill, anchor="mt")


def base_frame(text):
    image = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(image)
    draw.text((72, 49), "M Y N A H", font=font(23, True), fill=ACCENT)
    draw.line((72, 96, 1208, 96), fill=LINE)
    draw.text((72, 127), "VOICE COMPARISON", font=font(14, True), fill=MUTED)
    draw.text((70, 163), "Real voice. Generated voice.", font=font(46, True), fill=TEXT)
    draw.text((72, 226), "Same words, played back to back.", font=font(22), fill=MUTED)
    for index, (label, detail) in enumerate([
        ("My real voice", "Original recording"),
        ("Generated voice", "Cloned locally with mynah"),
    ]):
        x = 72 + index * 582
        draw.rounded_rectangle((x, 292, x + 554, 569), radius=18, fill=PANEL, outline=LINE, width=2)
        draw.text((x + 28, 323), label, font=font(29, True), fill=TEXT)
        draw.text((x + 28, 365), detail, font=font(17), fill=MUTED)
    # Wrap the shared transcript to the available width.
    face = font(25)
    lines, line = [], ""
    for word in text.split():
        candidate = f"{line} {word}".strip()
        if draw.textlength(candidate, font=face) > 1040:
            lines.append(line)
            line = word
        else:
            line = candidate
    if line:
        lines.append(line)
    if len(lines) > 3:
        raise ValueError("Use a shorter phrase for the comparison")
    for index, line in enumerate(lines):
        centered(draw, line, 619 + index * 35, face)
    centered(draw, "github.com/seena18/mynah", 754, font(17), MUTED)
    return image


def append_comparison(video, real, generated, text, output):
    for path in (video, real, generated):
        if path.resolve() == output.resolve():
            raise ValueError("Output must differ from every input; preserve the base demo")
    output.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    with tempfile.TemporaryDirectory(prefix="mynah-compare-", dir=output.parent) as tmp:
        work = pathlib.Path(tmp)
        clips = [prepare(path, work / f"audio{i}.wav", ffmpeg)
                 for i, path in enumerate((real, generated))]
        peak = max(float(np.max(np.abs(data))) for data in clips)
        if peak > 0.98:
            clips = [data * (0.98 / peak) for data in clips]
        durations = [len(data) / RATE for data in clips]
        starts = [1.25, 1.25 + durations[0] + 0.85]
        frames = math.ceil((starts[1] + durations[1] + 2.0) * FPS)
        duration = frames / FPS
        mix = np.zeros(round(duration * RATE), dtype="float32")
        for start, data in zip(starts, clips):
            offset = round(start * RATE)
            mix[offset:offset + len(data)] = data
        audio = work / "comparison.wav"
        sf.write(str(audio), mix, RATE)
        peaks = [envelope(data) for data in clips]
        base = base_frame(text)
        outro = work / "outro.mp4"
        command = [ffmpeg, "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
                   "-s", f"{WIDTH}x{HEIGHT}", "-r", str(FPS), "-i", "pipe:0", "-i", str(audio),
                   "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
                   "-c:a", "aac", "-b:a", "160k", "-t", str(duration), str(outro)]
        process = subprocess.Popen(command, stdin=subprocess.PIPE)
        face = font(14, True)
        clock_face = font(15)
        try:
            for frame in range(frames):
                t = frame / FPS
                image = base.copy()
                draw = ImageDraw.Draw(image)
                for index, (start, seconds) in enumerate(zip(starts, durations)):
                    x = 72 + index * 582
                    progress = min(1, max(0, (t - start) / seconds))
                    active = start <= t < start + seconds
                    label = "PLAYING" if active else "HEARD" if t >= start + seconds else "UP NEXT"
                    color = ACCENT if active else MUTED
                    if active:
                        draw.rounded_rectangle((x, 292, x + 554, 569), radius=18, outline=ACCENT, width=2)
                    draw.text((x + 526, 328), label, font=face, fill=color, anchor="rt")
                    for bar, value in enumerate(peaks[index]):
                        at = x + 28 + bar * 4.5
                        tall = max(3, float(value) * 68)
                        fill = ACCENT if (bar + 0.5) / len(peaks[index]) <= progress else "#3c4b52"
                        draw.rounded_rectangle((at, 455 - tall / 2, at + 2.5, 455 + tall / 2), radius=1, fill=fill)
                    draw.text((x + 28, 522), "A" if index == 0 else "B", font=face, fill=color)
                    draw.text((x + 526, 520), f"{min(seconds, max(0, t-start)):.1f} / {seconds:.1f} s",
                              font=clock_face, fill=MUTED, anchor="rt")
                if frame < 8:
                    image = Image.blend(Image.new("RGB", image.size, BG), image, frame / 8)
                process.stdin.write(image.tobytes())
            process.stdin.close()
            if process.wait() != 0:
                raise RuntimeError("Could not encode comparison")
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait()
        # Re-encode the join to keep timestamps, frame rate and audio continuous.
        final = work / "final.mp4"
        subprocess.run([
            ffmpeg, "-y", "-v", "error", "-i", str(video), "-i", str(outro),
            "-filter_complex",
            "[0:v]fps=25,setsar=1[v0];[1:v]setsar=1[v1];"
            "[v0][0:a][v1][1:a]concat=n=2:v=1:a=1[v][a]",
            "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "medium",
            "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
            "-ar", str(RATE), "-ac", "1", "-movflags", "+faststart", str(final),
        ], check=True)
        final.replace(output)
    return {"outro_seconds": duration, "real_seconds": durations[0],
            "generated_seconds": durations[1], "starts": starts, "output": str(output)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", type=pathlib.Path, required=True)
    parser.add_argument("--real", type=pathlib.Path, required=True)
    parser.add_argument("--generated", type=pathlib.Path, required=True)
    parser.add_argument("--text", required=True)
    parser.add_argument("--out", type=pathlib.Path, required=True)
    args = parser.parse_args()
    print(json.dumps(append_comparison(args.video, args.real, args.generated, args.text, args.out), indent=2))


if __name__ == "__main__":
    main()
