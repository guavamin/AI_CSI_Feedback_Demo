"""Softmodem output goes through a pipe to its log file (the softmodem never writes to the disk itself)."""

import subprocess
import sys
import time

from common.oai.net.softmodem_launcher import _start_log_pump


def test_pump_copies_output_and_closes(tmp_path):
    log_path = tmp_path / "ue.stdout.log"
    proc = subprocess.Popen([sys.executable, "-c", "import sys\nfor i in range(2000): print('line', i)\n"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    _start_log_pump(proc, log_path.open("ab"), "ue")
    proc.wait(timeout=10)
    t0 = time.monotonic()
    while time.monotonic() - t0 < 5:
        text = log_path.read_text()
        if text.endswith("line 1999\n"):
            break
        time.sleep(0.05)
    assert text.count("\n") == 2000 and text.startswith("line 0\n")
