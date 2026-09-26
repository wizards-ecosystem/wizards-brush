"""Serve the worker.

Deliberately dull: bind a port and run. The previous entrypoint started a
Cloudflare quick tunnel and ran uvicorn in a daemon thread behind it, because
its target was a notebook with no public route — which meant a tunnel crash
killed a healthy server. A container's host publishes the route.
"""
from __future__ import annotations

import os

import uvicorn

from . import runtime
from .api import app


def main() -> None:
    port = int(os.environ.get("PORT", "8000"))
    url = runtime.public_url(port)
    if url:
        print("=" * 70)
        print(f"  PUBLIC URL  ->  {url}")
        print("  Paste it into the app:  Settings -> Remote GPU")
        print("  This address is public; the shared secret is the only auth.")
        print("=" * 70, flush=True)
    else:
        print(f"[worker] serving on 0.0.0.0:{port} — publish this port with a "
              f"route you control, and keep the shared secret in front of it.",
              flush=True)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")


if __name__ == "__main__":
    main()
