"""Exercise checkpoint selection without torch, a GPU, or network downloads."""

import importlib.util
import os
import pathlib
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch


class ModelDownload(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = pathlib.Path(__file__).resolve().parents[1] / "mynah" / "engine.py"
        spec = importlib.util.spec_from_file_location("_download_test_engine", source)
        module = importlib.util.module_from_spec(spec)
        device = types.SimpleNamespace(is_available=lambda: False)
        torch = types.SimpleNamespace(backends=types.SimpleNamespace(mps=device), cuda=device)
        # Keep the real engine and any installed torch untouched for other tests.
        with patch.dict(sys.modules, {spec.name: module, "torch": torch}):
            spec.loader.exec_module(module)
        cls.module = module

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = pathlib.Path(temp.name)
        self.pinned, self.latest = root / "pinned", root / "latest"
        for directory, content in ((self.pinned, b"tested"), (self.latest, b"upstream-changed")):
            directory.mkdir()
            for name in self.module.MODEL_FILES:
                (directory / name).write_bytes(content)
        self.environment = patch.dict(os.environ, {self.module.MODEL_DIR_ENV: ""})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.hub = types.ModuleType("huggingface_hub")

        def directory(revision):
            return self.pinned if revision == self.module.MODEL_REVISION else self.latest

        def cached(_repo, name, revision=None):
            path = directory(revision) / name
            return str(path) if path.exists() else None

        def info(_repo, revision=None, **_kwargs):
            return types.SimpleNamespace(siblings=[
                types.SimpleNamespace(rfilename=path.name, size=path.stat().st_size)
                for path in directory(revision).iterdir()
            ])

        self.hub.try_to_load_from_cache = Mock(side_effect=cached)
        self.hub.snapshot_download = Mock(side_effect=lambda revision=None, **kw: str(directory(revision)))
        self.hub.HfApi = Mock(return_value=types.SimpleNamespace(model_info=Mock(side_effect=info)))
        modules = patch.dict(sys.modules, {"huggingface_hub": self.hub})
        modules.start()
        self.addCleanup(modules.stop)
        self.engine = self.module.Engine()
        self.engine._watch_download = Mock()

    def test_cache_ignores_newer_upstream_weights(self):
        path, size = self.engine.cached()
        self.assertEqual(path, self.pinned)
        self.assertEqual(size, 6 * len(self.module.MODEL_FILES))

    def test_incomplete_pin_does_not_fall_back_to_complete_latest_cache(self):
        (self.pinned / self.module.MODEL_FILES[-1]).unlink()
        self.assertIsNone(self.engine.cached())

    def test_download_selects_tested_revision_anonymously(self):
        self.hub.try_to_load_from_cache.return_value = None
        self.hub.try_to_load_from_cache.side_effect = None
        self.assertEqual(self.engine.download(), self.pinned)
        self.assertIs(self.hub.snapshot_download.call_args.kwargs["token"], False)

    def test_broad_retry_keeps_tested_revision(self):
        self.assertEqual(self.engine.download(patterns=self.module.UPSTREAM_PATTERNS), self.pinned)

    def test_progress_metadata_uses_same_checkpoint(self):
        self.assertEqual(self.engine._expected_bytes(), 6 * len(self.module.MODEL_FILES))
        self.assertIs(self.hub.HfApi().model_info.call_args.kwargs["token"], False)

    def test_manual_directory_still_overrides_hub(self):
        with patch.dict(os.environ, {self.module.MODEL_DIR_ENV: str(self.latest)}):
            self.assertEqual(self.engine.download(), self.latest)
        self.hub.try_to_load_from_cache.assert_not_called()
        self.hub.snapshot_download.assert_not_called()
