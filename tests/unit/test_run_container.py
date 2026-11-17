"""Signal cleanup for the Docker run wrapper."""

from __future__ import annotations

import os
import signal
import stat
import subprocess
import time
from pathlib import Path

WRAPPER = Path(__file__).resolve().parents[2] / "scripts" / "run-container.sh"


def test_wrapper_stops_and_removes_container_on_sigterm(tmp_path: Path) -> None:
    fake = tmp_path / "bin" / "docker"
    fake.parent.mkdir()
    log = tmp_path / "docker.log"
    running = tmp_path / "running"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'printf "%s\\n" "$*" >> "$FAKE_DOCKER_LOG"\n'
        'case "$1" in\n'
        "  run)\n"
        '    touch "$FAKE_DOCKER_RUNNING"\n'
        "    echo starting\n"
        '    while [[ -f "$FAKE_DOCKER_RUNNING" ]]; do sleep 0.05; done\n'
        "    ;;\n"
        "  stop|rm)\n"
        '    rm -f "$FAKE_DOCKER_RUNNING"\n'
        "    ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    env = {
        **os.environ,
        "PATH": f"{fake.parent}:{os.environ['PATH']}",
        "FAKE_DOCKER_LOG": str(log),
        "FAKE_DOCKER_RUNNING": str(running),
    }
    container_log = tmp_path / "container.log"
    proc = subprocess.Popen(
        ["bash", str(WRAPPER), "test-container", str(container_log), "--rm", "image"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.time() + 5
    while not running.exists() and time.time() < deadline:
        time.sleep(0.05)
    assert running.exists()
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=5) == 143
    calls = log.read_text(encoding="utf-8")
    assert "run --name test-container --rm image" in calls
    assert "stop --time 10 test-container" in calls
    assert "rm -f test-container" in calls
    assert "starting" in container_log.read_text(encoding="utf-8")
