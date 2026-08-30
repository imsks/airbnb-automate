"""Start/stop the web UI. Used by `make up` / `make down`."""

from __future__ import annotations

import argparse
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from app.config import BASE_DIR

PID_FILE = BASE_DIR / "data" / "web.pid"
LOG_FILE = BASE_DIR / "data" / "web.log"


def flask_port() -> int:
    """Return FLASK_PORT from the environment, defaulting to 5000."""
    raw = (os.getenv("FLASK_PORT") or "5000").strip()
    try:
        port = int(raw)
    except ValueError:
        return 5000
    return port if port > 0 else 5000


def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """Return True if something accepts TCP connections on *host*:*port*."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        return sock.connect_ex((host, port)) == 0


def pid_is_running(pid: int) -> bool:
    """Return True if *pid* exists (signal 0 probe)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def read_pid(pid_file: Path = PID_FILE) -> Optional[int]:
    """Read a PID from *pid_file*, or None if missing/invalid."""
    if not pid_file.exists():
        return None
    try:
        return int(pid_file.read_text(encoding="utf-8").strip())
    except ValueError:
        return None


def start(
    pid_file: Path = PID_FILE,
    log_file: Path = LOG_FILE,
    python: str = sys.executable,
) -> int:
    """Start `run.py` in the background if it is not already running."""
    port = flask_port()
    existing = read_pid(pid_file)
    if existing is not None and pid_is_running(existing):
        print(f"Already running (pid {existing}) — http://localhost:{port}")
        return 0
    if port_in_use(port):
        print(f"Already running — http://localhost:{port}")
        return 0

    pid_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with open(log_file, "a", encoding="utf-8") as log:
        proc = subprocess.Popen(
            [python, str(BASE_DIR / "run.py")],
            cwd=str(BASE_DIR),
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    pid_file.write_text(str(proc.pid), encoding="utf-8")
    time.sleep(0.2)
    if not pid_is_running(proc.pid):
        if pid_file.exists():
            pid_file.unlink()
        print(f"Failed to start — see {log_file}")
        return 1
    print(f"Started Airbnb Automate (pid {proc.pid})")
    print(f"Open http://localhost:{port}")
    return 0


def stop(pid_file: Path = PID_FILE) -> int:
    """Stop the background web UI if a PID file is present."""
    pid = read_pid(pid_file)
    if pid is None:
        print("Not running")
        return 0

    if pid_is_running(pid):
        os.kill(pid, signal.SIGTERM)
        for _ in range(20):
            if not pid_is_running(pid):
                break
            time.sleep(0.05)
        if pid_is_running(pid):
            os.kill(pid, signal.SIGKILL)
        print(f"Stopped (pid {pid})")
    else:
        print(f"Process {pid} already stopped")

    if pid_file.exists():
        pid_file.unlink()
    return 0


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Start or stop the Airbnb Automate web UI")
    parser.add_argument("action", choices=("up", "down"), help="up = start, down = stop")
    args = parser.parse_args(argv)
    if args.action == "up":
        return start()
    return stop()


if __name__ == "__main__":
    sys.exit(main())
