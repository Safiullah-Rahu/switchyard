from __future__ import annotations

import os
import subprocess
import sys


def _cli(home, *args: str) -> subprocess.CompletedProcess[bytes]:
    # a legacy code page on stdout, as Windows gives a piped or redirected process
    env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
    code = "import sys; from switchyard.cli import main; main(sys.argv[1:])"
    return subprocess.run(
        [sys.executable, "-c", code, "--home", str(home), "--mode", "offline", *args],
        capture_output=True,
        env=env,
        timeout=120,
    )


def test_fault_prints_its_arrow_on_a_legacy_code_page(tmp_path):
    home = tmp_path / "home"
    assert _cli(home, "init").returncode == 0
    armed = _cli(home, "fault", "email", "send_email", "timeout_after_write", "--times", "2")
    assert armed.returncode == 0, armed.stderr.decode("utf-8", "replace")
    assert "armed: email.send_email → timeout_after_write ×2" in armed.stdout.decode("utf-8")
    assert _cli(home, "fault", "--clear").returncode == 0
