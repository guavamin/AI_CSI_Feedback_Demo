"""Discover the UE tunnel IPv4 address (e.g. oaitun_ue1 after attach)."""

from __future__ import annotations

import fcntl
import logging
import math
import re
import socket
import struct
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

_INET_RE = re.compile(r"\binet\s+(\d{1,3}(?:\.\d{1,3}){3})\b")
# OAI UE log: TUN Interface oaitun_ue1 successfully configured, IPv4 10.0.0.19
_TUN_CFG_RE = re.compile(
    r"TUN Interface\s+(\S+)\s+successfully configured,\s+IPv4\s+"
    r"(\d{1,3}(?:\.\d{1,3}){3})",
    re.IGNORECASE,
)


def discover_ue_ipv4(
    iface: str = "oaitun_ue1",
    *,
    ue_log: Path | str | None = None,
) -> str | None:
    """Return the UE TUN IPv4, or None if the PDU interface is not up yet.

    Order:
      1. Configured ``iface`` (ioctl, then ``ip``)
      2. Any ``oaitun*`` interface currently present
      3. Last TUN IPv4 from the UE softmodem log, only if that iface still has it
    """
    name = (iface or "").strip()
    if name:
        addr = _ioctl_ipv4(name) or _ip_cmd_ipv4(name)
        if addr:
            return addr

    scanned = _scan_oaitun_ipv4()
    if scanned:
        return scanned

    if ue_log is not None:
        from_log = _ipv4_from_ue_log(ue_log)
        if from_log:
            log_iface, log_ip = from_log
            live = _ioctl_ipv4(log_iface) or _ip_cmd_ipv4(log_iface)
            if live == log_ip:
                return log_ip
            # Log is stale / TUN torn down
            log.debug(
                "UE log has %s=%s but iface is not live (got %s)",
                log_iface,
                log_ip,
                live,
            )
    return None


def list_oaitun_ifaces() -> list[str]:
    """Return names of present ``oaitun*`` netdevs (may be empty)."""
    try:
        proc = subprocess.run(
            ["ip", "-o", "link", "show"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    out: list[str] = []
    for line in (proc.stdout or "").splitlines():
        # "523: oaitun_ue1: <…>"
        parts = line.split(":", 2)
        if len(parts) < 2:
            continue
        ifname = parts[1].strip().split("@", 1)[0].strip()
        if ifname.startswith("oaitun"):
            out.append(ifname)
    return out


def _scan_oaitun_ipv4() -> str | None:
    for ifname in list_oaitun_ifaces():
        addr = _ioctl_ipv4(ifname) or _ip_cmd_ipv4(ifname)
        if addr:
            return addr
    return None


def _ipv4_from_ue_log(path: Path | str, *, max_bytes: int = 512_000) -> tuple[str, str] | None:
    """Return (iface, ipv4) from the latest TUN configure line in the UE log."""
    p = Path(path)
    try:
        raw = p.read_bytes()
    except OSError:
        return None
    if not raw:
        return None
    text = raw[-max_bytes:].decode("utf-8", errors="replace")
    matches = list(_TUN_CFG_RE.finditer(text))
    if not matches:
        return None
    m = matches[-1]
    return m.group(1), m.group(2)


def _ioctl_ipv4(iface: str) -> str | None:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError:
        return None
    try:
        ifreq = struct.pack("256s", iface[:15].encode("utf-8"))
        res = fcntl.ioctl(sock.fileno(), 0x8915, ifreq)  # SIOCGIFADDR
        return socket.inet_ntoa(res[20:24])
    except OSError:
        return None
    finally:
        sock.close()


def _ip_cmd_ipv4(iface: str) -> str | None:
    try:
        proc = subprocess.run(
            ["ip", "-4", "-o", "addr", "show", "dev", iface],
            check=False,
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.debug("ip addr show %s failed: %s", iface, exc)
        return None
    if proc.returncode != 0:
        return None
    match = _INET_RE.search(proc.stdout or "")
    return match.group(1) if match else None


def verify_ue_ping(
    target_ip: str,
    *,
    duration_s: float = 3.0,
    iface: str | None = None,
    docker_container: str | None = "oai-ext-dn",
) -> tuple[bool, str]:
    """Ping ``target_ip`` for about ``duration_s`` to verify the data path.

    Prefer ping from the DN container (same path as iperf client). Fall back to
    a host ``ping`` (optionally bound to ``iface``) if docker is unavailable.

    Returns ``(ok, detail)``.
    """
    ip = (target_ip or "").strip()
    if not ip:
        return False, "empty UE IP"
    deadline = max(1, int(math.ceil(max(0.1, float(duration_s)))))
    container = (docker_container or "").strip()
    if container and _docker_container_running(container):
        cmd = [
            "docker",
            "exec",
            container,
            "ping",
            "-n",
            "-w",
            str(deadline),
            "-W",
            "1",
            ip,
        ]
        ok, detail = _run_ping(cmd, timeout_s=deadline + 2.0)
        label = f"DN ({container})"
        return ok, (f"{label}: {detail}" if ok else f"{label} ping failed: {detail}")

    if container:
        log.info("DN container %s not running; falling back to host ping", container)

    cmd = ["ping", "-n", "-w", str(deadline), "-W", "1"]
    ifname = (iface or "").strip()
    if ifname:
        cmd.extend(["-I", ifname])
    cmd.append(ip)
    ok, detail = _run_ping(cmd, timeout_s=deadline + 2.0)
    prefix = f"host via {ifname}" if ifname else "host"
    return ok, (f"{prefix}: {detail}" if ok else f"{prefix} ping failed: {detail}")


def _docker_container_running(name: str) -> bool:
    try:
        proc = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", name],
            check=False,
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0 and (proc.stdout or "").strip().lower() == "true"


def _run_ping(cmd: list[str], *, timeout_s: float) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=max(1.0, float(timeout_s)),
        )
    except FileNotFoundError as exc:
        return False, str(exc)
    except subprocess.TimeoutExpired:
        return False, f"timed out: {' '.join(cmd)}"
    except OSError as exc:
        return False, str(exc)
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    summary = out.splitlines()[-1] if out else f"exit {proc.returncode}"
    if proc.returncode == 0:
        return True, summary
    return False, summary or f"exit {proc.returncode}"
