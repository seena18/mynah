"""Release regressions against a temporary server with synthetic inference.

MYNAH_BROWSER_TESTS=1 uv run --group dev python -m unittest discover -v tests
"""

import json
import os
import pathlib
import socket
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request


@unittest.skipUnless(os.environ.get("MYNAH_BROWSER_TESTS") == "1", "opt-in browser suite")
class Browser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        cls.base = f"http://127.0.0.1:{port}"
        cls.server = subprocess.Popen([
            sys.executable, str(pathlib.Path(__file__).with_name("serve_stub.py")), str(port),
        ])
        cls.addClassCleanup(cls.server.wait)
        cls.addClassCleanup(cls.server.terminate)
        for _ in range(100):
            try:
                urllib.request.urlopen(cls.base + "/api/state", timeout=1).close()
                break
            except urllib.error.URLError:
                if cls.server.poll() is not None:
                    raise RuntimeError("test server exited")
                time.sleep(0.1)
        else:
            raise RuntimeError("test server did not start")
        cls.playwright = sync_playwright().start()
        cls.addClassCleanup(cls.playwright.stop)
        cls.browser = cls.playwright.chromium.launch()
        cls.addClassCleanup(cls.browser.close)

    def request(self, path, method="GET", body=None):
        request = urllib.request.Request(
            self.base + path, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.load(response)

    def setUp(self):
        self.context = self.browser.new_context()
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.pid = self.new_project("Test A")
        self.other = self.new_project("Test B")
        self.request(f"/api/projects/{self.pid}/generate", "POST", {})
        self.wait_ready()
        self.page.goto(self.base + "/?p=" + self.pid)
        self.page.locator("#chunks textarea").wait_for()

    def new_project(self, title):
        pid = self.request("/api/projects", "POST", {"title": title})["project"]["id"]
        self.request(f"/api/projects/{pid}", "POST", {"voice_id": "testvoice"})
        self.request(f"/api/projects/{pid}/chunks", "POST", {"text": "Original spoken line."})
        return pid

    def wait_ready(self, text=None):
        for _ in range(100):
            state = self.request("/api/state?p=" + self.pid)
            chunk = state["project"]["chunks"][0]
            if chunk["status"] == "ready" and (text is None or chunk["text"] == text):
                return chunk
            time.sleep(0.05)
        self.fail("generation did not finish")

    def test_malformed_ids_preserve_projects_and_voices(self):
        paths = [
            ("/api/voices/%2e%2e", "DELETE", None),
            ("/api/projects/%2e%2e", "DELETE", None),
            ("/api/state?p=../voices", "GET", None),
            (f"/api/projects/{self.pid}", "POST", {"voice_id": ".."}),
        ]
        for path, method, body in paths:
            with self.subTest(path=path), self.assertRaises(urllib.error.HTTPError) as raised:
                self.request(path, method, body)
            self.assertEqual(raised.exception.code, 400)
        state = self.request("/api/state?p=" + self.pid)
        self.assertEqual(state["project"]["title"], "Test A")
        self.assertTrue(any(v["id"] == "testvoice" for v in state["voices"]))

    def test_footer_stays_at_viewport_bottom_for_short_project(self):
        footer_bottom = self.page.locator("footer.bar.bottom").evaluate(
            "node => node.getBoundingClientRect().bottom"
        )
        viewport_height = self.page.evaluate("window.innerHeight")
        self.assertAlmostEqual(footer_bottom, viewport_height, delta=1)

    def test_new_project_dialog_creates_and_switches_project(self):
        self.page.locator("#new-project").click()
        dialog = self.page.locator("#new-project-modal")
        dialog.wait_for(state="visible")
        name = self.page.locator("#new-project-name")
        self.assertEqual(name.input_value(), "Untitled")
        name.fill("Created in the app")
        name.press("Enter")
        dialog.wait_for(state="hidden")
        self.page.wait_for_function(
            "STATE.project.title === 'Created in the app' && STATE.project.chunks.length === 0"
        )
        self.assertEqual(self.page.locator("#title").input_value(), "Created in the app")
        self.assertIn("?p=", self.page.url)
        self.assertTrue(self.page.locator("#empty").is_visible())

    def test_reroll_plays_new_audio_at_same_url(self):
        chunk = self.wait_ready()
        url = f"/api/projects/{self.pid}/takes/{chunk['id']}.wav?v={chunk['fingerprint']}"
        fetch = """async url => {
          const response = await fetch(url);
          const hash = await crypto.subtle.digest('SHA-256', await response.arrayBuffer());
          return {hash: Array.from(new Uint8Array(hash)), cache: response.headers.get('cache-control')};
        }"""
        before = self.page.evaluate(fetch, url)
        self.request(f"/api/projects/{self.pid}/chunks/{chunk['id']}/generate", "POST", {})
        self.wait_ready()
        after = self.page.evaluate(fetch, url)
        self.assertNotEqual(before["hash"], after["hash"])
        self.assertEqual(after["cache"], "no-store")

    def test_dragging_lines_persists_the_new_order(self):
        self.request(f"/api/projects/{self.pid}/chunks", "POST", {"text": "Second line."})
        self.request(f"/api/projects/{self.pid}/chunks", "POST", {"text": "Third line."})
        self.page.reload()
        rows = self.page.locator("#chunks .chunk")
        self.assertEqual(rows.count(), 3)
        self.page.evaluate("""() => {
          window.__sawDragGhost = false;
          new MutationObserver(() => {
            if (document.querySelector('.drag-ghost')) window.__sawDragGhost = true;
          }).observe(document.body, {childList: true});
        }""")
        rows.nth(0).locator(".drag").drag_to(rows.nth(2))
        self.page.wait_for_function("STATE.project.chunks[1].text === 'Original spoken line.'")
        self.assertTrue(self.page.evaluate("window.__sawDragGhost"))
        self.assertEqual(self.page.locator(".drag-ghost").count(), 0)
        state = self.request("/api/state?p=" + self.pid)
        self.assertEqual([chunk["text"] for chunk in state["project"]["chunks"]], [
            "Second line.", "Original spoken line.", "Third line.",
        ])
        self.assertEqual(rows.locator(".idx").all_text_contents(), ["1", "2", "3"])

    def test_reorder_rejects_a_stale_chunk_list(self):
        state = self.request("/api/state?p=" + self.pid)
        chunk_id = state["project"]["chunks"][0]["id"]
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request(f"/api/projects/{self.pid}/chunks/order", "PUT", {"ids": [chunk_id, chunk_id]})
        self.assertEqual(raised.exception.code, 409)
        current = self.request("/api/state?p=" + self.pid)
        self.assertEqual(current["project"]["chunks"][0]["id"], chunk_id)

    def test_project_switch_clears_loaded_preview(self):
        self.page.locator("#preview-open").click()
        self.page.locator("#preview-active").wait_for(state="visible")
        self.page.locator("#project-select").select_option(self.other)
        self.page.wait_for_function("document.querySelector('#title').value === 'Test B'")
        self.assertTrue(self.page.locator("#preview-active").is_hidden())
        self.assertIsNone(self.page.locator("#preview-audio").get_attribute("src"))

    def test_deleting_a_chunk_discards_and_rebuilds_preview(self):
        self.request(f"/api/projects/{self.pid}/chunks", "POST", {"text": "Delete this line."})
        self.request(f"/api/projects/{self.pid}/generate", "POST", {})
        for _ in range(100):
            chunks = self.request("/api/state?p=" + self.pid)["project"]["chunks"]
            if len(chunks) == 2 and all(chunk["status"] == "ready" for chunk in chunks):
                break
            time.sleep(0.05)
        else:
            self.fail("both chunks did not finish")
        self.page.reload()
        self.page.locator("#preview-open").click()
        self.page.locator("#preview-active").wait_for(state="visible")
        self.page.locator("#chunks .chunk").nth(1).locator(".drop").click()
        self.page.locator("#chunks .chunk").nth(1).wait_for(state="detached")
        self.assertTrue(self.page.locator("#preview-active").is_hidden())
        self.assertIsNone(self.page.locator("#preview-audio").get_attribute("src"))

        self.page.locator("#preview-open").click()
        self.page.locator("#preview-active").wait_for(state="visible")
        self.page.wait_for_function("timelineSegments.length === 1")

    def test_project_switch_discards_inflight_preview(self):
        held = []
        self.page.route("**/preview.wav", lambda route: held.append(route))
        self.page.locator("#preview-open").click()
        for _ in range(100):
            if held:
                break
            self.page.wait_for_timeout(20)
        self.assertTrue(held)
        self.page.locator("#project-select").select_option(self.other)
        self.page.wait_for_function("document.querySelector('#title').value === 'Test B'")
        held[0].fulfill(response=held[0].fetch())
        self.page.wait_for_timeout(200)
        self.assertTrue(self.page.locator("#preview-active").is_hidden())
        self.assertIsNone(self.page.locator("#preview-audio").get_attribute("src"))

    def test_shortcut_saves_before_generating_even_with_slow_save(self):
        def slow_save(route):
            if route.request.method == "PUT":
                time.sleep(0.3)  # exceeds the old fixed 60 ms delay
            route.continue_()

        self.page.route("**/chunks/*", slow_save)
        editor = self.page.locator("#chunks textarea")
        editor.fill("Edited and generated with the shortcut.")
        editor.press("Control+Enter")
        self.page.wait_for_function("document.querySelector('#chunks .s-ready') !== null")
        # Pump browser routing until the edited text is saved and rendered.
        self.page.wait_for_function("STATE.project.chunks[0].text.startsWith('Edited') && STATE.project.chunks[0].status === 'ready'")
        self.assertEqual(self.wait_ready()["text"], editor.input_value())

    def test_generate_button_flushes_focused_edit(self):
        editor = self.page.locator("#chunks textarea")
        editor.fill("Generated by clicking the button.")
        self.page.locator("#generate").click()
        self.page.wait_for_function("STATE.project.chunks[0].text.startsWith('Generated') && STATE.project.chunks[0].status === 'ready'")
        self.assertEqual(self.wait_ready()["text"], "Generated by clicking the button.")

    def test_second_blur_saves_latest_text_while_first_save_is_pending(self):
        held = []

        def hold_first(route):
            if route.request.method == "PUT" and not held:
                held.append(route)
            else:
                route.continue_()

        self.page.route("**/chunks/*", hold_first)
        editor = self.page.locator("#chunks textarea")
        editor.fill("First edit.")
        editor.press("Tab")
        for _ in range(100):
            if held:
                break
            self.page.wait_for_timeout(20)
        self.assertTrue(held)
        editor.fill("Latest edit must survive.")
        editor.press("Tab")
        held[0].continue_()
        self.page.wait_for_function("STATE.project.chunks[0].text === 'Latest edit must survive.' && edits.size === 0")
        self.assertEqual(editor.input_value(), "Latest edit must survive.")

    def test_failed_save_does_not_generate_old_text(self):
        self.page.route("**/chunks/*", lambda route: route.fulfill(
            status=500, content_type="application/json", body='{"detail":"save failed"}',
        ) if route.request.method == "PUT" else route.continue_())
        generated = []
        self.page.on("request", lambda request: generated.append(request.url)
                     if request.url.endswith("/generate") else None)
        editor = self.page.locator("#chunks textarea")
        editor.fill("Keep this unsaved draft.")
        editor.press("Control+Enter")
        self.page.wait_for_function("document.querySelector('#queue').textContent === 'save failed'")
        self.assertEqual(generated, [])
        self.assertEqual(editor.input_value(), "Keep this unsaved draft.")
        self.assertEqual(self.wait_ready()["text"], "Original spoken line.")
