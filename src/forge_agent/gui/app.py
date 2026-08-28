"""Launch the local ForgeAgent GUI (FastAPI + static React app)."""

from __future__ import annotations

import contextlib
import threading
import time
import webbrowser

import uvicorn

from forge_agent.gui.server import create_app


def run_gui(
    *,
    host: str = "127.0.0.1",
    port: int = 8080,
    native: bool = False,
    reload: bool = False,
    show: bool = True,
) -> None:
    """Launch the local-only ForgeAgent GUI."""

    del native
    if show:

        def _open() -> None:
            time.sleep(0.6)
            with contextlib.suppress(Exception):
                webbrowser.open(f"http://{host}:{port}")

        threading.Thread(target=_open, daemon=True).start()
    with contextlib.suppress(KeyboardInterrupt):
        if reload:
            uvicorn.run(
                "forge_agent.gui.server:as_asgi",
                host=host,
                port=port,
                reload=True,
                factory=True,
                log_level="info",
            )
            return
        uvicorn.run(
            create_app(),
            host=host,
            port=port,
            log_level="info",
            timeout_graceful_shutdown=2,
        )


if __name__ in {"__main__", "__mp_main__"}:
    run_gui()
