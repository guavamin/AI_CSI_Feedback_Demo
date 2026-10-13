"""Start / stop UDP iperf server (host) and client (often via docker exec)."""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import IO, Literal

from common.oai.config import AppConfig, IperfSideConfig
from common.oai.net.ue_ip import discover_ue_ipv4

log = logging.getLogger(__name__)

Side = Literal["server", "client"]
_CONSOLE_MAX_LINES = 400


class IperfLauncher:
    def __init__(self, cfg: AppConfig, log_dir: Path | str):
        self.cfg = cfg
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._procs: dict[Side, subprocess.Popen] = {}
        self._out_fhs: dict[Side, IO[bytes]] = {}
        self._readers: dict[Side, threading.Thread] = {}
        self._last_ue_ip: str = ""
        self._console: deque[str] = deque(maxlen=_CONSOLE_MAX_LINES)
        self._console_lock = threading.Lock()

    def side_cfg(self, side: Side) -> IperfSideConfig:
        return self.cfg.iperf.server if side == "server" else self.cfg.iperf.client

    def resolve_ue_ip(self, *, required: bool = False, ue_log: Path | str | None = None) -> str:
        """Read current UE tunnel IPv4; optionally require it for launches."""
        iface = (self.cfg.iperf.ue_iface or "oaitun_ue1").strip()
        ip = discover_ue_ipv4(iface, ue_log=ue_log) or ""
        self._last_ue_ip = ip
        if required and not ip:
            raise RuntimeError(
                f"UE IP not found on {iface} (PDU TUN not up yet). "
                f"Radio Attached=YES is not enough — wait for oaitun_ue1, then retry."
            )
        return ip

    def stop_all(self) -> None:
        """Stop tracked server/client and sweep host + DN-container leftovers."""
        # Kill inside the DN container first so UDP load stops even if docker-exec
        # is already dead / orphaned (common with ``docker exec … iperf``).
        self._kill_container_iperf()
        if self.is_running("client"):
            self.stop("client")
        if self.is_running("server"):
            self.stop("server")
        self._kill_stray_iperf()

    def _kill_stray_iperf(self) -> None:
        """Best-effort cleanup of orphaned host + DN→UE iperf processes."""
        patterns = [
            r"iperf .*-c 10\.0\.0\.",
            r"iperf .*-c 12\.",
            r"iperf .* -B 10\.0\.0\.",
            r"iperf .* -B 12\.",
            r"iperf -s -u",
            r"stdbuf .*iperf",
            r"docker exec .*iperf",
        ]
        for pat in patterns:
            try:
                subprocess.run(
                    ["pkill", "-TERM", "-f", pat],
                    check=False,
                    capture_output=True,
                    timeout=3.0,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
        time.sleep(0.2)
        for pat in patterns:
            try:
                subprocess.run(
                    ["pkill", "-KILL", "-f", pat],
                    check=False,
                    capture_output=True,
                    timeout=3.0,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
        self._kill_container_iperf()

    def _kill_container_iperf(self) -> None:
        """SIGKILL iperf inside the lab DN container (oai-ext-dn)."""
        container = self._docker_client_container()
        if not container:
            return
        # pkill/killall inside the container; ignore "no process" failures
        for argv in (
            ["docker", "exec", container, "pkill", "-9", "-f", "iperf"],
            ["docker", "exec", container, "killall", "-9", "iperf"],
        ):
            try:
                subprocess.run(argv, check=False, capture_output=True, timeout=5.0)
            except (OSError, subprocess.TimeoutExpired) as exc:
                log.debug("container iperf kill %s: %s", argv, exc)

    def _docker_client_container(self) -> str | None:
        argv = [str(a) for a in (self.cfg.iperf.client.argv or [])]
        if len(argv) >= 3 and argv[0] == "docker" and argv[1] == "exec":
            # docker exec [-it] NAME …
            i = 2
            while i < len(argv) and argv[i].startswith("-"):
                i += 1
            if i < len(argv):
                return argv[i]
        return None

    def clear_console(self) -> None:
        with self._console_lock:
            self._console.clear()

    def console_text(self) -> str:
        with self._console_lock:
            return "\n".join(self._console)

    def _append_console(self, line: str) -> None:
        text = line.rstrip("\n\r")
        if not text:
            return
        with self._console_lock:
            self._console.append(text)

    def build_argv(self, side: Side, *, ue_ip: str | None = None) -> list[str]:
        sc = self.side_cfg(side)
        argv = [str(a) for a in (sc.argv or [])]
        if not argv:
            raise ValueError(f"iperf.{side}.argv is empty")
        token = (self.cfg.iperf.ue_ip_token or "{ue_ip}").strip() or "{ue_ip}"
        if token in "".join(argv):
            ip = ue_ip if ue_ip is not None else self.resolve_ue_ip(required=True)
            argv = [a.replace(token, ip) for a in argv]
        if sc.use_sudo and argv[0] != "sudo":
            argv = ["sudo", "-n", *argv]
        return self._with_line_buffering(argv)

    @staticmethod
    def _with_line_buffering(argv: list[str]) -> list[str]:
        """Prefer line-buffered stdout so interval reports appear live in the console."""
        if not shutil.which("stdbuf"):
            return argv
        if argv and argv[0] == "stdbuf":
            return argv
        if argv[:2] == ["sudo", "-n"]:
            return ["sudo", "-n", "stdbuf", "-oL", "-eL", *argv[2:]]
        return ["stdbuf", "-oL", "-eL", *argv]

    def is_running(self, side: Side) -> bool:
        proc = self._procs.get(side)
        if proc is None:
            return False
        if proc.poll() is not None:
            log.info("iperf %s exited with code %s", side, proc.returncode)
            self._cleanup_side(side)
            return False
        return True

    def _stream_output(self, side: Side, proc: subprocess.Popen, out_fh: IO[bytes]) -> None:
        try:
            stdout = proc.stdout
            if stdout is None:
                return
            while True:
                raw = stdout.readline()
                if not raw:
                    break
                try:
                    out_fh.write(raw)
                    out_fh.flush()
                except OSError:
                    pass
                if side == "client":
                    self._append_console(raw.decode("utf-8", errors="replace"))
        finally:
            try:
                if proc.stdout is not None:
                    proc.stdout.close()
            except OSError:
                pass

    def _cleanup_side(self, side: Side) -> None:
        self._procs.pop(side, None)
        reader = self._readers.pop(side, None)
        if reader is not None and reader.is_alive() and reader is not threading.current_thread():
            reader.join(timeout=1.0)
        fh = self._out_fhs.pop(side, None)
        if fh is not None:
            try:
                fh.close()
            except OSError:
                pass

    def start(self, side: Side, *, ue_ip: str | None = None) -> None:
        if self.is_running(side):
            log.info("iperf %s already running", side)
            return
        sc = self.side_cfg(side)
        raw = [str(a) for a in (sc.argv or [])]
        token = (self.cfg.iperf.ue_ip_token or "{ue_ip}").strip() or "{ue_ip}"
        needs_ue_ip = token in "".join(raw)
        if needs_ue_ip:
            if ue_ip:
                self._last_ue_ip = ue_ip
            else:
                ue_ip = self.resolve_ue_ip(required=True)
        else:
            ue_ip = ue_ip or ""
        argv = self.build_argv(side, ue_ip=ue_ip if needs_ue_ip else None)
        out_path = self.log_dir / f"iperf_{side}.log"
        out_fh = out_path.open("ab", buffering=0)
        log.info(
            "Starting iperf %s%s: %s",
            side,
            f" (ue_ip={ue_ip})" if ue_ip else "",
            " ".join(argv),
        )
        if side == "client":
            self._append_console(f"--- iperf client start (ue_ip={ue_ip or 'n/a'}) ---")
        try:
            proc = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                bufsize=0,
            )
        except OSError:
            out_fh.close()
            raise
        self._procs[side] = proc
        self._out_fhs[side] = out_fh
        reader = threading.Thread(
            target=self._stream_output,
            args=(side, proc, out_fh),
            name=f"iperf-{side}-out",
            daemon=True,
        )
        self._readers[side] = reader
        reader.start()
        time.sleep(0.3)
        if proc.poll() is not None:
            self._cleanup_side(side)
            raise RuntimeError(
                f"iperf {side} exited immediately (code={proc.returncode}). See {out_path}"
            )
        log.info("iperf %s started pid=%s → %s", side, proc.pid, out_path)

    def stop(self, side: Side, timeout_s: float = 5.0) -> None:
        proc = self._procs.get(side)
        if proc is None:
            return
        log.info("Stopping iperf %s (pid=%s)", side, proc.pid)
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError:
            subprocess.run(
                ["sudo", "-n", "kill", "-TERM", f"-{os.getpgid(proc.pid)}"],
                check=False,
                capture_output=True,
            )
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            time.sleep(0.15)
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                subprocess.run(
                    ["sudo", "-n", "kill", "-KILL", f"-{os.getpgid(proc.pid)}"],
                    check=False,
                    capture_output=True,
                )
            try:
                proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                pass
        if side == "client":
            self._append_console("--- iperf client stopped ---")
        self._cleanup_side(side)
        log.info("iperf %s stopped", side)
