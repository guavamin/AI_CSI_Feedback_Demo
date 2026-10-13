"""Start / stop / probe nr-softmodem and nr-uesoftmodem processes."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Literal

from common.oai.config import AppConfig, SoftmodemSideConfig

log = logging.getLogger(__name__)

Side = Literal["gnb", "ue"]


class SoftmodemLauncher:
    """Owns softmodem subprocesses (optionally via sudo -n)."""

    def __init__(self, cfg: AppConfig, log_dir: Path | str):
        self.cfg = cfg
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._procs: dict[Side, subprocess.Popen] = {}
        self._stdout_paths: dict[Side, Path] = {}
        # UE --csi-record-path root (H_*.bin under csi_rs_channels/)
        self.csi_record_path: Path | None = None

    def side_cfg(self, side: Side) -> SoftmodemSideConfig:
        return self.cfg.oai.gnb if side == "gnb" else self.cfg.oai.ue

    def build_argv(self, side: Side, mode: str) -> list[str]:
        oai = self.cfg.oai
        sc = self.side_cfg(side)
        binary = sc.binary.strip() or ("nr-softmodem" if side == "gnb" else "nr-uesoftmodem")
        build = Path(oai.build_dir).expanduser().resolve()
        bin_path = Path(binary)
        if not bin_path.is_absolute():
            bin_path = build / binary

        argv: list[str] = [str(bin_path)]

        conf = (sc.conf or "").strip()
        if not conf and side == "gnb":
            conf = (oai.gnb_conf or "").strip()
        if not conf and side == "ue":
            conf = (oai.ue_conf or "").strip()
        if conf:
            argv.extend(["-O", conf])

        argv.extend(str(a) for a in (sc.base_args or []))

        mode_key = (mode or "legacy").strip().lower()
        mode_args = (sc.mode_args or {}).get(mode_key) or (sc.mode_args or {}).get("legacy") or []
        argv.extend(str(a) for a in mode_args)

        model = (sc.model_path or "").strip()
        if model and "--ai-fb-model-path" not in argv:
            argv.extend(["--ai-fb-model-path", model])

        # UE CSI-RS H dumps for the GUI (no OAI stack change; uses existing --csi-record-path).
        # Softmodem cwd is build_dir, so the path MUST be absolute or dumps land under
        # cmake_targets/.../build/data/... while the GUI watches logging.root_dir.
        if (
            side == "ue"
            and oai.csi_record
            and self.csi_record_path is not None
            and "--csi-record-path" not in argv
        ):
            rec = Path(self.csi_record_path).expanduser().resolve()
            argv.extend(["--csi-record-path", str(rec)])

        if oai.use_sudo:
            return ["sudo", "-n", *argv]
        return argv

    def stdout_path(self, side: Side) -> Path:
        return self._stdout_paths.get(side) or (self.log_dir / f"{side}.stdout.log")

    def is_running(self, side: Side) -> bool:
        proc = self._procs.get(side)
        if proc is None:
            return False
        rc = proc.poll()
        if rc is not None:
            log.info("%s exited with code %s", side, rc)
            self._procs.pop(side, None)
            return False
        return True

    def pid(self, side: Side) -> int | None:
        if not self.is_running(side):
            return None
        return self._procs[side].pid

    def _require_radio_device_lib(self, build: Path) -> None:
        """USRP / real-RF needs liboai_device.so; RFsim uses librfsimulator.so instead."""
        if self.cfg.oai.rfsim:
            return
        device = build / "liboai_device.so"
        if device.exists() or device.is_symlink():
            return
        raise FileNotFoundError(
            f"Real-RF start needs {device} but it is missing "
            f"(this build looks RFsim-only; CMake OAI_USRP is often OFF). "
            f"Rebuild with USRP support, e.g.:\n"
            f"  cd {build.parents[2]} && ./build_oai -w USRP --gNB --nrUE -c\n"
            f"Or run configs/lab_rfsim.yaml until that library is present."
        )

    @staticmethod
    def _tail_text(path: Path, max_bytes: int = 1200) -> str:
        try:
            data = path.read_bytes()
        except OSError:
            return ""
        if not data:
            return ""
        text = data[-max_bytes:].decode("utf-8", errors="replace").strip()
        lines = [ln for ln in text.splitlines() if ln.strip()]
        return "\n".join(lines[-12:])

    def start(self, side: Side, mode: str) -> None:
        if self.is_running(side):
            log.info("%s already running (pid=%s)", side, self.pid(side))
            return

        oai = self.cfg.oai
        build = Path(oai.build_dir).expanduser().resolve()
        if not build.is_dir():
            raise FileNotFoundError(f"oai.build_dir not found: {build}")
        self._require_radio_device_lib(build)

        argv = self.build_argv(side, mode)
        out_path = self.log_dir / f"{side}.stdout.log"
        self._stdout_paths[side] = out_path
        out_fh = out_path.open("ab", buffering=0)

        log.info("Starting %s in %s: %s", side, build, " ".join(argv))
        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(build),
                stdout=out_fh,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            out_fh.close()
            raise

        self._procs[side] = proc
        # Brief settle so immediate failures surface (USRP lib load can take >0.4s)
        settle_s = 1.2 if not oai.rfsim else 0.4
        time.sleep(settle_s)
        if proc.poll() is not None:
            out_fh.close()
            self._procs.pop(side, None)
            tail = self._tail_text(out_path)
            detail = f" Last log lines:\n{tail}" if tail else ""
            raise RuntimeError(
                f"{side} exited immediately (code={proc.returncode}). "
                f"See {out_path}. If sudo failed, configure NOPASSWD for the softmodem binary."
                f"{detail}"
            )
        log.info("%s started pid=%s → %s", side, proc.pid, out_path)

    def stop(self, side: Side, timeout_s: float | None = None) -> None:
        proc = self._procs.get(side)
        if proc is None:
            return
        timeout = float(timeout_s if timeout_s is not None else self.cfg.oai.stop_timeout_s)
        log.info("Stopping %s (pid=%s)", side, proc.pid)
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError:
            # Root-owned process group: ask sudo to signal
            subprocess.run(
                ["sudo", "-n", "kill", "-TERM", f"-{os.getpgid(proc.pid)}"],
                check=False,
                capture_output=True,
            )

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            time.sleep(0.2)
        else:
            log.warning("%s did not exit; sending SIGKILL", side)
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
            proc.wait(timeout=3.0)

        self._procs.pop(side, None)
        log.info("%s stopped", side)

    def stop_all(self) -> None:
        # UE first, then gNB (RFsim client before server)
        if self.is_running("ue"):
            self.stop("ue")
        if self.is_running("gnb"):
            self.stop("gnb")
        # Also reap orphans (previous crash / kill -9 left softmodems behind)
        self.kill_stray_softmodems()

    def softmodem_process_names(self) -> set[str]:
        """Basenames for gNB/UE binaries we must never leave running."""
        names = {"nr-softmodem", "nr-uesoftmodem"}
        for side in ("gnb", "ue"):
            binary = (self.side_cfg(side).binary or "").strip()
            if binary:
                names.add(Path(binary).name)
        return names

    def kill_stray_softmodems(self) -> None:
        """
        Kill every matching softmodem process on the host (not only ones we spawned).

        Softmodems are often started via sudo and may outlive a killed GUI; this
        ensures at most one gNB/UE pair remains after cleanup.
        """
        names = sorted(self.softmodem_process_names())
        if not names:
            return
        log.info("Reaping stray softmodems: %s", ", ".join(names))
        for name in names:
            self._pkill_named(name, "TERM")
        # Brief grace period, then force-kill leftovers
        time.sleep(0.6)
        for name in names:
            self._pkill_named(name, "KILL")
        self._procs.clear()

    def _pkill_named(self, name: str, sig: str) -> None:
        """Send ``sig`` (TERM/KILL) to all processes whose name is exactly ``name``."""
        # Prefer sudo when configured — softmodems usually run as root
        cmds: list[list[str]] = []
        pkill = ["pkill", f"-{sig}", "-x", name]
        if self.cfg.oai.use_sudo:
            cmds.append(["sudo", "-n", *pkill])
        cmds.append(list(pkill))
        if not self.cfg.oai.use_sudo:
            # Leftovers from a prior sudo run
            cmds.append(["sudo", "-n", *pkill])

        for argv in cmds:
            try:
                result = subprocess.run(
                    argv, check=False, capture_output=True, text=True
                )
            except FileNotFoundError:
                log.warning("pkill not found; cannot reap %s", name)
                return
            except OSError as exc:
                log.warning("Failed %s: %s", " ".join(argv), exc)
                continue
            # 0 = matched ≥1 process; 1 = none matched
            if result.returncode == 0:
                log.info("Sent SIG%s to %s via: %s", sig, name, " ".join(argv))
                return
            if result.returncode not in (0, 1):
                err = (result.stderr or result.stdout or "").strip()
                if err:
                    log.debug("pkill %s %s → %s: %s", sig, name, result.returncode, err)
