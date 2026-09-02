"""Tests for everything that does not need the model.

Run with:  python -m unittest discover tests
"""

import json
import pathlib
import sys
import tempfile
import time
import unittest

import numpy as np
import soundfile as sf

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from mynah import audio, store  # noqa: E402


class Sandbox(unittest.TestCase):
    """Point every on-disk path at a temp dir for the duration of a test."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        self._saved = (store.DATA, store.TAKES, store.VOICES, store.PROJECTS,
                       store.LEGACY_PROJECT_FILE)
        store.DATA = root
        store.TAKES = root / "takes"
        store.VOICES = root / "voices"
        store.PROJECTS = root / "projects"
        store.LEGACY_PROJECT_FILE = root / "project.json"
        for path in (store.TAKES, store.VOICES, store.PROJECTS):
            path.mkdir()

    def tearDown(self):
        (store.DATA, store.TAKES, store.VOICES, store.PROJECTS,
         store.LEGACY_PROJECT_FILE) = self._saved
        self.tmp.cleanup()

    def voice(self, voice_id: str, compiled: bool = True) -> None:
        directory = store.VOICES / voice_id
        directory.mkdir(parents=True)
        if compiled:
            (directory / "voice.pt").write_bytes(b"x")


class Split(unittest.TestCase):
    def test_blank_lines_break(self):
        self.assertEqual(store.split_script("One.\n\nTwo.\n\n\nThree."),
                         ["One.", "Two.", "Three."])

    def test_single_newlines_join(self):
        self.assertEqual(store.split_script("One\nline."), ["One line."])

    def test_limit_holds_with_sentences(self):
        for chunk in store.split_script("A sentence here. " * 40, 120):
            self.assertLessEqual(len(chunk), 120)

    def test_limit_holds_without_punctuation(self):
        chunks = store.split_script("word " * 300, 100)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 100)

    def test_greedy_packing_prefers_fewer_chunks(self):
        self.assertEqual(len(store.split_script("Twenty five characters.. " * 4, 60)), 2)

    def test_empty(self):
        self.assertEqual(store.split_script(""), [])
        self.assertEqual(store.split_script("  \n\n  "), [])

    def test_unsplittable_token_survives(self):
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

    def test_project_identity_excluded(self):
        # Two projects, same line, same voice: one take on disk serves both.
        chunk = store.Chunk(text="hello")
        other = store.Project(voice_id="v1")
        self.assertNotEqual(self.project.id, other.id)
        self.assertEqual(self.project.fingerprint(chunk), other.fingerprint(chunk))

    def test_whitespace_normalised(self):
        self.assertEqual(self.project.fingerprint(store.Chunk(text="hello")),
                         self.project.fingerprint(store.Chunk(text="  hello \n")))

    def test_text_voice_params_included(self):
        chunk = store.Chunk(text="hello")
        base = self.project.fingerprint(chunk)
        self.assertNotEqual(base, self.project.fingerprint(store.Chunk(text="hello!")))
        self.assertNotEqual(base, store.Project(voice_id="v2").fingerprint(chunk))
        other = store.Project(voice_id="v1")
        other.params["temperature"] = 0.5
        self.assertNotEqual(base, other.fingerprint(chunk))


class Status(Sandbox):
    def setUp(self):
        super().setUp()
        self.voice("v1")
        self.voice("compiling", compiled=False)
        self.project = store.Project(voice_id="v1")

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


class Voices(Sandbox):
    def _meta(self, voice_id: str, **fields) -> None:
        directory = store.VOICES / voice_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "meta.json").write_text(json.dumps(
            {"id": voice_id, "name": "orig", "created": "2026-01-01T00:00:00",
             "status": "ready", "seconds": 12.0, **fields}))

    def test_rename_keeps_everything_else(self):
        self._meta("v1")
        meta = store.rename_voice("v1", "  Narrator  ")
        self.assertEqual(meta["name"], "Narrator")
        self.assertEqual(meta["seconds"], 12.0)
        self.assertEqual(store.list_voices()[0]["name"], "Narrator")

    def test_rename_blank_keeps_old_name(self):
        self._meta("v1")
        self.assertEqual(store.rename_voice("v1", "   ")["name"], "orig")

    def test_rename_unknown_is_keyerror(self):
        with self.assertRaises(KeyError):
            store.rename_voice("nope", "x")

    def test_list_marks_ready_by_compiled_file(self):
        self._meta("v1")
        self._meta("v2")
        (store.VOICES / "v1" / "voice.pt").write_bytes(b"x")
        ready = {v["id"]: v["ready"] for v in store.list_voices()}
        self.assertEqual(ready, {"v1": True, "v2": False})


class Projects(Sandbox):
    def test_create_list_delete(self):
        a = store.create_project("Alpha")
        time.sleep(1.05)                       # `updated` has second resolution
        b = store.create_project("Bravo")
        self.assertEqual([p.title for p in store.list_projects()], ["Bravo", "Alpha"])
        store.delete_project(a.id)
        self.assertEqual([p.id for p in store.list_projects()], [b.id])

    def test_save_bumps_updated_and_reorders(self):
        a = store.create_project("Alpha")
        time.sleep(1.05)
        store.create_project("Bravo")
        time.sleep(1.05)
        a.save()
        self.assertEqual(store.list_projects()[0].id, a.id)

    def test_load_round_trip(self):
        project = store.create_project("Round trip")
        project.voice_id = "v9"
        project.chunks = [store.Chunk(text="one", pause_after=1.5)]
        project.save()
        loaded = store.Project.load(project.id)
        self.assertEqual(loaded.title, "Round trip")
        self.assertEqual(loaded.voice_id, "v9")
        self.assertEqual(loaded.chunks[0].pause_after, 1.5)

    def test_load_unknown_is_keyerror(self):
        with self.assertRaises(KeyError):
            store.Project.load("nope")

    def test_from_dict_ignores_unknown_and_legacy_keys(self):
        project = store.Project.from_dict({
            "title": "t", "voice_id": "v", "future_field": 1,
            "chunks": [{"id": "a", "text": "x", "rendered": "legacy"}],
        })
        self.assertEqual(project.title, "t")
        self.assertEqual(project.chunks[0].text, "x")

    def test_directory_name_is_authoritative(self):
        project = store.create_project("Moved")
        # Simulate a copied directory whose file still carries the old id.
        (store.PROJECTS / "copy").mkdir()
        (store.PROJECTS / "copy" / "project.json").write_text(
            (project.directory / "project.json").read_text())
        self.assertEqual(store.Project.load("copy").id, "copy")

    def test_legacy_migration(self):
        store.LEGACY_PROJECT_FILE.write_text(json.dumps({
            "title": "Old single project", "voice_id": "v1",
            "params": {"temperature": 0.65},
            "chunks": [{"id": "c1", "text": "kept", "pause_after": 1.5}],
        }))
        migrated = store.migrate_legacy()
        self.assertIsNotNone(migrated)
        self.assertEqual(migrated.title, "Old single project")
        # Everything that affects a take's fingerprint must survive, or every
        # chunk of the migrated project would silently read as stale.
        self.assertEqual(migrated.voice_id, "v1")
        self.assertEqual(migrated.params["temperature"], 0.65)
        self.assertEqual(migrated.chunks[0].text, "kept")
        self.assertEqual(migrated.chunks[0].pause_after, 1.5)
        reloaded = store.Project.load(migrated.id)
        self.assertEqual(reloaded.voice_id, "v1")
        self.assertFalse(store.LEGACY_PROJECT_FILE.exists())
        self.assertTrue(store.LEGACY_PROJECT_FILE.with_suffix(".json.migrated").exists())
        self.assertEqual([p.id for p in store.list_projects()], [migrated.id])
        # Running again is a no-op: projects exist now.
        self.assertIsNone(store.migrate_legacy())

    def test_migration_skipped_when_projects_exist(self):
        store.create_project("Already here")
        store.LEGACY_PROJECT_FILE.write_text(json.dumps({"title": "old"}))
        self.assertIsNone(store.migrate_legacy())
        self.assertTrue(store.LEGACY_PROJECT_FILE.exists())

    def test_unused_takes_spans_all_projects(self):
        self.voice("v1")
        a = store.create_project("A")
        a.voice_id = "v1"
        a.chunks = [store.Chunk(text="shared line"), store.Chunk(text="only in a")]
        a.save()
        b = store.create_project("B")
        b.voice_id = "v1"
        b.chunks = [store.Chunk(text="shared line"), store.Chunk(text="only in b")]
        b.save()
        for project in (a, b):
            for chunk in project.chunks:
                project.take_path(chunk).write_bytes(b"x")
        # The shared line produced one file, so three takes are live.
        self.assertEqual(len(list(store.TAKES.glob("*.wav"))), 3)
        orphan = store.TAKES / "deadbeef.wav"
        orphan.write_bytes(b"x")
        self.assertEqual(store.unused_takes(), [orphan])
        # Deleting B must not orphan the shared take A still uses.
        store.delete_project(b.id)
        self.assertEqual({p.name for p in store.unused_takes()},
                         {orphan.name, b.take_path(b.chunks[1]).name})


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
            sf.write(str(a), self._tone(1.0, 0.3, 0.3), self.RATE)
            sf.write(str(b), self._tone(1.0, 0.3, 0.3), self.RATE)
            audio.stitch([(a, 0.5), (b, 0.0)], out, self.RATE)
            data, _ = sf.read(str(out), dtype="float32")
        regions = self._regions(data)
        self.assertEqual(len(regions), 2)
        gap = (regions[1][0] - regions[0][1]) / self.RATE
        self.assertAlmostEqual(gap, 0.5 + 2 * audio.EDGE_MARGIN, delta=0.02)

    def test_loudness_matched_across_takes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            a, b, out = root / "a.wav", root / "b.wav", root / "out.wav"
            sf.write(str(a), self._tone(1.0, 0.05, 0.1), self.RATE)
            sf.write(str(b), self._tone(1.0, 0.40, 0.1), self.RATE)
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
