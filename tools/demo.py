#!/usr/bin/env python3
"""Record a stylized demo of mynah by driving the real app with Playwright.

Nothing here is faked: it clicks through a live server, waits on real
generation, and the audio in the finished video is the WAV that run actually
exported. What is added is presentation — a visible cursor (headless Chromium
draws none), captions, and a timelapse over the stretches where the machine is
just thinking.

    uv run --group dev tools/demo.py --url http://localhost:8765

Outputs docs/demo.mp4 (with audio) and docs/demo.gif (a short silent excerpt
of the part worth putting in a README).
"""

from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import subprocess
import sys
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"

# Two short lines: long enough to hear the voice, short enough that the whole
# demo stays under a minute.
SCRIPT = ("This runs entirely on your own machine.\n\n"
          "No account, no API key, and nothing is uploaded.")
EDITED = "No account, no API key, and not one byte leaves the room."

# Seconds each timelapsed stretch of waiting is compressed to.
TIMELAPSE_TARGET = 4.5


# ---- the overlay ---------------------------------------------------------
# Injected into the page rather than composited afterwards, so it is captured
# by Playwright's own recorder and lands in the video for free.

OVERLAY_JS = r"""
window.__demo = {
  init() {
    const css = document.createElement('style');
    css.textContent = `
      #__cur { position: fixed; z-index: 99999; left: 0; top: 0; pointer-events: none;
               transition: transform 620ms cubic-bezier(.4,0,.2,1); }
      #__rip { position: fixed; z-index: 99998; pointer-events: none; border-radius: 50%;
               border: 2px solid #5ad1c8; opacity: 0; }
      #__cap { position: fixed; z-index: 99997; left: 50%; bottom: 84px; transform: translateX(-50%);
               background: rgba(12,15,17,.92); border: 1px solid #2f3a42; border-radius: 999px;
               color: #e8edf0; font: 15px/1 -apple-system, "Segoe UI", Roboto, sans-serif;
               padding: 11px 20px; opacity: 0; transition: opacity 320ms;
               box-shadow: 0 8px 40px rgba(0,0,0,.5); white-space: nowrap; }
      #__cap.on { opacity: 1; }
      @keyframes __ripple { from { transform: scale(.3); opacity: .9; } to { transform: scale(1); opacity: 0; } }
    `;
    document.head.append(css);
    const cur = document.createElement('div');
    cur.id = '__cur';
    cur.innerHTML = `<svg width="22" height="22" viewBox="0 0 22 22">
      <path d="M3 2 L3 17 L7.4 13 L10 19 L13 17.6 L10.5 11.6 L16 11.6 Z"
            fill="#ffffff" stroke="#0c0f11" stroke-width="1.3" stroke-linejoin="round"/></svg>`;
    cur.style.transform = 'translate(640px, 700px)';
    const cap = document.createElement('div');
    cap.id = '__cap';
    document.body.append(cur, cap);
    this.cur = cur; this.cap = cap;
  },
  moveTo(x, y) { this.cur.style.transform = `translate(${x - 3}px, ${y - 2}px)`; },
  ripple(x, y) {
    const r = document.createElement('div');
    r.id = '__rip';
    const size = 46;
    r.style.cssText += `left:${x - size / 2}px; top:${y - size / 2}px; width:${size}px; height:${size}px;
                        animation: __ripple 480ms ease-out forwards;`;
    document.body.append(r);
    setTimeout(() => r.remove(), 520);
  },
  caption(text) {
    if (!text) { this.cap.classList.remove('on'); return; }
    this.cap.textContent = text;
    this.cap.classList.add('on');
  },
};
"""


class Stage:
    """Drives the page, and records when each beat happened.

    Times are relative to context creation, which is when Playwright starts
    the recording — close enough to frame zero to place the audio by.
    """

    def __init__(self, page, t0: float):
        self.page = page
        self.t0 = t0
        self.marks: dict[str, float] = {}
        self.fast: list[tuple[float, float, float]] = []

    def at(self) -> float:
        return time.monotonic() - self.t0

    def mark(self, name: str) -> float:
        self.marks[name] = self.at()
        return self.marks[name]

    def timelapse(self, start: float) -> None:
        """Mark [start, now] as a stretch to speed up in the finished video.

        How much to speed it up is decided at build time from how long it
        actually took, so the demo runs the same length on a fast machine and
        a slow one.
        """
        self.fast.append((start, self.at()))

    def caption(self, text: str, hold: int = 0) -> None:
        self.page.evaluate("t => window.__demo.caption(t)", text)
        if hold:
            self.page.wait_for_timeout(hold)

    def point(self, selector: str, settle: int = 700) -> tuple[float, float]:
        box = self.page.locator(selector).first.bounding_box()
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        self.page.evaluate("([x, y]) => window.__demo.moveTo(x, y)", [x, y])
        self.page.wait_for_timeout(settle)
        return x, y

    def click(self, selector: str, settle: int = 700, after: int = 400) -> None:
        x, y = self.point(selector, settle)
        self.page.evaluate("([x, y]) => window.__demo.ripple(x, y)", [x, y])
        self.page.wait_for_timeout(160)
        self.page.locator(selector).first.click()
        self.page.wait_for_timeout(after)

    def type_into(self, selector: str, text: str, delay: int = 26) -> None:
        self.point(selector, 520)
        self.page.locator(selector).first.click()
        self.page.locator(selector).first.type(text, delay=delay)

    def wait_settled(self, timeout: float = 600.0) -> None:
        """Wait until no line is stale, queued or rendering."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            busy = self.page.evaluate(
                "() => [...document.querySelectorAll('.chunk')]"
                ".some(li => /s-(stale|queued|rendering)/.test(li.className))")
            if not busy:
                return
            self.page.wait_for_timeout(400)
        raise RuntimeError("generation did not finish in time")


def sound_seconds(path: pathlib.Path) -> float:
    import soundfile

    info = soundfile.info(str(path))
    return info.frames / float(info.samplerate)


def api(url: str, path: str, method: str = "GET", body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        f"{url}{path}", data=data, method=method,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.loads(response.read() or b"{}")


def clear_own_takes(url: str, pid: str) -> int:
    """Delete the takes this demo project produced, unless another project
    shares them.

    Takes are named by a hash of text + voice + parameters and are deliberately
    shared between projects, so a second run of the demo finds its own lines
    already rendered, nothing goes stale, and Generate stays disabled — the
    tool defeated by the cache it exists to show off. Removing only the takes
    no other project references makes every run generate for real without
    touching anyone else's work.
    """
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from mynah import store

    state = api(url, f"/api/state?p={pid}")
    mine = {c["fingerprint"] for c in state["project"]["chunks"] if c["text"].strip()}
    shared: set[str] = set()
    for project in state["projects"]:
        if project["id"] == pid:
            continue
        other = api(url, f"/api/state?p={project['id']}")
        shared |= {c["fingerprint"] for c in other["project"]["chunks"] if c["text"].strip()}
    removed = 0
    for fingerprint in mine - shared:
        take = store.TAKES / f"{fingerprint}.wav"
        if take.exists():
            take.unlink()
            removed += 1
    return removed


def run(url: str, out_dir: pathlib.Path, keep_project: bool) -> dict:
    from playwright.sync_api import sync_playwright

    state = api(url, "/api/state")
    ready = [v for v in state["voices"] if v["status"] == "ready"]
    if not ready:
        raise SystemExit("no compiled voice — record or upload one first")
    voice = ready[0]

    project = api(url, "/api/projects", "POST", {"title": "Demo"})["project"]
    pid = project["id"]
    api(url, f"/api/projects/{pid}", "POST", {"voice_id": voice["id"]})
    print(f"demo project {pid} using voice {voice['name']!r}")

    raw = out_dir / "raw"
    if raw.exists():
        shutil.rmtree(raw)
    raw.mkdir(parents=True)
    wav = raw / "demo.wav"

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=["--force-color-profile=srgb"])
        t0 = time.monotonic()
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            record_video_dir=str(raw),
            record_video_size={"width": 1280, "height": 800},
        )
        page = context.new_page()
        stage = Stage(page, t0)
        try:
            page.goto(f"{url}/?p={pid}", wait_until="networkidle")
            page.evaluate(OVERLAY_JS)
            page.evaluate("window.__demo.init()")
            page.wait_for_timeout(700)

            stage.caption("mynah — voice cloning TTS that runs on your machine", 2600)

            # --- voices ---
            stage.caption("Voices are cloned from a few seconds of speech")
            stage.click("#open-voices", after=900)
            page.wait_for_timeout(1800)
            stage.click("#voices-drawer [data-close]", after=700)

            # --- script ---
            stage.caption("Paste a script; blank lines split it into takes")
            stage.click("#paste-script", after=600)
            stage.type_into("#script", SCRIPT)
            page.wait_for_timeout(700)
            stage.click("#split", after=900)

            # --- generate ---
            # If a previous run already rendered these exact lines, nothing is
            # stale and Generate is disabled. Drop those takes and wait for the
            # poll to notice, so the demo always shows real work.
            if page.locator("#generate").is_disabled():
                freed = clear_own_takes(url, pid)
                print(f"cleared {freed} cached take(s) so the demo generates for real")
                page.wait_for_function(
                    "() => !document.getElementById('generate').disabled", timeout=30000)

            stage.caption("Each line is generated on its own")
            began = stage.at()
            stage.click("#generate", after=300)
            stage.wait_settled()
            stage.timelapse(began)
            page.wait_for_timeout(600)

            # --- preview: the part with sound ---
            stage.caption("Preview plays the stitched mix, in the browser")
            stage.click("#preview-open", after=200)
            page.wait_for_selector("#preview-active:not([hidden])", timeout=60000)
            # The app starts playing as soon as the browser says canplay. On a
            # loaded machine that stalls mid-clip, and the picture then runs
            # slower than the audio being muxed under it. Let it buffer fully,
            # then start from the top, so screen time and audio time agree.
            page.wait_for_function(
                "() => { const a = document.getElementById('preview-audio');"
                "        return a && a.readyState >= 4; }", timeout=120000)
            page.evaluate("() => { const a = document.getElementById('preview-audio');"
                          "        a.pause(); a.currentTime = 0; }")
            page.wait_for_timeout(120)
            stage.mark("preview_start")
            page.evaluate("() => document.getElementById('preview-audio').play()")
            page.wait_for_function(
                "() => { const a = document.getElementById('preview-audio');"
                "        return a && a.ended; }", timeout=120000)
            stage.mark("preview_end")
            # Grab the mix now, not at the end: the edit beat below changes
            # line 2, and the audio in the video has to be what was heard.
            with urllib.request.urlopen(
                    f"{url}/api/projects/{pid}/export.wav", timeout=300) as response:
                wav.write_bytes(response.read())
            on_screen = stage.marks["preview_end"] - stage.marks["preview_start"]
            heard = sound_seconds(wav)
            if heard and abs(on_screen - heard) / heard > 0.15:
                print(f"  warning: preview took {on_screen:.1f}s on screen for "
                      f"{heard:.1f}s of audio — the machine stalled playback, so "
                      f"the muxed audio will drift against the highlighting")
            page.wait_for_timeout(500)
            stage.click("#preview-close", after=400)

            # --- the point of the whole thing ---
            stage.caption("Change one line — only that line goes stale")
            rows = page.locator(".chunk textarea")
            stage.point(".chunk:nth-child(2) textarea", 620)
            rows.nth(1).click()
            page.keyboard.press("Meta+A")
            rows.nth(1).type(EDITED, delay=24)
            # Blur so the edit commits. Not a click on some other element:
            # #counts is empty when a project has no lines, and a zero-size
            # target would fail to click.
            page.evaluate("() => document.activeElement && document.activeElement.blur()")
            page.wait_for_timeout(1400)

            stage.caption("Regenerate just that one — the rest are untouched")
            began = stage.at()
            stage.click(".chunk:nth-child(2) .regen", after=300)
            stage.wait_settled()
            stage.timelapse(began)

            stage.caption("Export the finished mix", 1800)
            stage.caption("")
            page.wait_for_timeout(900)
            stage.mark("end")
        except BaseException:
            # A crash mid-run must not leave a stray project behind; the demo
            # created it, the demo takes it away. `finally` still does the
            # closing, so nothing is closed twice.
            if not keep_project:
                clear_own_takes(url, pid)
                api(url, f"/api/projects/{pid}", "DELETE")
                print(f"run failed — removed demo project {pid}")
            raise
        finally:
            context.close()
            browser.close()

    video = next(raw.glob("*.webm"))
    result = {"video": str(video), "wav": str(wav), "marks": stage.marks,
              "fast": stage.fast, "pid": pid}
    print(json.dumps({k: v for k, v in result.items() if k != "video"}, indent=2))

    if not keep_project:
        freed = clear_own_takes(url, pid)
        api(url, f"/api/projects/{pid}", "DELETE")
        print(f"deleted demo project {pid} and {freed} take(s) only it used")
    return result


# ---- post ----------------------------------------------------------------

def ffmpeg() -> str:
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def probe_duration(path: pathlib.Path) -> float:
    out = subprocess.run([ffmpeg(), "-i", str(path)], capture_output=True, text=True).stderr
    for line in out.splitlines():
        if "Duration:" in line:
            clock = line.split("Duration:")[1].split(",")[0].strip()
            h, m, s = clock.split(":")
            return int(h) * 3600 + int(m) * 60 + float(s)
    raise RuntimeError("could not read duration")


def build(result: dict, out_dir: pathlib.Path) -> pathlib.Path:
    """Speed up the thinking, drop the real audio in the right place.

    Each segment is encoded to its own file and the pieces are concatenated,
    rather than done as one filter_complex with several [0:v] trim branches:
    that form makes ffmpeg buffer every frame a later branch will need, which
    on a machine that is already swapping does not finish.
    """
    video = pathlib.Path(result["video"])
    duration = probe_duration(video)
    marks = result["marks"]
    work = out_dir / "raw"

    # Waiting is compressed to about TIMELAPSE_TARGET seconds however long it
    # really took, so the finished demo is the same length whether the model
    # took eight seconds a line or eighty.
    fast = []
    for window in sorted(result["fast"]):
        begin, finish = window[0], window[1]
        factor = max(2.0, (finish - begin) / TIMELAPSE_TARGET)
        fast.append((begin, finish, factor))

    # [(start, end, speed)] covering the whole timeline, in order.
    spans: list[tuple[float, float, float]] = []
    cursor = 0.0
    for begin, finish, factor in fast:
        begin, finish = max(0.0, begin), min(duration, finish)
        if begin > cursor:
            spans.append((cursor, begin, 1.0))
        spans.append((begin, finish, factor))
        cursor = finish
    if cursor < duration:
        spans.append((cursor, duration, 1.0))

    common = ["-c:v", "libx264", "-preset", "medium", "-crf", "21",
              "-pix_fmt", "yuv420p", "-an", "-r", "25"]
    pieces = []
    for index, (begin, finish, factor) in enumerate(spans):
        piece = work / f"seg{index:02d}.mp4"
        command = [ffmpeg(), "-y", "-v", "error", "-ss", f"{begin:.3f}",
                   "-to", f"{finish:.3f}", "-i", str(video)]
        if factor != 1.0:
            command += ["-vf", f"setpts=(PTS-STARTPTS)/{factor}"]
        else:
            command += ["-vf", "setpts=PTS-STARTPTS"]
        subprocess.run(command + common + [str(piece)], check=True)
        pieces.append(piece)
        print(f"  segment {index}: {begin:6.1f}-{finish:6.1f}s  x{factor:g}")

    listing = work / "concat.txt"
    listing.write_text("".join(f"file '{p.name}'\n" for p in pieces))

    # Every timelapse before the preview removes wall-clock time; the audio has
    # to slide back by exactly as much or it drifts off the picture.
    removed = sum((e - s) * (1 - 1 / f) for s, e, f in fast if e <= marks["preview_start"])
    delay = max(0.0, marks["preview_start"] - removed)

    mp4 = out_dir / "demo.mp4"
    subprocess.run([
        ffmpeg(), "-y", "-v", "error",
        "-f", "concat", "-safe", "0", "-i", str(listing),
        "-i", result["wav"],
        "-af", f"adelay={int(delay * 1000)}|{int(delay * 1000)}",
        # No -shortest: the audio is ~6s of speech placed 23s in, and it would
        # otherwise cut the video off there, losing every beat after the
        # preview — including the edit-one-line one, which is the point.
        "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart", str(mp4),
    ], check=True)
    print(f"wrote {mp4}  ({mp4.stat().st_size / 1e6:.1f} MB, audio at {delay:.1f}s)")

    # A README wants a short silent loop, not the whole thing.
    gif_start = max(0.0, delay - 1.0)
    gif_len = min(11.0, marks["preview_end"] - marks["preview_start"] + 2.0)
    palette = work / "palette.png"
    subprocess.run([ffmpeg(), "-y", "-v", "error", "-ss", f"{gif_start:.2f}", "-t", f"{gif_len:.2f}",
                    "-i", str(mp4), "-vf", "fps=12,scale=760:-1:flags=lanczos,palettegen=stats_mode=diff",
                    str(palette)], check=True)
    gif = out_dir / "demo.gif"
    subprocess.run([ffmpeg(), "-y", "-v", "error", "-ss", f"{gif_start:.2f}", "-t", f"{gif_len:.2f}",
                    "-i", str(mp4), "-i", str(palette), "-lavfi",
                    "fps=12,scale=760:-1:flags=lanczos[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=3",
                    str(gif)], check=True)
    print(f"wrote {gif}  ({gif.stat().st_size / 1e6:.1f} MB, {gif_len:.0f}s from {gif_start:.0f}s)")
    return mp4


def main() -> int:
    parser = argparse.ArgumentParser(description="record a demo of mynah")
    parser.add_argument("--url", default="http://localhost:8765")
    parser.add_argument("--out", type=pathlib.Path, default=DOCS)
    parser.add_argument("--keep-project", action="store_true",
                        help="leave the demo project behind for inspection")
    args = parser.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    try:
        api(args.url, "/api/state")
    except Exception as error:  # noqa: BLE001
        print(f"no server at {args.url} — start one with `python run.py`\n  {error}",
              file=sys.stderr)
        return 1

    result = run(args.url, args.out, args.keep_project)
    (args.out / "raw" / "marks.json").write_text(
        json.dumps({"marks": result["marks"], "fast": result["fast"]}, indent=2) + "\n")
    build(result, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
