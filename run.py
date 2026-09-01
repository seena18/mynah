#!/usr/bin/env python3
"""Start mynah and open it."""

import argparse
import threading
import webbrowser

import uvicorn


def main() -> int:
    parser = argparse.ArgumentParser(description="self-hosted voice cloning TTS")
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address; the default keeps it off the network")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()

    url = f"http://{'localhost' if args.host == '127.0.0.1' else args.host}:{args.port}"
    print(f"mynah → {url}")
    if not args.no_open:
        threading.Timer(1.2, webbrowser.open, [url]).start()
    uvicorn.run("mynah.server:app", host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
