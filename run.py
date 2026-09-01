#!/usr/bin/env python3
"""Start mynah and open it.

The preflight below exists because the failure it catches is silent otherwise:
the banner prints, uvicorn imports the app, pydantic chokes on modern type
syntax thirty frames deep, and the browser opens on a refused connection. The
message you get instead names the interpreter that is actually running.
"""

import argparse
import glob
import os
import shutil
import socket

# Set before anything from the model stack is imported; see mynah/__init__.py.
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("TQDM_DISABLE", "1")
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

MINIMUM = (3, 10)


def find_working_interpreter() -> str:
    """Look for a Python on this machine that could actually run mynah.

    Only called when the current one cannot. `python` on a pyenv machine is
    whatever the shim points at, which is often not the environment the model
    was installed into — so pointing at a specific working binary is far more
    use than repeating the install instructions.
    """
    candidates: list[str] = []
    for name in ("python3.12", "python3.11", "python3.13", "python3.10", "python3"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    candidates += sorted(glob.glob(
        os.path.expanduser("~/.pyenv/versions/3.1[0-3]*/bin/python")), reverse=True)
    candidates += sorted(glob.glob(str(Path(__file__).resolve().parent / ".venv/bin/python")))

    probe = "import fastapi, uvicorn, chatterbox.tts_turbo"
    for path in dict.fromkeys(candidates):          # de-duplicate, keep order
        try:
            done = subprocess.run([path, "-c", probe], capture_output=True, timeout=60)
        except Exception:  # noqa: BLE001 - a broken candidate is just not the answer
            continue
        if done.returncode == 0:
            return path
    return ""


def preflight() -> str:
    """Return a problem to report, or "" if the environment can run mynah."""
    if sys.version_info < MINIMUM:
        want = ".".join(str(n) for n in MINIMUM)
        message = (
            f"mynah needs Python {want} or newer, but this is "
            f"{sys.version.split()[0]}:\n  {sys.executable}\n"
        )
        print(f"\n{message}\nlooking for an interpreter that works…",
              file=sys.stderr)
        working = find_working_interpreter()
        if working:
            return message + f"\nThis one has everything mynah needs:\n  {working} run.py"
        return message + (
            "\nNo suitable interpreter found. From the project root:\n"
            "  python3.12 -m venv .venv\n"
            "  source .venv/bin/activate\n"
            "  pip install -r requirements.txt\n"
            "  python run.py"
        )
    missing = []
    for module, package in (("fastapi", "fastapi"), ("uvicorn", "uvicorn"),
                            ("soundfile", "soundfile"), ("multipart", "python-multipart")):
        try:
            __import__(module)
        except ImportError:
            missing.append(package)
    if missing:
        return ("Missing dependencies: " + ", ".join(missing) + "\n"
                "  pip install -r requirements.txt")
    try:
        __import__("chatterbox.tts_turbo")
    except ImportError as error:
        # Not fatal: the server still runs and reports this in the UI. But
        # saying it here saves a confused trip through the browser.
        print(f"warning: the TTS model is not importable, so nothing will "
              f"generate.\n  {error}\n  pip install -r requirements.txt\n",
              file=sys.stderr)
    return ""


def port_in_use(host: str, port: int) -> bool:
    """Check before starting rather than after.

    uvicorn catches the bind failure itself and exits, so an OSError never
    reaches this file — and the banner would already have printed, making a
    dead start look like a live one.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
            return False
        except OSError:
            return True


def main() -> int:
    parser = argparse.ArgumentParser(description="self-hosted voice cloning TTS")
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address; the default keeps it off the network")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()

    problem = preflight()
    if problem:
        print(f"\n{problem}\n", file=sys.stderr)
        return 1

    if port_in_use(args.host, args.port):
        print(f"\nPort {args.port} is already in use — mynah may already be "
              f"running at\n  http://localhost:{args.port}\n\n"
              f"Open that, or start another copy with --port {args.port + 1}.\n",
              file=sys.stderr)
        return 1

    import uvicorn

    os.chdir(Path(__file__).resolve().parent)
    url = f"http://{'localhost' if args.host == '127.0.0.1' else args.host}:{args.port}"
    print(f"mynah → {url}")
    if not args.no_open:
        threading.Timer(1.5, webbrowser.open, [url]).start()
    uvicorn.run("mynah.server:app", host=args.host, port=args.port,
                log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
