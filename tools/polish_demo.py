#!/usr/bin/env python3
"""Build a short 4:5 social cut from the real mynah demo capture.

The app footage and every voice remain the original recordings. This script
only changes presentation: it trims idle beats, frames the landscape app in a
LinkedIn-friendly canvas, adds readable chapter labels, and normalizes volume.

    uv run tools/polish_demo.py
"""

from __future__ import annotations

import argparse
import math
import pathlib
import re
import subprocess
import sys
import tempfile

import imageio_ffmpeg
import numpy as np
import pyloudnorm
import soundfile as sf
from PIL import Image, ImageDraw, ImageFont

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from mynah import store  # noqa: E402


DOCS = ROOT / "docs"
WIDTH, HEIGHT, FPS, RATE = 1080, 1350, 25, 24000

BG = "#090c0e"
PANEL = "#111719"
PANEL_2 = "#151d20"
TEXT = "#edf3f4"
MUTED = "#91a0a6"
ACCENT = "#5ad1c8"
LINE = "#263337"
CORAL = "#ff786b"


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = [
        pathlib.Path("/System/Library/Fonts/Supplemental") /
        ("Arial Bold.ttf" if bold else "Arial.ttf"),
        pathlib.Path("C:/Windows/Fonts") / ("arialbd.ttf" if bold else "arial.ttf"),
        pathlib.Path("/usr/share/fonts/truetype/dejavu") /
        ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return ImageFont.truetype(str(candidate), size)
    raise RuntimeError("Arial or DejaVu Sans is required")


def ffmpeg() -> str:
    return imageio_ffmpeg.get_ffmpeg_exe()


def wrap(draw: ImageDraw.ImageDraw, text: str, face, width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if current and draw.textlength(candidate, font=face) > width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def text_block(draw: ImageDraw.ImageDraw, text: str, xy: tuple[int, int], face,
               fill: str, width: int, gap: int = 8) -> int:
    x, y = xy
    bbox = draw.textbbox((0, 0), "Ag", font=face)
    line_height = bbox[3] - bbox[1]
    for line in wrap(draw, text, face, width):
        draw.text((x, y), line, font=face, fill=fill)
        y += line_height + gap
    return y


def brand(draw: ImageDraw.ImageDraw) -> None:
    draw.text((58, 50), "M Y N A H", font=font(24, True), fill=ACCENT)
    draw.text((1022, 54), "github.com/seena18/mynah", font=font(16),
              fill=MUTED, anchor="ra")
    draw.line((58, 94, 1022, 94), fill=LINE, width=2)


def stage_card(eyebrow: str, title: str, transcript: str = "") -> Image.Image:
    image = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(image)
    draw.text((28, 38), eyebrow, font=font(16, True), fill=ACCENT)
    text_block(draw, title, (27, 72), font(39, True), TEXT, 1025, 4)

    # Keep the product itself dominant. The surrounding type explains the
    # action without dressing the capture up as a second fake interface.
    draw.rounded_rectangle((22, 144, 1058, 796), radius=12, fill="#050708",
                           outline=LINE, width=2)

    draw.line((58, 838, 1022, 838), fill=LINE, width=2)
    if transcript:
        draw.rectangle((58, 880, 62, 1240), fill=ACCENT)
        text_block(draw, f'“{transcript}”', (86, 880), font(25), TEXT, 906, 8)
    return image


def prepare_voice(path: pathlib.Path, target: pathlib.Path) -> np.ndarray:
    subprocess.run([ffmpeg(), "-y", "-v", "error", "-i", str(path),
                    "-ac", "1", "-ar", str(RATE), str(target)], check=True)
    data, _ = sf.read(str(target), dtype="float32")
    voiced = np.flatnonzero(np.abs(data) > 10 ** (-55 / 20))
    if not voiced.size:
        raise ValueError(f"No speech found in {path}")
    margin = round(0.05 * RATE)
    data = data[max(0, voiced[0] - margin):min(len(data), voiced[-1] + margin)]
    measured = pyloudnorm.Meter(RATE).integrated_loudness(data)
    data *= 10 ** ((-20.0 - measured) / 20)
    peak = float(np.max(np.abs(data)))
    if peak > 0.96:
        data *= 0.96 / peak
    return data.astype("float32")


def envelope(data: np.ndarray, buckets: int = 150) -> np.ndarray:
    peaks = np.array([np.max(np.abs(chunk)) if chunk.size else 0
                      for chunk in np.array_split(data, buckets)])
    return (peaks / max(float(peaks.max()), 1e-9)) ** 0.72


def join_voiceover(paths: list[pathlib.Path], target: pathlib.Path,
                   gap: float = 0.18) -> tuple[list[float], pathlib.Path]:
    clips, durations = [], []
    for path in paths:
        clip, rate = sf.read(str(path), dtype="float32")
        if rate != RATE:
            raise ValueError(f"Narration must be {RATE} Hz: {path}")
        clips.append(clip)
        durations.append(len(clip) / RATE)
    silence = np.zeros(round(gap * RATE), dtype="float32")
    joined: list[np.ndarray] = []
    for index, clip in enumerate(clips):
        if index:
            joined.append(silence)
        joined.append(clip)
    sf.write(str(target), np.concatenate(joined), RATE)
    return durations, target


def intro_base() -> Image.Image:
    image = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(image)
    brand(draw)
    draw.text((58, 176), "Two voices. Same words.", font=font(20), fill=MUTED)
    draw.text((56, 220), "Can you tell which one is real?", font=font(49, True), fill=TEXT)
    return image


def make_intro(real: pathlib.Path, generated: pathlib.Path, work: pathlib.Path,
               thumbnail: pathlib.Path) -> tuple[pathlib.Path, float, list[tuple[float, float, str]]]:
    clips = [prepare_voice(path, work / f"voice-{index}.wav")
             for index, path in enumerate((real, generated))]
    durations = [len(data) / RATE for data in clips]
    starts = [1.20, 1.20 + durations[0] + 0.50]
    duration = starts[1] + durations[1] + 0.55
    frames = math.ceil(duration * FPS)
    duration = frames / FPS

    mix = np.zeros(round(duration * RATE), dtype="float32")
    for start, clip in zip(starts, clips):
        offset = round(start * RATE)
        mix[offset:offset + len(clip)] += clip
    audio = work / "intro.wav"
    sf.write(str(audio), mix, RATE)

    waves = [envelope(clip) for clip in clips]
    base = intro_base()
    preview = base.copy()
    preview_draw = ImageDraw.Draw(preview)
    preview_draw.line((58, 613, 1022, 613), fill=LINE, width=2)
    for bar, value in enumerate(waves[0]):
        x = 92 + bar * 5.95
        tall = max(4, float(value) * 105)
        preview_draw.rounded_rectangle((x, 612 - tall / 2, x + 3.5,
                                        612 + tall / 2), radius=2, fill="#344247")
    preview_draw.text((540, 935),
                      "“Testing one, two, three. I’m saying a few words right now.”",
                      font=font(25), fill=TEXT, anchor="ma")
    thumbnail.parent.mkdir(parents=True, exist_ok=True)
    preview.save(thumbnail)

    video = work / "00-intro.mp4"
    command = [ffmpeg(), "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
               "-s", f"{WIDTH}x{HEIGHT}", "-r", str(FPS), "-i", "pipe:0",
               "-i", str(audio), "-c:v", "libx264", "-preset", "medium", "-crf", "18",
               "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-ar", str(RATE),
               "-ac", "1", "-t", f"{duration:.3f}", str(video)]
    process = subprocess.Popen(command, stdin=subprocess.PIPE)
    try:
        for frame in range(frames):
            t = frame / FPS
            image = base.copy()
            draw = ImageDraw.Draw(image)
            current = 0 if t < starts[1] else 1
            active = starts[current] <= t < starts[current] + durations[current]
            amount = min(1.0, max(0.0, (t - starts[current]) / durations[current]))
            draw.line((58, 613, 1022, 613), fill=ACCENT if active else LINE,
                      width=3 if active else 2)
            for bar, value in enumerate(waves[current]):
                x = 92 + bar * 5.95
                tall = max(4, float(value) * 105)
                color = ACCENT if (bar + 0.5) / len(waves[current]) <= amount else "#344247"
                draw.rounded_rectangle((x, 612 - tall / 2, x + 3.5,
                                        612 + tall / 2), radius=2, fill=color)
            draw.text((540, 935),
                      "“Testing one, two, three. I’m saying a few words right now.”",
                      font=font(25), fill=TEXT, anchor="ma")
            if frame < 8:
                image = Image.blend(Image.new("RGB", image.size, BG), image, frame / 8)
            process.stdin.write(image.tobytes())
        process.stdin.close()
        if process.wait() != 0:
            raise RuntimeError("Could not encode introduction")
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait()
    captions = [
        (starts[0], starts[0] + durations[0],
         "Testing one, two, three. I'm saying a few words right now."),
        (starts[1], starts[1] + durations[1],
         "Testing one, two, three. I'm saying a few words right now."),
    ]
    return video, duration, captions


def narration_card() -> Image.Image:
    image = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(image)
    brand(draw)
    draw.text((58, 174), "Okay.", font=font(23, True), fill=CORAL)
    draw.text((56, 225), "That was obvious.", font=font(66, True), fill=TEXT)
    draw.line((58, 355, 1022, 355), fill=LINE, width=2)
    draw.text((58, 435), "The first recording was real.", font=font(31), fill=MUTED)
    draw.text((58, 500), "The second voice was generated locally.",
              font=font(31, True), fill=ACCENT)
    draw.text((58, 664), "15 seconds of reference speech.", font=font(25), fill=TEXT)
    draw.text((58, 715), "Rendered on a six-year-old MacBook.", font=font(25), fill=TEXT)
    draw.line((58, 852, 220, 852), fill=ACCENT, width=5)
    draw.text((58, 916), "Still pretty cool.", font=font(48, True), fill=TEXT)
    draw.text((58, 998), "The cloned voice narrates from here.",
              font=font(26), fill=MUTED)
    return image


def local_card() -> Image.Image:
    image = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(image)
    brand(draw)
    draw.text((58, 174), "And one more thing.", font=font(23), fill=ACCENT)
    text_block(draw, "This entire video is narrated by the clone.",
               (56, 240), font(58, True), TEXT, 950, 8)
    draw.line((58, 570, 1022, 570), fill=LINE, width=2)
    draw.text((58, 660), "Generated with Mynah", font=font(31), fill=MUTED)
    draw.text((58, 724), "on this M1 MacBook Pro.", font=font(38, True), fill=TEXT)
    draw.text((58, 900), "The app and model both ran locally.",
              font=font(27), fill=ACCENT)
    return image


def make_static_narrated(image: Image.Image, narration: pathlib.Path,
                         work: pathlib.Path, name: str,
                         minimum: float = 0.0) -> tuple[pathlib.Path, float]:
    seconds = sf.info(str(narration)).duration
    duration = max(minimum, seconds + 0.55)
    still = work / f"{name}.png"
    output = work / f"{name}.mp4"
    image.save(still)
    subprocess.run([
        ffmpeg(), "-y", "-v", "error", "-loop", "1", "-framerate", str(FPS),
        "-i", str(still), "-i", str(narration), "-filter_complex",
        f"[1:a]adelay=250|250,apad,atrim=0:{duration:.3f}[a]",
        "-map", "0:v", "-map", "[a]", "-t", f"{duration:.3f}", "-r", str(FPS),
        "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k", "-ar", str(RATE), "-ac", "1",
        "-movflags", "+faststart", str(output),
    ], check=True)
    return output, duration


def make_stage(source: pathlib.Path, work: pathlib.Path, index: int, start: float,
               end: float, duration: float, card: Image.Image,
               narration: pathlib.Path | None = None,
               keep_source_audio: bool = False) -> tuple[pathlib.Path, float]:
    speed = (end - start) / duration
    still = work / f"card-{index:02d}.png"
    card.save(still)
    output = work / f"{index:02d}-stage.mp4"
    command = [ffmpeg(), "-y", "-v", "error", "-ss", f"{start:.3f}",
               "-t", f"{end - start:.3f}", "-i", str(source),
               "-loop", "1", "-framerate", str(FPS), "-i", str(still)]
    if narration is not None:
        command += ["-i", str(narration)]
    elif not keep_source_audio:
        command += ["-f", "lavfi", "-t", f"{duration:.3f}",
                    "-i", f"anullsrc=r={RATE}:cl=mono"]
    app_pts = "PTS-STARTPTS" if speed == 1 else f"(PTS-STARTPTS)/{speed:.6f}"
    filters = (f"[0:v]setpts={app_pts},scale=1024:640:flags=lanczos[app];"
               "[1:v][app]overlay=28:150:shortest=1[v]")
    audio_map = "0:a"
    if narration is not None:
        filters += f";[2:a]adelay=250|250,apad,atrim=0:{duration:.3f}[a]"
        audio_map = "[a]"
    elif not keep_source_audio:
        audio_map = "2:a"
    command += ["-filter_complex", filters, "-map", "[v]",
                "-map", audio_map, "-t", f"{duration:.3f}",
                "-r", str(FPS), "-c:v", "libx264", "-preset", "medium", "-crf", "18",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
                "-ar", str(RATE), "-ac", "1", "-movflags", "+faststart", str(output)]
    subprocess.run(command, check=True)
    return output, duration


def make_end(work: pathlib.Path, narration: pathlib.Path) -> tuple[pathlib.Path, float]:
    image = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(image)
    brand(draw)
    draw.text((58, 245), "Mynah", font=font(92, True), fill=TEXT)
    draw.text((58, 375), "Open-source voice cloning", font=font(35), fill=MUTED)
    draw.text((58, 425), "and narration sandbox.", font=font(35), fill=MUTED)
    draw.line((58, 565, 1022, 565), fill=LINE, width=2)
    draw.text((58, 655), "Runs on your machine.", font=font(29), fill=TEXT)
    draw.text((58, 710), "No account. No API key. No cloud upload.",
              font=font(29), fill=TEXT)
    draw.text((58, 874), "github.com/seena18/mynah", font=font(38, True), fill=ACCENT)
    draw.line((58, 932, 620, 932), fill=ACCENT, width=3)
    draw.text((58, 1080), "Use voices you own or have permission to use.",
              font=font(21), fill=MUTED)
    draw.text((58, 1130), "Generated audio includes the Chatterbox Perth watermark.",
              font=font(18), fill=MUTED)
    return make_static_narrated(image, narration, work, "99-end", minimum=5.0)


def timestamp(seconds: float) -> str:
    millis = round(seconds * 1000)
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def write_srt(path: pathlib.Path, captions: list[tuple[float, float, str]]) -> None:
    blocks = []
    for index, (start, end, words) in enumerate(captions, 1):
        blocks.append(f"{index}\n{timestamp(start)} --> {timestamp(end)}\n{words}\n")
    path.write_text("\n".join(blocks), encoding="utf-8")


def display_text(text: str) -> str:
    """Turn TTS-oriented spelling into clean copy for cards and captions."""
    text = re.sub(r"(?i)\b(?:my-nuh|mynah)\b", "Mynah", text)
    text = re.sub(r"(?i)macbook pro", "MacBook Pro", text)
    text = re.sub(r"\b6 year old\b", "six-year-old", text)
    text = re.sub(r"(?<=[.!?])(?=[A-Z])", " ", text)
    return text


def add_spoken_captions(captions: list[tuple[float, float, str]], start: float,
                        duration: float, text: str, max_chars: int = 72) -> None:
    """Split one generated take into short captions, timed by word count."""
    units = [part.strip() for part in re.split(r"(?<=[.!?])\s+|(?<=,)\s+", display_text(text))
             if part.strip()]
    lines: list[str] = []
    current = ""
    for unit in units:
        candidate = f"{current} {unit}".strip()
        if current and len(candidate) > max_chars:
            lines.append(current)
            current = unit
        else:
            current = candidate
    if current:
        lines.append(current)
    # A long punctuation-free unit still needs to fit a social caption.
    fitted: list[str] = []
    for line in lines:
        words, current = line.split(), ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if current and len(candidate) > max_chars:
                fitted.append(current)
                current = word
            else:
                current = candidate
        if current:
            fitted.append(current)
    weights = [max(1, len(line.split())) for line in fitted]
    total = sum(weights)
    elapsed = start
    for index, (line, weight) in enumerate(zip(fitted, weights)):
        end = start + duration if index == len(fitted) - 1 else elapsed + duration * weight / total
        captions.append((elapsed, end, line))
        elapsed = end


def project_narration(project_id: str) -> tuple[list[str], list[pathlib.Path]]:
    project = store.Project.load(project_id)
    if not project.chunks:
        raise SystemExit(f"Project {project_id} has no narration chunks")
    missing = [chunk.text for chunk in project.chunks if project.status(chunk) != "ready"]
    if missing:
        raise SystemExit(f"Project {project_id} has {len(missing)} chunk(s) that are not ready")
    return ([chunk.text for chunk in project.chunks],
            [project.take_path(chunk) for chunk in project.chunks])


def build(args) -> None:
    narration_text, narration = project_narration(args.project)
    if len(narration) != 8:
        raise SystemExit(f"This edit expects 8 narration chunks; project has {len(narration)}")
    for source in (args.video, args.real, args.generated, *narration):
        if not source.exists():
            raise SystemExit(f"Missing source: {source}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="mynah-social-", dir=args.output.parent) as tmp:
        work = pathlib.Path(tmp)
        intro, intro_duration, captions = make_intro(
            args.real, args.generated, work, args.thumbnail)
        pieces = [intro]
        elapsed = intro_duration

        reveal, reveal_duration = make_static_narrated(
            narration_card(), narration[0], work, "01-reveal")
        pieces.append(reveal)
        add_spoken_captions(captions, elapsed + 0.25,
                            sf.info(str(narration[0])).duration, narration_text[0])
        elapsed += reveal_duration

        local, local_duration = make_static_narrated(
            local_card(), narration[1], work, "02-local")
        pieces.append(local)
        add_spoken_captions(captions, elapsed + 0.25,
                            sf.info(str(narration[1])).duration, narration_text[1])
        elapsed += local_duration

        stages = [
            # source start/end, target seconds, narration index, source audio,
            # eyebrow, headline, footer, on-screen transcript
            (2.8, 29.0, 11.85, 2, False, "01  Capture",
             "Record it. Compile it once.", "Five clean seconds is enough to start",
             display_text(narration_text[2])),
            (30.8, 38.6, 4.45, 3, False, "02  Write",
             "Paste a script", "Blank lines become independent, editable takes",
             display_text(narration_text[3])),
            (38.6, 42.0, 4.15, 4, False, "03  Generate",
             "Generate line by line", "Each take is rendered and cached independently",
             display_text(narration_text[4])),
            (42.0, 45.0, 3.40, 5, False, "04  Preview",
             "Preview in the browser", "The stitched result plays without a download",
             display_text(narration_text[5])),
            (45.0, 51.2, 6.20, None, True, "04  Preview",
             "Hear the complete mix", "Preview plays directly in the browser",
             "This runs entirely on your own machine. No account, no API key, and nothing is uploaded."),
            (51.3, 61.3, 6.90, 6, False, "05  Iterate",
             "Fix one line", "Only the changed take becomes stale and regenerates",
             display_text(narration_text[6])),
            (61.8, 69.0, 7.20, None, True, "06  Export",
             "Hear the corrected mix", "Every untouched take stays cached",
             "This runs entirely on your own machine. No account, no API key, and not one byte leaves the room."),
        ]
        for index, (start, end, duration, narration_index, source_audio,
                    eyebrow, title, _footer, transcript) in enumerate(stages, 2):
            voiceover = narration[narration_index] if narration_index is not None else None
            piece, duration = make_stage(
                args.video, work, index, start, end, duration,
                stage_card(eyebrow, title, transcript),
                narration=voiceover, keep_source_audio=source_audio)
            pieces.append(piece)
            if narration_index is not None:
                spoken = sf.info(str(voiceover)).duration
                add_spoken_captions(captions, elapsed + 0.25, spoken,
                                    narration_text[narration_index])
            if source_audio and start == 45.0:
                captions += [
                    (elapsed + 0.00, elapsed + 1.90,
                     "This runs entirely on your own machine."),
                    (elapsed + 2.39, elapsed + 5.23,
                     "No account, no API key, and nothing is uploaded."),
                ]
            if source_audio and start == 61.8:
                captions += [
                    (elapsed + 0.04, elapsed + 1.94,
                     "This runs entirely on your own machine."),
                    (elapsed + 2.43, elapsed + 6.40,
                     "No account, no API key, and not one byte leaves the room."),
                ]
            elapsed += duration
        ending, end_duration = make_end(work, narration[7])
        pieces.append(ending)
        add_spoken_captions(captions, elapsed + 0.25,
                            sf.info(str(narration[7])).duration, narration_text[7])
        elapsed += end_duration

        listing = work / "concat.txt"
        listing.write_text("".join(f"file '{piece.name}'\n" for piece in pieces))
        joined = work / "joined.mp4"
        subprocess.run([ffmpeg(), "-y", "-v", "error", "-f", "concat", "-safe", "0",
                        "-i", str(listing), "-c", "copy", str(joined)], check=True)
        subprocess.run([ffmpeg(), "-y", "-v", "error", "-i", str(joined),
                        "-map", "0:v", "-map", "0:a", "-c:v", "copy",
                        "-af", "loudnorm=I=-18:TP=-1.5:LRA=11", "-c:a", "aac",
                        "-b:a", "160k", "-ar", str(RATE), "-ac", "1",
                        "-movflags", "+faststart", str(args.output)], check=True)
        write_srt(args.captions, sorted(captions))
    print(f"wrote {args.output} ({args.output.stat().st_size / 1e6:.1f} MB, {elapsed:.1f}s)")
    print(f"wrote {args.thumbnail}")
    print(f"wrote {args.captions}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", type=pathlib.Path,
                        default=DOCS / "raw" / "comparison" / "demo-base.mp4")
    parser.add_argument("--real", type=pathlib.Path,
                        default=DOCS / "raw" / "comparison" / "original.wav")
    parser.add_argument("--generated", type=pathlib.Path,
                        default=DOCS / "raw" / "comparison" / "generated.wav")
    parser.add_argument("--output", type=pathlib.Path,
                        default=DOCS / "mynah-linkedin.mp4")
    parser.add_argument("--thumbnail", type=pathlib.Path,
                        default=DOCS / "mynah-linkedin-thumbnail.png")
    parser.add_argument("--captions", type=pathlib.Path,
                        default=DOCS / "mynah-linkedin.srt")
    parser.add_argument("--project", default="4ae3e33177",
                        help="Project id containing the eight ready narration chunks")
    args = parser.parse_args()
    build(args)


if __name__ == "__main__":
    main()
