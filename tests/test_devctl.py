"""Tests for make up / make down helpers."""

import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from app import devctl


def test_makefile_delegates_up_and_down():
    makefile = Path(__file__).resolve().parent.parent / "Makefile"
    text = makefile.read_text(encoding="utf-8")
    assert "app.devctl up" in text
    assert "app.devctl down" in text


def test_flask_port_default(monkeypatch):
    monkeypatch.delenv("FLASK_PORT", raising=False)
    assert devctl.flask_port() == 5000


@pytest.mark.parametrize(
    "value, expected",
    [
        ("8080", 8080),
        ("  3000  ", 3000),
        ("", 5000),
        ("nope", 5000),
        ("0", 5000),
        ("-1", 5000),
    ],
)
def test_flask_port_env(monkeypatch, value, expected):
    if value == "":
        monkeypatch.setenv("FLASK_PORT", "")
    else:
        monkeypatch.setenv("FLASK_PORT", value)
    assert devctl.flask_port() == expected


def test_pid_is_running_current_process():
    assert devctl.pid_is_running(os.getpid()) is True


def test_pid_is_running_invalid():
    assert devctl.pid_is_running(0) is False
    assert devctl.pid_is_running(-3) is False
    assert devctl.pid_is_running(999_999_999) is False


def test_read_pid_missing(tmp_path):
    assert devctl.read_pid(tmp_path / "missing.pid") is None


def test_read_pid_invalid(tmp_path):
    pid_file = tmp_path / "web.pid"
    pid_file.write_text("not-a-pid", encoding="utf-8")
    assert devctl.read_pid(pid_file) is None


def test_read_pid_valid(tmp_path):
    pid_file = tmp_path / "web.pid"
    pid_file.write_text("  4242\n", encoding="utf-8")
    assert devctl.read_pid(pid_file) == 4242


def test_port_in_use_true_and_false():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]
    try:
        assert devctl.port_in_use(port) is True
    finally:
        sock.close()
    assert devctl.port_in_use(port) is False


def test_start_writes_pid(tmp_path, capsys, monkeypatch):
    class FakeProc:
        pid = os.getpid()

    monkeypatch.setattr(devctl.subprocess, "Popen", lambda *a, **k: FakeProc())
    monkeypatch.setattr(devctl, "port_in_use", lambda port: False)
    monkeypatch.setenv("FLASK_PORT", "8080")
    pid_file = tmp_path / "web.pid"
    log_file = tmp_path / "nested" / "web.log"

    assert devctl.start(pid_file=pid_file, log_file=log_file) == 0
    assert pid_file.read_text(encoding="utf-8") == str(os.getpid())
    out = capsys.readouterr().out
    assert f"Started Airbnb Automate (pid {os.getpid()})" in out
    assert "http://localhost:8080" in out


def test_start_skips_when_port_in_use(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("FLASK_PORT", "8080")
    monkeypatch.setattr(devctl, "port_in_use", lambda port: True)

    assert devctl.start(pid_file=tmp_path / "web.pid", log_file=tmp_path / "web.log") == 0
    assert "Already running" in capsys.readouterr().out
    assert not (tmp_path / "web.pid").exists()


def test_start_fails_if_child_exits(tmp_path, capsys, monkeypatch):
    class DeadProc:
        pid = 999_999_999

    monkeypatch.setattr(devctl.subprocess, "Popen", lambda *a, **k: DeadProc())
    monkeypatch.setattr(devctl, "port_in_use", lambda port: False)
    pid_file = tmp_path / "web.pid"

    assert devctl.start(pid_file=pid_file, log_file=tmp_path / "web.log") == 1
    assert "Failed to start" in capsys.readouterr().out
    assert not pid_file.exists()


def test_start_skips_when_already_running(tmp_path, capsys, monkeypatch):
    pid_file = tmp_path / "web.pid"
    pid_file.write_text(str(os.getpid()), encoding="utf-8")
    monkeypatch.setenv("FLASK_PORT", "8080")

    assert devctl.start(pid_file=pid_file, log_file=tmp_path / "web.log") == 0
    out = capsys.readouterr().out
    assert "Already running" in out
    assert "8080" in out


def test_start_and_stop_child_process(tmp_path, capsys):
    pid_file = tmp_path / "web.pid"
    log_file = tmp_path / "web.log"
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )
    try:
        pid_file.write_text(str(child.pid), encoding="utf-8")
        assert devctl.pid_is_running(child.pid)

        assert devctl.stop(pid_file=pid_file) == 0
        out = capsys.readouterr().out
        assert f"Stopped (pid {child.pid})" in out
        assert not pid_file.exists()
        child.wait(timeout=2)
        assert child.returncode is not None
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=2)


def test_stop_when_not_running(tmp_path, capsys):
    assert devctl.stop(pid_file=tmp_path / "missing.pid") == 0
    assert "Not running" in capsys.readouterr().out


def test_stop_stale_pid_file(tmp_path, capsys):
    pid_file = tmp_path / "web.pid"
    pid_file.write_text("999999999", encoding="utf-8")
    assert devctl.stop(pid_file=pid_file) == 0
    assert "already stopped" in capsys.readouterr().out
    assert not pid_file.exists()


def test_main_down_dispatches(monkeypatch):
    called = {}

    def fake_stop():
        called["stop"] = True
        return 0

    monkeypatch.setattr(devctl, "stop", fake_stop)
    assert devctl.main(["down"]) == 0
    assert called["stop"] is True


def test_main_up_dispatches(monkeypatch):
    called = {}

    def fake_start():
        called["start"] = True
        return 0

    monkeypatch.setattr(devctl, "start", fake_start)
    assert devctl.main(["up"]) == 0
    assert called["start"] is True
