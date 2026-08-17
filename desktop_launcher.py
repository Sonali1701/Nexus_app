"""Windows launcher for the packaged Nexus Resume Uploader."""
from __future__ import annotations

import multiprocessing
import os
import socket
import threading
import time
import urllib.error
import urllib.request
import webbrowser

import uvicorn


HOST = "127.0.0.1"
PORT_SEARCH_LIMIT = 100


def _port_is_available(port: int) -> bool:
    """Return whether Windows will allow a server to bind this local port."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((HOST, port))
        return True
    except OSError:
        return False


def choose_port(preferred: int) -> int:
    """Use the configured port, falling back when it is occupied or reserved."""
    for port in range(preferred, preferred + PORT_SEARCH_LIMIT):
        if _port_is_available(port):
            return port
    raise RuntimeError(
        f"Could not find an available local port from {preferred} to "
        f"{preferred + PORT_SEARCH_LIMIT - 1}."
    )


def _open_browser_when_ready(url: str) -> None:
    status_url = f"{url}/api/status"
    for _ in range(120):
        try:
            with urllib.request.urlopen(status_url, timeout=1) as response:
                if response.status < 500:
                    webbrowser.open(url)
                    return
        except (OSError, urllib.error.URLError):
            time.sleep(0.25)
    print(f"The browser did not open automatically. Open {url} manually.")


def main() -> None:
    # Import after frozen-process initialization so config reads the .env next
    # to the executable before the FastAPI application is constructed.
    from app.config import APP_PORT, ENV_FILE
    from app.main import app

    port = choose_port(APP_PORT)
    url = f"http://{HOST}:{port}"

    print("Nexus Resume Uploader")
    print(f"Configuration: {ENV_FILE}")
    if port != APP_PORT:
        print(f"Port {APP_PORT} was unavailable; using port {port} instead.")
    print(f"Opening {url}")
    print("Keep this window open while using the app. Press Ctrl+C to stop it.")

    if os.getenv("NEXUS_NO_BROWSER", "").strip().lower() not in {"1", "true", "yes"}:
        threading.Thread(
            target=_open_browser_when_ready,
            args=(url,),
            daemon=True,
        ).start()

    uvicorn.run(app, host=HOST, port=port, reload=False, log_level="info")


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
