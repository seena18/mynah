"""Tests for everything that does not need the model.

Run with:  python -m unittest discover tests
"""

import json
import pathlib
import sys
import tempfile
import unittest

import numpy as np
import soundfile as sf

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from mynah import audio, store  # noqa: E402


class Split(unittest.TestCase):
    def test_blank_lines_break(self):
        self.assertEqual(store.split_script("One.\n\nTwo.\n\n\nThree."),
                         ["One.", "Two.", "Three."])

    def test_single_newlines_join(self):
        self.assertEqual(store.split_script("One\nline."), ["One line."])

    def test_limit_holds_with_sentences(self):
        text = "A sentence here. " * 40
        for chunk in store.split_script(text, 120):
            self.assertLessEqual(len(chunk), 120)

    def test_limit_holds_without_punctuation(self):
        chunks = store.split_script("word " * 300, 100)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 100)

    def test_greedy_packing_prefers_fewer_chunks(self):
        # Four 25-char sentences fit two-per-chunk at a 60 limit.
        text = "Twenty five characters.. " * 4
        self.assertEqual(len(store.split_script(text, 60)), 2)

    def test_empty(self):
        self.assertEqual(store.split_script(""), [])
        self.assertEqual(store.split_script("  \n\n  "), [])

    def test_unsplittable_token_survives(self):
        # A 300-character word cannot be spoken in pieces; leave it whole.
        self.assertEqual(store.split_script("x" * 300, 50), ["x" * 300])


class Fingerprint(unittest.TestCase):
    def setUp(self):
        self.project = store.Project(voice_id="v1")

    def test_stable(self):
        chunk = store.Chunk(text="hello")
        self.assertEqual(self.project.fingerprint(chunk), self.project.fingerprint(chunk))

    def test_pause_excluded(self):
        a = store.Chunk(text="hello", pause_after=0.2)
        b = store.Chunk(text="hello", pause_after=2.0)
        self.assertEqual(self.project.fingerprint(a), self.project.fingerprint(b))

    def test_whitespace_normalised(self):
        a = store.Chunk(text="hello")
        b = store.Chunk(text="  hello \n")
        self.assertEqual(self.project.fingerprint(a), self.project.fingerprint(b))

    def test_text_voice_params_included(self):
        chunk = store.Chunk(text="hello")
        base = self.project.fingerprint(chunk)
        self.assertNotEqual(base, self.project.fingerprint(store.Chunk(text="hello!")))
        self.assertNotEqual(base, store.Project(voice_id="v2").fingerprint(chunk))
        other = store.Project(voice_id="v1")
        other.params["temperature"] = 0.5
        self.assertNotEqual(base, other.fingerprint(chunk))


class Status(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        self._saved = (store.TAKES, store.VOICES)
        store.TAKES = root / "takes"
        store.VOICES = root / "voices"
        store.TAKES.mkdir()
        (store.VOICES / "v1").mkdir(parents=True)
        (store.VOICES / "v1" / "voice.pt").write_bytes(b"x")
        (store.VOICES / "compiling").mkdir()
        self.project = store.Project(voice_id="v1")

    def tearDown(self):
        store.TAKES, store.VOICES = self._saved
        self.tmp.cleanup()

    def test_empty_text(self):
        self.assertEqual(self.project.status(store.Chunk(text="  ")), "empty")

    def test_no_voice(self):
        self.assertEqual(store.Project().status(store.Chunk(text="hi")), "no-voice")

    def test_voice_still_compiling_counts_as_no_voice(self):
        project = store.Project(voice_id="compiling")
        self.assertEqual(project.status(store.Chunk(text="hi")), "no-voice")

    def test_stale_then_ready(self):
        chunk = store.Chunk(text="hi")
        self.assertEqual(self.project.status(chunk), "stale")
        self.project.take_path(chunk).write_bytes(b"x")
        self.assertEqual(self.project.status(chunk), "ready")

    def test_edit_invalidates_and_revert_restores(self):
        chunk = store.Chunk(text="original")
        self.project.take_path(chunk).write_bytes(b"x")
        chunk.text = "edited"
        self.assertEqual(self.project.status(chunk), "stale")
        chunk.text = "original"
        self.assertEqual(self.project.status(chunk), "ready")

    def test_unused_takes(self):
        chunk = store.Chunk(text="live")
        self.project.chunks = [chunk]
        live = self.project.take_path(chunk)
        live.write_bytes(b"x")
        orphan = store.TAKES / "deadbeef.wav"
        orphan.write_bytes(b"x")
        self.assertEqual(store.unused_takes(self.project), [orphan])


class Load(unittest.TestCase):
    def test_ignores_unknown_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            saved = store.PROJECT_FILE
            store.PROJECT_FILE = pathlib.Path(tmp) / "project.json"
            try:
                store.PROJECT_FILE.write_text(json.dumps({
                    "title": "t", "voice_id": "v", "future_field": 1,
                    "chunks": [{"id": "a", "text": "x", "rendered": "legacy"}],
                }))
                project = store.Project.load()
            finally:
                store.PROJECT_FILE = saved
        self.assertEqual(project.title, "t")
        self.assertEqual(project.chunks[0].text, "x")


class Stitch(unittest.TestCase):
    RATE = 24000

    def _tone(self, seconds: float, amplitude: float, pad: float) -> np.ndarray:
        t = np.arange(int(seconds * self.RATE)) / self.RATE
        tone = (amplitude * np.sin(2 * np.pi * 440 * t)).astype("float32")
        silence = np.zeros(int(pad * self.RATE), dtype="float32")
        return np.concatenate([silence, tone, silence])

    def _regions(self, data: np.ndarray) -> list[tuple[int, int]]:
        """Speech regions by a short envelope, not raw samples.

        A pure tone dips below the silence threshold at every zero crossing,
        so sample-level detection would report hundreds of regions. A 5 ms
        max-abs envelope reads a tone as one region, which is what the stitch
        (which only looks at first and last loud sample) also sees.
        """
        win = int(0.005 * self.RATE)
        n = len(data) // win
        env = np.abs(data[:n * win]).reshape(n, win).max(axis=1)
        loud = env > 10 ** (audio.TRIM_DB / 20)
        regions, open_at = [], None
        for i, on in enumerate(loud):
            if on and open_at is None:
                open_at = i
            elif not on and open_at is not None:
                regions.append((open_at * win, i * win))
                open_at = None
        if open_at is not None:
            regions.append((open_at * win, n * win))
        return regions

    def test_pause_is_the_pause_you_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            a, b, out = root / "a.wav", root / "b.wav", root / "out.wav"
            # 0.3 s of model-style silence around each tone; must not leak
            # into the gap.
            sf.write(str(a), self._tone(1.0, 0.3, 0.3), self.RATE)
            sf.write(str(b), self._tone(1.0, 0.3, 0.3), self.RATE)
            audio.stitch([(a, 0.5), (b, 0.0)], out, self.RATE)
            data, _ = sf.read(str(out), dtype="float32")
        regions = self._regions(data)
        self.assertEqual(len(regions), 2)
        gap = (regions[1][0] - regions[0][1]) / self.RATE
        expected = 0.5 + 2 * audio.EDGE_MARGIN
        self.assertAlmostEqual(gap, expected, delta=0.02)

    def test_loudness_matched_across_takes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            a, b, out = root / "a.wav", root / "b.wav", root / "out.wav"
            sf.write(str(a), self._tone(1.0, 0.05, 0.1), self.RATE)   # quiet
            sf.write(str(b), self._tone(1.0, 0.40, 0.1), self.RATE)   # 18 dB louder
            audio.stitch([(a, 0.2), (b, 0.0)], out, self.RATE)
            data, _ = sf.read(str(out), dtype="float32")
        regions = self._regions(data)
        rms = [np.sqrt((data[s:e] ** 2).mean()) for s, e in regions]
        self.assertAlmostEqual(rms[0] / rms[1], 1.0, delta=0.1)

    def test_nothing_to_stitch(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError):
                audio.stitch([], pathlib.Path(tmp) / "out.wav", self.RATE)


if __name__ == "__main__":
    unittest.main()
