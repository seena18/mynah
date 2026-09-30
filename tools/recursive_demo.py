#!/usr/bin/env python3
"""Capture and cut the recursive Mynah launch demo.

The narration comes from an editable Mynah project. Playwright drives the real
local app for the picture, while the final cut alternates between a clean wide
view and cursor/row-following close-ups.

    uv run --group dev tools/recursive_demo.py
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import shutil
import subprocess
import sys
import time
import urllib.request

import cv2
import imageio_ffmpeg
import numpy as np
import soundfile as sf
from PIL import Image, ImageDraw, ImageFont

ROOT = pathlib.Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
WIDTH, HEIGHT, FPS = 1080, 1350, 25
SOURCE_W, SOURCE_H = 1440, 900
BG = (9, 12, 14)
PROJECT_ID = "1461c2cfa8"
VOICE_ID = "3322e6e5a3"
# This leaves roughly 60 source pixels on either side of the 920px chunk
# column. A close shot can therefore follow a row without clipping its drag
# handle, text, playback controls, pause, or delete button.
DETAIL_ZOOM = 1.36


def api(base: str, path: str, method: str = "GET", body: dict | None = None) -> dict:
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=300) as response:
        return json.loads(response.read() or b"{}")


def ffmpeg() -> str:
    return imageio_ffmpeg.get_ffmpeg_exe()


def prepare_microphone(source: pathlib.Path, target: pathlib.Path) -> float:
    """Make the user's demo recording usable as Chromium's fake microphone."""
    subprocess.run([
        ffmpeg(), "-y", "-v", "error", "-i", str(source), "-ac", "1", "-ar", "48000",
        "-af", "volume=0.4", "-c:a", "pcm_s16le", str(target),
    ], check=True)
    return sf.info(target).duration


OVERLAY_JS = r"""
window.__demo = {
  init() {
    const css = document.createElement('style');
    css.textContent = `
      #__demo_cursor { position:fixed; left:0; top:0; z-index:99999; pointer-events:none;
        transition:transform 520ms cubic-bezier(.22,.8,.3,1); filter:drop-shadow(0 2px 2px #0008) }
      @keyframes demoRipple { from { transform:scale(.25); opacity:.9 }
        to { transform:scale(1); opacity:0 } }
      .__demo_ripple { position:fixed; z-index:99998; width:48px; height:48px;
        margin:-24px 0 0 -24px; border:2px solid #5ad1c8; border-radius:50%;
        pointer-events:none; animation:demoRipple 480ms ease-out forwards }
    `;
    document.head.append(css);
    const cursor = document.createElement('div');
    cursor.id = '__demo_cursor';
    cursor.innerHTML = `<svg width="25" height="25" viewBox="0 0 24 24">
      <path d="M3 2.5V19l4.8-4.2 3.1 6.3 3.2-1.6-3.1-6.1h6.5z"
        fill="#fff" stroke="#071012" stroke-width="1.4" stroke-linejoin="round"/></svg>`;
    cursor.style.transform = 'translate(700px,820px)';
    document.body.append(cursor); this.cursor = cursor;
  },
  move(x,y) { this.cursor.style.transform = `translate(${x-3}px,${y-2}px)` },
  ripple(x,y) {
    const ring=document.createElement('div'); ring.className='__demo_ripple';
    ring.style.left=x+'px'; ring.style.top=y+'px'; document.body.append(ring);
    setTimeout(()=>ring.remove(),520);
  }
};
"""


class Stage:
    def __init__(self, page, started: float):
        self.page = page
        self.started = started
        self.marks: dict[str, float] = {}
        self.camera: list[dict] = []

    def at(self) -> float:
        return time.monotonic() - self.started

    def mark(self, name: str) -> float:
        self.marks[name] = self.at()
        return self.marks[name]

    def box(self, selector: str) -> tuple[float, float]:
        box = self.page.locator(selector).first.bounding_box()
        if not box:
            raise RuntimeError(f"Nothing visible for {selector}")
        return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2

    def shot(self, mode: str, selector: str | None = None) -> None:
        if mode == "wide":
            x, y, zoom = SOURCE_W / 2, SOURCE_H / 2, 1.0
        else:
            x, y = self.box(selector or "body")
            zoom = DETAIL_ZOOM
        self.camera.append({"time": self.at(), "x": x, "y": y, "zoom": zoom})

    def point(self, selector: str, settle: int = 500, shot: str | None = None):
        x, y = self.box(selector)
        self.page.evaluate("([x,y])=>window.__demo.move(x,y)", [x, y])
        if shot:
            self.camera.append({"time": self.at(), "x": x, "y": y,
                                "zoom": DETAIL_ZOOM})
        self.page.wait_for_timeout(settle)
        return x, y

    def click(self, selector: str, settle: int = 420, after: int = 350,
              shot: str | None = None):
        x, y = self.point(selector, settle, shot)
        self.page.evaluate("([x,y])=>window.__demo.ripple(x,y)", [x, y])
        self.page.wait_for_timeout(130)
        self.page.locator(selector).first.click()
        self.page.wait_for_timeout(after)


def capture(base: str, work: pathlib.Path, project_id: str) -> dict:
    from playwright.sync_api import sync_playwright

    source = ROOT / "data" / "voices" / VOICE_ID / "reference.wav"
    if not source.exists():
        raise SystemExit("The 'demo rec' reference is missing")
    narration_state = api(base, f"/api/state?p={project_id}")
    narration_chunks = narration_state["project"]["chunks"]
    script = "\n\n".join(chunk["text"] for chunk in narration_chunks)
    chunk_count = len(narration_chunks)
    def row_for(prefix: str) -> int:
        return next(index for index, chunk in enumerate(narration_chunks, 1)
                    if chunk["text"].startswith(prefix))
    setup_row = row_for("To give it a voice")
    edit_row = row_for("Once they're ready")
    reorder_row = row_for("I can also change")
    setup_id = narration_chunks[setup_row - 1]["id"]
    timeline_segments = api(base, f"/api/projects/{project_id}/timeline")["segments"]
    opening_end = next(segment["start"] for segment in timeline_segments
                       if segment["id"] == setup_id)
    temp = api(base, "/api/projects", "POST", {"title": "Mynah demo"})["project"]
    temp_id = temp["id"]
    # A tiny run-specific delta makes these screen-capture takes genuinely
    # stale even after an earlier rehearsal left content-addressed audio in
    # the cache. The editable narration project remains fixed at 0.65.
    capture_temperature = 0.66 + (time.time_ns() % 1_000_000) / 1_000_000_000
    api(base, f"/api/projects/{temp_id}", "POST",
        {"voice_id": VOICE_ID, "params": {"temperature": capture_temperature}})

    raw_dir = work / "capture"
    if raw_dir.exists():
        shutil.rmtree(raw_dir)
    raw_dir.mkdir(parents=True)

    with sync_playwright() as p:
        microphone = raw_dir / "demo-rec-mic.wav"
        record_for = prepare_microphone(source, microphone) + 0.45
        browser = p.chromium.launch(args=[
            "--force-color-profile=srgb",
            "--use-fake-device-for-media-stream",
            "--use-fake-ui-for-media-stream",
            f"--use-file-for-fake-audio-capture={microphone}%noloop",
        ])
        started = time.monotonic()
        context = browser.new_context(
            viewport={"width": SOURCE_W, "height": SOURCE_H},
            record_video_dir=str(raw_dir),
            record_video_size={"width": SOURCE_W, "height": SOURCE_H},
            accept_downloads=True,
            permissions=["microphone"],
        )
        page = context.new_page()
        stage = Stage(page, started)
        recorded_voice = ""
        try:
            # The opening is the finished project playing its own narration.
            page.goto(f"{base}/?p={project_id}", wait_until="networkidle")
            page.evaluate(OVERLAY_JS); page.evaluate("window.__demo.init()")
            page.wait_for_timeout(600)
            stage.click("#preview-open", after=150)
            page.wait_for_selector("#preview-active:not([hidden])", timeout=60000)
            page.wait_for_function("document.getElementById('preview-audio').readyState >= 3")
            page.evaluate("() => { const a=document.getElementById('preview-audio'); a.currentTime=0; a.play(); }")
            stage.mark("intro_start")
            # Chromium occasionally stops advancing the audio element while a
            # Playwright video recorder is attached. The final soundtrack is
            # assembled from Mynah's exported WAV, so follow each opening row
            # using the measured timeline instead of depending on that playhead.
            opening_segments = timeline_segments[:setup_row - 1]
            for index, segment in enumerate(opening_segments, 1):
                stage.shot("detail", f".chunk:nth-child({index})")
                next_start = (opening_segments[index]["start"]
                              if index < len(opening_segments)
                              else opening_end - 0.18)
                page.wait_for_timeout(int(max(0.25, next_start - segment["start"]) * 1000))
            page.evaluate("document.getElementById('preview-audio').pause()")
            stage.click("#preview-close", after=500)
            stage.shot("wide")
            stage.mark("intro_end")

            # Feed the user's real 17.3-second "demo rec" through the app's
            # actual microphone flow. The long recording and compilation are
            # time-lapsed later, but the meter, timer, upload and compile are
            # all the live product behavior.
            page.goto(f"{base}/?p={temp_id}", wait_until="networkidle")
            page.evaluate(OVERLAY_JS); page.evaluate("window.__demo.init()")
            page.wait_for_timeout(650)
            stage.mark("voice_start")
            stage.shot("wide")
            page.wait_for_timeout(900)
            stage.point("#open-voices", settle=750, shot="close")
            stage.click("#open-voices", settle=0, after=650)
            previous_voice = page.evaluate("STATE.project.voice_id")
            page.once("dialog", lambda dialog: dialog.accept("demo rec"))
            stage.click("#record", after=100, shot="detail")
            stage.mark("record_start")
            page.wait_for_timeout(int(record_for * 1000))
            stage.click("#record", after=250, shot="detail")
            stage.mark("record_stop")
            page.wait_for_function(
                "old => STATE.project.voice_id && STATE.project.voice_id !== old",
                arg=previous_voice, timeout=120000)
            recorded_voice = page.evaluate("STATE.project.voice_id")
            stage.shot("close", "#voices-drawer")
            page.wait_for_function(
                "id => STATE.voices.some(v => v.id === id && v.status === 'ready')",
                arg=recorded_voice, timeout=300000)
            stage.mark("compile_end")
            page.wait_for_timeout(650)
            stage.point(".voice-card.current", settle=650, shot="detail")
            stage.mark("voice_end")

            # Paste the exact narration heard so far.
            stage.mark("paste_start")
            stage.click("#voices-drawer [data-close]", after=500, shot="close")
            stage.click("#paste-script", after=250, shot="close")
            stage.point("#script", settle=250, shot="detail")
            page.locator("#script").fill(script)
            page.wait_for_timeout(1300)
            stage.click("#split", after=850, shot="close")
            stage.shot("close", "#chunks")
            page.wait_for_timeout(900)
            stage.mark("paste_end")

            # Real sequential generation, then the line-level controls.
            stage.mark("edit_start")
            stage.point("#generate", settle=850, shot="close")
            stage.mark("generate_click")
            stage.click("#generate", settle=0, after=700)
            page.wait_for_function(
                "document.querySelector('.chunk:nth-child(1)').classList.contains('s-rendering')",
                timeout=120000)
            stage.shot("detail", ".chunk:nth-child(1)")
            stage.mark("generation_queue_start")
            page.wait_for_timeout(650)
            for index in range(1, chunk_count + 1):
                selector = f".chunk:nth-child({index})"
                try:
                    page.wait_for_function(
                        "i => document.querySelector(`.chunk:nth-child(${i})`)?.classList.contains('s-rendering')",
                        arg=index, timeout=180000)
                except Exception:
                    pass
                stage.shot("detail", selector)
                page.wait_for_timeout(550)
            page.wait_for_function(
                "[...document.querySelectorAll('.chunk')].every(x=>x.classList.contains('s-ready'))",
                timeout=600000)
            stage.mark("generation_end")
            page.wait_for_timeout(550)

            # The next narration beat is specifically about editing and
            # re-rolling, so let those actions occupy the full beat.
            stage.mark("edit_action_start")
            row = f".chunk:nth-child({edit_row})"
            stage.shot("detail", row)
            stage.point(row + " textarea", settle=600)
            field = page.locator(row + " textarea")
            field.click(); page.keyboard.press("Meta+A"); page.wait_for_timeout(450)
            stage.mark("edit_typing_start")
            edited_text = narration_chunks[edit_row - 1]["text"].replace("re-roll", "reroll")
            field.type(edited_text, delay=24)
            stage.mark("edit_typing_end")
            page.evaluate("document.activeElement.blur()")
            page.wait_for_function(
                "i => document.querySelector(`.chunk:nth-child(${i})`).classList.contains('s-stale')",
                arg=edit_row, timeout=30000)
            page.wait_for_timeout(900)
            stage.shot("detail", row)
            stage.point(row + " .regen", settle=500)
            stage.mark("reroll_start")
            stage.click(row + " .regen", settle=0, after=550)
            page.wait_for_function(
                "i => !document.querySelector(`.chunk:nth-child(${i})`).classList.contains('s-rendering') && document.querySelector(`.chunk:nth-child(${i})`).classList.contains('s-ready')",
                arg=edit_row, timeout=300000)
            stage.mark("reroll_end")
            page.wait_for_timeout(850)
            stage.mark("edit_action_end")

            # Pause and drag have their own line and their own camera hold.
            stage.mark("reorder_action_start")
            row = f".chunk:nth-child({reorder_row})"
            stage.shot("detail", row)
            stage.point(row + " .pause", settle=650)
            pause = page.locator(row + " .pause")
            pause.click(); page.keyboard.press("Meta+A"); page.wait_for_timeout(300)
            pause.type("0.6", delay=180); page.evaluate("document.activeElement.blur()")
            page.wait_for_timeout(1000)
            stage.mark("pause_action_end")
            stage.point(row + " .drag", settle=550)
            stage.mark("drag_action_start")
            handle = page.locator(row + " .drag").bounding_box()
            target = page.locator(".chunk:nth-child(4)").bounding_box()
            start_x = handle["x"] + handle["width"] / 2
            start_y = handle["y"] + handle["height"] / 2
            end_x = start_x
            end_y = target["y"] + 8
            before_order = page.evaluate("chunkOrder()")
            page.mouse.move(start_x, start_y)
            page.mouse.down()
            page.wait_for_timeout(250)
            page.evaluate("window.__demo.cursor.style.transition='none'")
            for step in range(1, 25):
                amount = step / 24
                y = start_y + (end_y - start_y) * amount
                page.mouse.move(end_x, y)
                page.evaluate("([x,y])=>window.__demo.move(x,y)", [end_x, y])
                if step % 4 == 0:
                    stage.camera.append({"time": stage.at(), "x": SOURCE_W / 2,
                                         "y": y, "zoom": DETAIL_ZOOM})
                page.wait_for_timeout(55)
            page.mouse.up()
            page.evaluate("window.__demo.cursor.style.transition='' ")
            page.wait_for_timeout(1100)
            after_order = page.evaluate("chunkOrder()")
            if after_order == before_order:
                raise RuntimeError("the recorded drag did not reorder the chunk")
            stage.mark("reorder_action_end")
            stage.shot("detail", ".chunk:nth-child(4)")
            page.wait_for_timeout(650)
            stage.mark("edit_end")

            # Show a few seconds of the real stitched waveform, then verify the
            # browser receives the exported WAV and hold its in-app confirmation.
            stage.mark("final_start")
            stage.click("#preview-open", after=150, shot="close")
            page.wait_for_selector("#preview-active:not([hidden])", timeout=60000)
            page.wait_for_function("document.getElementById('preview-audio').readyState >= 3")
            page.evaluate("() => { const a=document.getElementById('preview-audio'); a.playbackRate=1; a.currentTime=0; a.play(); }")
            stage.mark("preview_play_start")
            stage.shot("detail", "#preview-active")
            page.wait_for_timeout(4000)
            page.evaluate("document.getElementById('preview-audio').pause()")
            stage.mark("preview_play_end")
            with page.expect_download(timeout=30000) as download_info:
                stage.click("#export", after=250, shot="close")
            download = download_info.value
            if download.failure():
                raise RuntimeError(f"export failed: {download.failure()}")
            stage.mark("export_downloaded")
            stage.shot("detail", ".bar.bottom")
            page.wait_for_timeout(1800)
            stage.shot("wide")
            page.wait_for_timeout(850)
            stage.mark("final_end")
        finally:
            context.close(); browser.close()
            try:
                if recorded_voice:
                    api(base, f"/api/voices/{recorded_voice}?p={project_id}", "DELETE")
                api(base, f"/api/projects/{temp_id}", "DELETE")
            except Exception:
                pass

    video = next(raw_dir.glob("*.webm"))
    return {"video": str(video), "marks": stage.marks, "camera": stage.camera,
            "temp_id": temp_id}


def font(size: int, bold: bool = False):
    path = pathlib.Path("/System/Library/Fonts/Supplemental") / ("Arial Bold.ttf" if bold else "Arial.ttf")
    return ImageFont.truetype(str(path), size)


def wrap_text(draw: ImageDraw.ImageDraw, text: str, face, max_width: int) -> list[str]:
    lines, current = [], ""
    for word in text.split():
        trial = f"{current} {word}".strip()
        if current and draw.textlength(trial, font=face) > max_width:
            lines.append(current); current = word
        else:
            current = trial
    if current:
        lines.append(current)
    return lines


def smoothstep(value: float) -> float:
    value = min(1.0, max(0.0, value))
    return value * value * (3 - 2 * value)


def camera_at(events: list[dict], t: float) -> tuple[float, float, float]:
    if not events:
        return SOURCE_W / 2, SOURCE_H / 2, 1.0
    previous = events[0]
    for event in events[1:]:
        if t < event["time"]:
            break
        previous = event
    index = events.index(previous)
    if index == 0:
        return previous["x"], previous["y"], previous["zoom"]
    older = events[index - 1]
    blend = smoothstep((t - previous["time"]) / 0.45)
    return tuple(older[key] + (previous[key] - older[key]) * blend
                 for key in ("x", "y", "zoom"))


def render_camera(frame: np.ndarray, x: float, y: float, zoom: float) -> np.ndarray:
    # The full app lives in the middle of a 4:5 matte. Keep that screen plane
    # anchored as the camera zooms: following a row should feel like a small
    # camera adjustment, not like the whole browser viewport is sliding around
    # inside the black canvas.
    ext = np.empty((SOURCE_H * 2, SOURCE_W, 3), dtype=np.uint8)
    ext[:] = BG
    ext[SOURCE_H // 2:SOURCE_H // 2 + SOURCE_H] = frame
    focus = smoothstep((zoom - 1) / (DETAIL_ZOOM - 1))
    center_x = SOURCE_W / 2 + (x - SOURCE_W / 2) * focus
    crop_w = SOURCE_W / zoom
    crop_h = SOURCE_H * 2 / zoom
    x0 = int(max(0, min(SOURCE_W - crop_w, center_x - crop_w / 2)))
    vertical_follow = max(-70.0, min(70.0, (y - SOURCE_H / 2) * 0.18))
    center_y = SOURCE_H + vertical_follow * focus
    y0 = int(max(0, min(SOURCE_H * 2 - crop_h, center_y - crop_h / 2)))
    crop = ext[y0:int(y0 + crop_h), x0:int(x0 + crop_w)]
    return cv2.resize(crop, (WIDTH, HEIGHT), interpolation=cv2.INTER_LANCZOS4)


def render_intro_line(frame: np.ndarray, row_y: float, reveal: float) -> np.ndarray:
    """Isolate one complete line, then pull back into the full application.

    A conventional 4:5 crop cannot both preserve the desktop line's full
    width and exclude the rows above and below it. Start from an enlarged row
    strip on the matte, then animate that strip into its position in the wide
    screen while the rest of the interface is revealed.
    """
    reveal = smoothstep(reveal)
    wide = render_camera(frame, SOURCE_W / 2, SOURCE_H / 2, 1.0)

    # Keep the real interface recognizable behind the opening row. A moderate
    # blur and dark grade separate the sharp subject while making every other
    # control illegible. The cover layer fills the portrait edges; the second
    # layer preserves the complete desktop layout. Both resolve into the live
    # wide interface as the camera pulls back.
    cover_scale = max(WIDTH / SOURCE_W, HEIGHT / SOURCE_H)
    cover = cv2.resize(
        frame,
        (round(SOURCE_W * cover_scale), round(SOURCE_H * cover_scale)),
        interpolation=cv2.INTER_LANCZOS4,
    )
    cover_left = (cover.shape[1] - WIDTH) // 2
    cover = cover[:HEIGHT, cover_left:cover_left + WIDTH]
    cover = cv2.GaussianBlur(cover, (0, 0), 16)
    tint = np.empty_like(cover)
    tint[:] = BG
    ambient = cv2.addWeighted(tint, 0.68, cover, 0.32, 0)
    full_interface = cv2.GaussianBlur(wide, (0, 0), 11)
    ambient = cv2.addWeighted(ambient, 0.54, full_interface, 0.46, 0)
    matte = cv2.addWeighted(ambient, 1 - reveal, wide, reveal, 0)

    # Playwright's 1440×900 capture places each line card at x=280..1160 and
    # 81px high. Include the half-pixel antialiased border, with no surrounding
    # page pixels, so the cutout is exactly the highlighted container.
    source_left, source_right = 280, 1160
    half_height = 41
    source_top = max(0, round(row_y) - half_height)
    source_bottom = min(SOURCE_H, round(row_y) + half_height)
    strip = frame[source_top:source_bottom, source_left:source_right]

    focus_width = WIDTH - 80
    focus_height = round(strip.shape[0] * focus_width / strip.shape[1])
    wide_scale = WIDTH / SOURCE_W
    settled_width = round((source_right - source_left) * wide_scale)
    settled_height = round(strip.shape[0] * wide_scale)
    width = round(focus_width + (settled_width - focus_width) * reveal)
    height = round(focus_height + (settled_height - focus_height) * reveal)
    resized = cv2.resize(strip, (width, height), interpolation=cv2.INTER_LANCZOS4)

    focus_x = (WIDTH - focus_width) / 2
    focus_y = (HEIGHT - focus_height) / 2
    settled_x = source_left * wide_scale
    screen_top = (HEIGHT - SOURCE_H * wide_scale) / 2
    settled_y = screen_top + source_top * wide_scale
    left = round(focus_x + (settled_x - focus_x) * reveal)
    top = round(focus_y + (settled_y - focus_y) * reveal)

    # The moving strip is fully opaque while isolated. Fade it out only near
    # the end of the pullback, when the same row underneath is already clear.
    overlay_alpha = 1 - smoothstep(max(0.0, (reveal - 0.55) / 0.45))
    if overlay_alpha > 0:
        region = matte[top:top + height, left:left + width]
        if region.shape == resized.shape:
            matte[top:top + height, left:left + width] = cv2.addWeighted(
                region, 1 - overlay_alpha, resized, overlay_alpha, 0)
    return matte


def render_intro_pan(frame: np.ndarray, row_distance: float, amount: float) -> np.ndarray:
    """Pan the fully revealed interface down by one source-row distance."""
    wide = render_camera(frame, SOURCE_W / 2, SOURCE_H / 2, 1.0)
    shift = -round(row_distance * (WIDTH / SOURCE_W) * smoothstep(amount))
    if not shift:
        return wide
    moved = np.empty_like(wide)
    moved[:] = BG
    moved[:HEIGHT + shift] = wide[-shift:]
    return moved


def timeline(base: str, project_id: str) -> tuple[list[dict], float]:
    state = api(base, f"/api/state?p={project_id}")
    rows = {c["id"]: c["text"] for c in state["project"]["chunks"]}
    data = api(base, f"/api/projects/{project_id}/timeline")
    result = [{**segment, "text": rows[segment["id"]]} for segment in data["segments"]]
    return result, data["duration"]


def post(base: str, result: dict, work: pathlib.Path, project_id: str) -> pathlib.Path:
    segments, spoken_duration = timeline(base, project_id)
    def segment_for(prefix: str) -> tuple[int, dict]:
        return next((index, segment) for index, segment in enumerate(segments)
                    if segment["text"].startswith(prefix))
    setup_index, setup_segment = segment_for("To give it a voice")
    paste_index, paste_segment = segment_for("Mynah turns")
    generate_index, generate_segment = segment_for("Mynah generates")
    edit_index, edit_segment = segment_for("Once they're ready")
    reorder_index, reorder_segment = segment_for("I can also change")
    final_index, final_segment = segment_for("The preview button")
    closing_index, closing_segment = segment_for("This entire demo")
    narration = work / "recursive-narration.wav"
    with urllib.request.urlopen(f"{base}/api/projects/{project_id}/export.wav") as response:
        narration.write_bytes(response.read())

    # The reference gets its own uninterrupted beat between the narrator's
    # setup line and "Mynah turns that recording...". Match its loudness to
    # the generated voice without otherwise processing or shortening it.
    reference_path = ROOT / "data" / "voices" / VOICE_ID / "reference.wav"
    generated, rate = sf.read(narration, dtype="float32")
    reference, reference_rate = sf.read(reference_path, dtype="float32")
    if reference_rate != rate:
        raise ValueError("demo rec and narration must use the same sample rate")
    import pyloudnorm
    meter = pyloudnorm.Meter(rate)
    generated_lufs = meter.integrated_loudness(generated)
    reference_lufs = meter.integrated_loudness(reference)
    reference *= 10 ** ((generated_lufs - reference_lufs) / 20)
    fade = min(round(0.025 * rate), len(reference) // 2)
    reference[:fade] *= np.linspace(0, 1, fade, dtype="float32")
    reference[-fade:] *= np.linspace(1, 0, fade, dtype="float32")
    after_reference = np.zeros(round(0.35 * rate), dtype="float32")
    insert_at = paste_segment["start"]
    insert_sample = round(insert_at * rate)
    soundtrack_data = np.concatenate([
        generated[:insert_sample], reference, after_reference, generated[insert_sample:]
    ])
    soundtrack = work / "recursive-soundtrack.wav"
    sf.write(soundtrack, soundtrack_data, rate)
    reference_duration = len(reference) / rate
    inserted_duration = reference_duration + len(after_reference) / rate
    final_duration = len(soundtrack_data) / rate

    marks = result["marks"]
    voice_t0 = setup_segment["start"]
    record_t0 = insert_at
    record_t1 = record_t0 + reference_duration
    line4_t0 = record_t0 + inserted_duration
    generate_t0 = generate_segment["start"] + inserted_duration
    edit_t0 = edit_segment["start"] + inserted_duration
    reorder_t0 = reorder_segment["start"] + inserted_duration
    final_t0 = final_segment["start"] + inserted_duration
    closing_t0 = closing_segment["start"] + inserted_duration
    paste_t0 = line4_t0 + 3.4
    # The comparison setup line is now much longer than the UI action needed
    # to open Voices and click Record. Keep the pointer movement near natural
    # speed, rest on Record while the sentence finishes, then click immediately
    # before the real sample begins.
    record_action_source = max(marks["voice_start"], marks["record_start"] - 0.70)
    drawer_action_end = min(record_t0 - 0.90, voice_t0 + 3.4)
    generate_span = edit_t0 - generate_t0
    edit_span = reorder_t0 - edit_t0
    source_sections = [
        (marks["intro_start"], marks["intro_end"], 0.0, voice_t0),
        # Move into the drawer at normal speed, hold on the Record control for
        # the remainder of the spoken setup, then show the click itself.
        (marks["voice_start"], record_action_source, voice_t0, drawer_action_end),
        (record_action_source, record_action_source + 0.04,
         drawer_action_end, record_t0 - 0.70),
        (record_action_source, marks["record_start"], record_t0 - 0.70, record_t0),
        # The user's full 17.3-second explainer plays at natural speed while
        # the real meter and timer run.
        (marks["record_start"], marks["record_stop"], record_t0, record_t1),
        # Resume generated narration over Stop, compilation and voice ready,
        # then let the script interaction occupy the rest of that line.
        (marks["record_stop"], marks["compile_end"], record_t1, line4_t0 + 2.4),
        (marks["compile_end"], marks["voice_end"], line4_t0 + 2.4, paste_t0),
        (marks["paste_start"], marks["paste_end"], paste_t0, generate_t0),
        # Settle on Generate, show the click, then pan into the queue before
        # the generation timelapse begins.
        (marks["edit_start"], marks["generate_click"],
         generate_t0, generate_t0 + generate_span * 0.18),
        (marks["generate_click"], marks["generation_queue_start"],
         generate_t0 + generate_span * 0.18, generate_t0 + generate_span * 0.31),
        (marks["generation_queue_start"], marks["generation_end"],
         generate_t0 + generate_span * 0.31, edit_t0),
        # Preserve the visible selection and typed replacement, hold the stale
        # state, then give the reroll's rendering state time to read.
        (marks["generation_end"], marks["edit_action_start"],
         edit_t0, edit_t0 + edit_span * 0.07),
        (marks["edit_action_start"], marks["edit_typing_start"],
         edit_t0 + edit_span * 0.07, edit_t0 + edit_span * 0.20),
        (marks["edit_typing_start"], marks["edit_typing_end"],
         edit_t0 + edit_span * 0.20, edit_t0 + edit_span * 0.55),
        (marks["edit_typing_end"], marks["reroll_start"],
         edit_t0 + edit_span * 0.55, edit_t0 + edit_span * 0.70),
        (marks["reroll_start"], marks["reroll_end"],
         edit_t0 + edit_span * 0.70, edit_t0 + edit_span * 0.92),
        (marks["reroll_end"], marks["edit_action_end"],
         edit_t0 + edit_span * 0.92, reorder_t0),
        (marks["reorder_action_start"], marks["pause_action_end"],
         reorder_t0, reorder_t0 + (final_t0 - reorder_t0) * 0.46),
        (marks["pause_action_end"], marks["drag_action_start"],
         reorder_t0 + (final_t0 - reorder_t0) * 0.46,
         reorder_t0 + (final_t0 - reorder_t0) * 0.56),
        (marks["drag_action_start"], marks["reorder_action_end"],
         reorder_t0 + (final_t0 - reorder_t0) * 0.56, final_t0 - 0.45),
        (marks["reorder_action_end"], marks["edit_end"], final_t0 - 0.45, final_t0),
        # Let Preview and Export occupy their own feature line at natural
        # speed. Hold the confirmed download while the closing CTA finishes.
        (marks["final_start"], marks["preview_play_start"],
         final_t0, final_t0 + 1.4),
        (marks["preview_play_start"], marks["preview_play_end"],
         final_t0 + 1.4, final_t0 + 4.9),
        (marks["preview_play_end"], marks["export_downloaded"],
         final_t0 + 4.9, closing_t0),
        (marks["export_downloaded"], marks["final_end"],
         closing_t0, min(final_duration, closing_t0 + 2.8)),
        (max(marks["export_downloaded"], marks["final_end"] - 0.04),
         marks["final_end"], min(final_duration, closing_t0 + 2.8), final_duration),
    ]
    # Paste, Generate, edit and reorder are one continuous screen workflow;
    # camera motion handles those handoffs without a dissolve.
    scene_boundaries = [voice_t0, final_t0]

    def source_time(t: float) -> float:
        for s0, s1, t0, t1 in source_sections:
            if t <= t1 + 1e-6:
                return s0 + (s1 - s0) * ((t - t0) / max(t1 - t0, 1e-6))
        return source_sections[-1][1]

    def target_time(source: float) -> float | None:
        for s0, s1, t0, t1 in source_sections:
            if s0 <= source <= s1:
                return t0 + (t1 - t0) * ((source - s0) / max(s1 - s0, 1e-6))
        return None

    camera = []
    for event in result["camera"]:
        mapped = target_time(event["time"])
        if mapped is not None:
            camera.append({**event, "time": mapped,
                           "zoom": DETAIL_ZOOM if event["zoom"] > 1 else 1.0})
    camera.sort(key=lambda item: item["time"])

    # Queue completion flows directly into editing at the same row-level
    # scale. Older captures contain a wide keyframe at this exact boundary;
    # discard it so the camera pans to the editable row without pumping out
    # and back in.
    camera = [event for event in camera
              if not (event["zoom"] == 1.0 and abs(event["time"] - edit_t0) < 0.8)]

    # The opening plays lines one and two continuously. Their source camera
    # events give us the exact row centres used by the isolated opening below.
    intro_close = [event for event in camera
                   if event["time"] < voice_t0 and event["zoom"] > 1]
    later_camera = [event for event in camera if event["time"] >= voice_t0]
    if intro_close:
        camera = ([{**intro_close[0], "time": 0.0, "zoom": DETAIL_ZOOM}]
                  + intro_close[1:] + later_camera)
    else:
        camera = later_camera

    intro_rows = [event["y"] for event in intro_close[:2]]
    if len(intro_rows) != 2:
        raise RuntimeError("opening capture did not contain both line centres")
    first_line_end = segments[0]["end"]
    second_line_start = segments[1]["start"]
    second_line_end = segments[1]["end"]
    # Finish line one in isolation and complete the pullback before moving the
    # camera. Only then pan down by one row while line two is speaking.
    reveal_start = first_line_end
    reveal_end = reveal_start + 1.00
    pan_start = max(reveal_end, second_line_start)
    pan_end = pan_start + 0.80

    cap = cv2.VideoCapture(result["video"])
    raw = work / "recursive-picture.mp4"
    writer = cv2.VideoWriter(str(raw), cv2.VideoWriter_fourcc(*"avc1"), FPS, (WIDTH, HEIGHT))
    if not writer.isOpened():
        writer = cv2.VideoWriter(str(raw), cv2.VideoWriter_fourcc(*"mp4v"), FPS, (WIDTH, HEIGHT))
    frame_count = math.ceil(final_duration * FPS)
    for number in range(frame_count):
        t = number / FPS
        mapped_source = source_time(t)
        cap.set(cv2.CAP_PROP_POS_MSEC, mapped_source * 1000)
        ok, frame = cap.read()
        if not ok:
            raise RuntimeError(f"Could not read source frame at {mapped_source:.2f}s")
        if t < reveal_end:
            reveal = (t - reveal_start) / max(reveal_end - reveal_start, 1e-6)
            framed = render_intro_line(
                frame, intro_rows[0], min(1.0, max(0.0, reveal)))
        elif t < voice_t0:
            pan = (t - pan_start) / max(pan_end - pan_start, 1e-6)
            framed = render_intro_pan(
                frame, intro_rows[1] - intro_rows[0], min(1.0, max(0.0, pan)))
        else:
            x, y, zoom = camera_at(camera, t)
            framed = render_camera(frame, x, y, zoom)

        # A short dissolve gives each major workflow change a visual handoff.
        # Camera pans and the generation timelapse stay as direct motion.
        dissolve = 0.60 if abs(t - voice_t0) < 0.60 else 0.28
        boundary = next((value for value in scene_boundaries
                         if value <= t < value + dissolve), None)
        if boundary is not None:
            prior = next(section for section in source_sections if abs(section[3] - boundary) < 1e-5)
            cap.set(cv2.CAP_PROP_POS_MSEC, max(prior[0], prior[1] - 0.04) * 1000)
            prior_ok, prior_frame = cap.read()
            if prior_ok:
                px, py, pzoom = camera_at(camera, max(0, boundary - 0.04))
                prior_framed = render_camera(prior_frame, px, py, pzoom)
                alpha = smoothstep((t - boundary) / dissolve)
                framed = cv2.addWeighted(prior_framed, 1 - alpha, framed, alpha, 0)

        # The only editorial overlay is a small state change marker when the
        # soundtrack switches from generated narration to the original sample.
        if record_t0 <= t <= record_t1:
            image = Image.fromarray(cv2.cvtColor(framed, cv2.COLOR_BGR2RGB))
            draw = ImageDraw.Draw(image, "RGBA")
            fade = min(1.0, (t - record_t0) / 0.25, (record_t1 - t) / 0.25)
            alpha = round(228 * max(0.0, fade))
            label = "ORIGINAL VOICE SAMPLE"
            label_face = font(23, True)
            width = draw.textlength(label, font=label_face)
            left, top = 48, 48
            right, bottom = left + width + 68, top + 48
            draw.rounded_rectangle((left, top, right, bottom), radius=24,
                                   fill=(5, 8, 9, alpha),
                                   outline=(90, 209, 200, alpha), width=2)
            draw.ellipse((left + 18, top + 18, left + 30, top + 30),
                         fill=(240, 138, 118, alpha))
            draw.text((left + 44, top + 24), label, font=label_face,
                      fill=(239, 244, 245, alpha), anchor="lm")
            framed = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR)
        writer.write(framed)
    writer.release(); cap.release()

    output = DOCS / "mynah-recursive-demo.mp4"
    subprocess.run([
        ffmpeg(), "-y", "-v", "error", "-i", str(raw), "-i", str(soundtrack),
        "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k", "-ar", "24000", "-ac", "1",
        "-t", f"{final_duration:.3f}", "-movflags", "+faststart", str(output)
    ], check=True)
    return output


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:8765")
    parser.add_argument("--project", default=PROJECT_ID)
    args = parser.parse_args()
    work = DOCS / "raw" / "recursive"
    work.mkdir(parents=True, exist_ok=True)
    result = capture(args.url.rstrip("/"), work, args.project)
    (work / "capture.json").write_text(json.dumps(result, indent=2))
    output = post(args.url.rstrip("/"), result, work, args.project)
    info = sf.info(work / "recursive-narration.wav")
    print(json.dumps({"output": str(output), "duration": info.duration,
                      "project": args.project}, indent=2))


if __name__ == "__main__":
    main()
