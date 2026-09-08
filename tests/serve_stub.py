"""Isolated HTTP fixture: real app, synthetic inference, no weights or user data."""

import json
import pathlib
import sys
import tempfile
import types

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


class Wave:
    def __init__(self, data):
        self.data = data

    def squeeze(self, _axis):
        return self

    def cpu(self):
        return self

    def numpy(self):
        return self.data


class StubEngine:
    state, device, loader, progress, error = "ready", "synthetic", "", "", ""
    sample_rate = 24000
    serial = 0

    def warm(self):
        pass

    def cancel(self):
        pass

    def forget_voice(self, _path):
        pass

    def compile_voice(self, _reference, target):
        target.write_bytes(b"synthetic voice")

    def speak(self, text, _voice, _params):
        self.serial += 1
        # A distinguishable take on every render, including unchanged text.
        hz = 220 + self.serial * 31 + len(text)
        return Wave((0.2 * np.sin(np.arange(24000) * 2 * np.pi * hz / 24000)).astype("float32"))


def main():
    from mynah import store

    with tempfile.TemporaryDirectory(prefix="mynah-test-") as tmp:
        store.DATA = pathlib.Path(tmp) / "data"
        store.VOICES = store.DATA / "voices"
        store.TAKES = store.DATA / "takes"
        store.PROJECTS = store.DATA / "projects"
        store.LEGACY_PROJECT_FILE = store.DATA / "project.json"
        engine = types.ModuleType("mynah.engine")
        engine.ENGINE = StubEngine()
        engine.Params = type("Params", (), {"from_dict": staticmethod(lambda data: data)})
        sys.modules["mynah.engine"] = engine
        from mynah.server import app

        voice = store.voice_dir("testvoice")
        voice.mkdir(parents=True)
        (voice / "voice.pt").write_bytes(b"synthetic voice")
        (voice / "meta.json").write_text(json.dumps({
            "id": "testvoice", "name": "Test voice", "status": "ready", "seconds": 8,
        }))
        import uvicorn

        uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="error")


if __name__ == "__main__":
    main()
