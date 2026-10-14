"""Load YAML configs with optional extends + environment overrides."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


@dataclass
class FeaturesConfig:
    simulate_metrics: bool = False
    softmodem_control: bool = False
    # Reserved for future OAI telnet AI-FB hooks (not available in stack today)
    live_mode_telnet: bool = False
    # Restart gNB/UE with mode-specific argv when CSI mode changes
    mode_switch_relaunch: bool = False
    # >0: scheduled live switch ("aifb schedule"), both sides change this many frames (10 ms) after
    # arming; 0: immediate "aifb pucch_replace" (older OAI builds)
    live_mode_lead_frames: int = 0
    record_run: bool = True
    # Offline UE CSI-RS H magnitude (per RX-TX link) for the CSI-RS tab
    simulate_csirs: bool = True
    # If true, start stub/softmodem gNB+UE automatically on launch
    auto_start_modems: bool = False
    # Host iperf -s + docker-exec iperf client controls
    iperf_control: bool = False


@dataclass
class NetworkConfig:
    gnb_host: str = "127.0.0.1"
    ue_host: str = "127.0.0.1"
    gnb_telnet_port: int = 9090
    ue_telnet_port: int = 9091
    metrics_port: int = 9100
    # CSI-RS |H| UDP (Option A forwarder → GUI); bind on GUI host
    csirs_udp_bind: str = "0.0.0.0"
    csirs_udp_port: int = 9101


@dataclass
class SoftmodemSideConfig:
    binary: str = ""
    conf: str = ""
    model_path: str = ""
    base_args: list[str] = field(default_factory=list)
    # keyed by legacy | csinet — argv appended after base_args
    mode_args: dict[str, list[str]] = field(default_factory=dict)
    # appended after base_args: lets a profile add arguments without repeating the inherited base_args
    extra_args: list[str] = field(default_factory=list)


@dataclass
class OaiConfig:
    build_dir: str = ""
    use_sudo: bool = True
    stats_log: str = "nrMAC_stats.log"
    stop_timeout_s: float = 8.0
    gnb_boot_s: float = 3.0
    mode_settle_s: float = 1.5
    gnb: SoftmodemSideConfig = field(default_factory=SoftmodemSideConfig)
    ue: SoftmodemSideConfig = field(default_factory=SoftmodemSideConfig)
    # Legacy flat fields (still honored if side.conf empty)
    gnb_conf: str = ""
    ue_conf: str = ""
    rfsim: bool = True
    # When True, UE is started with --csi-record-path (H_*.bin for CSI-RS GUI)
    csi_record: bool = False
    # Keep this many newest H_*.bin files; older dumps are deleted while running
    csi_record_keep_h: int = 2
    # Parent folder of the per-session record dirs ("" = <logging.root_dir>/csi_record). Put it in RAM
    # (/dev/shm/...) so the UE's real-time thread never waits for the disk when writing a dump.
    csi_record_dir: str = ""
    # How the GUI obtains CSI-RS |H|: file (local Phase 1), udp (forwarder), both
    csi_transport: str = "file"
    # Destination host for the local/remote Option-A forwarder (port = network.csirs_udp_port)
    csi_udp_dest_host: str = "127.0.0.1"


@dataclass
class IperfSideConfig:
    use_sudo: bool = False
    argv: list[str] = field(default_factory=list)


@dataclass
class IperfConfig:
    # Start client this many seconds after server (DN→UE UDP)
    client_delay_s: float = 1.0
    # Host interface that receives the UE PDU IP after attach (OAI TUN)
    ue_iface: str = "oaitun_ue1"
    # Replaced in server/client argv when present (e.g. -B {ue_ip}, -c {ue_ip})
    ue_ip_token: str = "{ue_ip}"
    # While "Start traffic" is desired: wait this long after (re)starting UE for attach
    # (legacy blind timer — prefer stage timeouts below when bringup monitor is on)
    ue_boot_s: float = 20.0
    # After radio attach, wait this long for oaitun_ue1 before *optionally* restarting UE.
    # 0 = never restart just for a missing TUN IP (Attached≠PDU; default avoids thrashing).
    # Ignored when stage-aware bring-up monitor is enabled (uses tun_timeout_s instead).
    ip_wait_s: float = 0.0
    # Supervisor poll while traffic is desired (UE health / IP / iperf restart)
    supervise_period_s: float = 1.0
    # Min gap between UE re-inits when attach keeps failing
    ue_restart_cooldown_s: float = 2.0
    # After live PDU/TUN IP appears, wait this long before ping/iperf (iface settle)
    ip_settle_s: float = 0.0
    # Ping UE IP for this many seconds to verify the link before starting iperf (0=skip)
    ip_ping_s: float = 3.0
    # Prefer ping from this docker DN container (empty = host ping only)
    ip_ping_dn_container: str = "oai-ext-dn"
    # Brief debounce after IP disappears before starting PDU-retry waits
    ip_lost_grace_s: float = 2.0
    # After PDU was up / iperf initiated: wait this long per attempt for PDU to return
    pdu_retry_wait_s: float = 5.0
    # Failed PDU-recovery waits (or iperf starts) before UE re-init
    pdu_retry_max: int = 3
    # Stage-aware UE bring-up (SSB→RACH→RRC→PDU→TUN) from UE softmodem log
    bringup_monitor: bool = True
    bringup_ssb_timeout_s: float = 15.0
    bringup_rach_timeout_s: float = 20.0
    bringup_rrc_timeout_s: float = 15.0
    bringup_pdu_timeout_s: float = 60.0
    bringup_tun_timeout_s: float = 20.0
    server: IperfSideConfig = field(default_factory=IperfSideConfig)
    client: IperfSideConfig = field(default_factory=IperfSideConfig)


@dataclass
class CsiConfig:
    default_mode: str = "legacy"  # legacy | csinet
    csi_period_s: float = 0.04
    # Simulated / expected UE CSI-RS geometry (matches OAI scope layout: rx × port × subc)
    n_rx: int = 1
    n_ports: int = 2
    n_subc: int = 128


@dataclass
class PlotConfig:
    bler_ylim: list[float] = field(default_factory=lambda: [0.0, 1.0])
    tput_ylim: list[float] = field(default_factory=lambda: [0.0, 50.0])
    max_points: int = 256
    # Trailing window for thick avg traces + title averages on real-time plots
    avg_window: int = 32
    # Re-fit BLER/throughput axes from samples (realtime Y + CDF X)
    auto_scale_bler_tput: bool = True
    # Recompute limits when sample count crosses multiples of this step
    auto_scale_every_n: int = 8
    # Extra room above peak/mean so curves are not flush with the axis edge
    axis_headroom: float = 0.20
    csirs_mag_ylim: list[float] = field(default_factory=lambda: [0.0, 1.2])
    # CSI-RS |H| x-axis: "native" = OAI FFT bin 0..N-1; "signed" = fftshift-style −N/2..N/2−1 (DC at 0)
    csirs_freq_axis: str = "native"
    # When signed: draw DC (f=0) and FFT-edge guard markers (±N/2)
    csirs_show_markers: bool = True
    # OAI FILTER_MARGIN stripped before signed remap (see csi_rx.c)
    csirs_filter_margin: int = 32
    # Distribution tab
    dist_max_samples: int = 5000
    dist_cdf_points: int = 64
    dist_avg_max_points: int = 512
    # CDF plot x-axis limits (metric on X, CDF on Y) — used as fallback / floor when auto-scale is on
    dist_bler_xlim: list[float] = field(default_factory=lambda: [0.0, 1.0])
    dist_tput_xlim: list[float] = field(default_factory=lambda: [0.0, 50.0])
    dist_cqi_xlim: list[float] = field(default_factory=lambda: [0.0, 15.0])
    cqi_ylim: list[float] = field(default_factory=lambda: [0.0, 15.0])
    # RI plotted as layers (OAI RI+1); 0–4 covers typical ≤4-layer MIMO with headroom
    ri_ylim: list[float] = field(default_factory=lambda: [0.0, 4.0])
    # PMI dual-axis limits
    pmi_x1_ylim: list[float] = field(default_factory=lambda: [-1.0, 1.0])
    pmi_x2_ylim: list[float] = field(default_factory=lambda: [-1.0, 4.0])


@dataclass
class LoggingConfig:
    root_dir: str = "data"


@dataclass
class DevicesConfig:
    # Prefer geometric sans (Roboto) for InterDigital-like demo typography
    font_path: str = "/usr/share/fonts/truetype/roboto/unhinted/RobotoTTF/Roboto-Regular.ttf"


@dataclass
class GuiConfig:
    display: str = ""
    auto_detect_display: bool = True
    auto_xhost: bool = False
    xhost_user: str = ""
    # CSI-RS tab checkbox default (plot panel stays hidden until enabled)
    csirs_view_enabled: bool = False
    font_size: int = 22
    wi_lab_url: str = "https://www.wi-lab.net/"
    # Overview illustration shown when CSI mode is legacy
    legacy_mode_image: str = "images/legacy.png"
    # Overview illustration shown when CSI mode is csinet
    csinet_mode_image: str = "images/ai_model.png"
    # When true: tabs/buttons/Traffic header/radio+checkbox marks follow CSI mode
    # (legacy=cyan, AI=amber). When false: InterDigital logo cyan for both modes.
    mode_based_colors: bool = True


@dataclass
class AppConfig:
    profile: str = "offline_sim"
    features: FeaturesConfig = field(default_factory=FeaturesConfig)
    network: NetworkConfig = field(default_factory=NetworkConfig)
    oai: OaiConfig = field(default_factory=OaiConfig)
    iperf: IperfConfig = field(default_factory=IperfConfig)
    csi: CsiConfig = field(default_factory=CsiConfig)
    plot: PlotConfig = field(default_factory=PlotConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    devices: DevicesConfig = field(default_factory=DevicesConfig)
    gui: GuiConfig = field(default_factory=GuiConfig)
    station: dict[str, Any] = field(default_factory=dict)  # separate-laptops station settings, as written


def _from_mapping(cls, data: dict[str, Any] | None):
    if not data:
        return cls()
    allowed = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in allowed})


def _load_softmodem_side(data: dict[str, Any] | None, *, default_binary: str) -> SoftmodemSideConfig:
    if not data:
        return SoftmodemSideConfig(binary=default_binary)
    mode_args_raw = data.get("mode_args") or {}
    mode_args: dict[str, list[str]] = {}
    if isinstance(mode_args_raw, dict):
        for key, val in mode_args_raw.items():
            if isinstance(val, list):
                mode_args[str(key)] = [str(x) for x in val]
    base_args = data.get("base_args") or []
    if not isinstance(base_args, list):
        base_args = []
    return SoftmodemSideConfig(
        binary=str(data.get("binary") or default_binary),
        conf=str(data.get("conf") or ""),
        model_path=str(data.get("model_path") or ""),
        base_args=[str(x) for x in base_args],
        mode_args=mode_args,
        extra_args=[str(x) for x in (data.get("extra_args") or [])],
    )


def _load_oai(data: dict[str, Any] | None) -> OaiConfig:
    if not data:
        return OaiConfig()
    gnb = _load_softmodem_side(data.get("gnb"), default_binary="nr-softmodem")
    ue = _load_softmodem_side(data.get("ue"), default_binary="nr-uesoftmodem")
    # Flat legacy conf fields fill side.conf when empty
    flat_gnb_conf = str(data.get("gnb_conf") or "")
    flat_ue_conf = str(data.get("ue_conf") or "")
    if not gnb.conf and flat_gnb_conf:
        gnb.conf = flat_gnb_conf
    if not ue.conf and flat_ue_conf:
        ue.conf = flat_ue_conf
    return OaiConfig(
        build_dir=str(data.get("build_dir") or ""),
        use_sudo=bool(data.get("use_sudo", True)),
        stats_log=str(data.get("stats_log") or "nrMAC_stats.log"),
        stop_timeout_s=float(data.get("stop_timeout_s", 8.0)),
        gnb_boot_s=float(data.get("gnb_boot_s", 3.0)),
        mode_settle_s=float(data.get("mode_settle_s", 1.5)),
        gnb=gnb,
        ue=ue,
        gnb_conf=flat_gnb_conf,
        ue_conf=flat_ue_conf,
        rfsim=bool(data.get("rfsim", True)),
        csi_record=bool(data.get("csi_record", False)),
        csi_record_keep_h=max(1, int(data.get("csi_record_keep_h", 2))),
        csi_record_dir=str(data.get("csi_record_dir") or ""),
        csi_transport=str(data.get("csi_transport") or "file").strip().lower() or "file",
        csi_udp_dest_host=str(data.get("csi_udp_dest_host") or "127.0.0.1").strip()
        or "127.0.0.1",
    )


def _load_iperf_side(data: dict[str, Any] | None) -> IperfSideConfig:
    if not data:
        return IperfSideConfig()
    argv = data.get("argv") or []
    if not isinstance(argv, list):
        argv = []
    return IperfSideConfig(
        use_sudo=bool(data.get("use_sudo", False)),
        argv=[str(x) for x in argv],
    )


def _load_iperf(data: dict[str, Any] | None) -> IperfConfig:
    if not data:
        return IperfConfig()
    return IperfConfig(
        client_delay_s=float(data.get("client_delay_s", 1.0)),
        ue_iface=str(data.get("ue_iface") or "oaitun_ue1"),
        ue_ip_token=str(data.get("ue_ip_token") or "{ue_ip}"),
        ue_boot_s=float(data.get("ue_boot_s", 20.0)),
        ip_wait_s=float(data.get("ip_wait_s", 0.0)),
        supervise_period_s=float(data.get("supervise_period_s", 1.0)),
        ue_restart_cooldown_s=float(data.get("ue_restart_cooldown_s", 2.0)),
        ip_settle_s=float(data.get("ip_settle_s", 0.0)),
        ip_ping_s=float(data.get("ip_ping_s", 3.0)),
        ip_ping_dn_container=str(data.get("ip_ping_dn_container") or "oai-ext-dn"),
        ip_lost_grace_s=float(data.get("ip_lost_grace_s", 2.0)),
        pdu_retry_wait_s=float(data.get("pdu_retry_wait_s", 5.0)),
        pdu_retry_max=int(data.get("pdu_retry_max", 3)),
        bringup_monitor=bool(data.get("bringup_monitor", True)),
        bringup_ssb_timeout_s=float(data.get("bringup_ssb_timeout_s", 15.0)),
        bringup_rach_timeout_s=float(data.get("bringup_rach_timeout_s", 20.0)),
        bringup_rrc_timeout_s=float(data.get("bringup_rrc_timeout_s", 15.0)),
        bringup_pdu_timeout_s=float(data.get("bringup_pdu_timeout_s", 60.0)),
        bringup_tun_timeout_s=float(data.get("bringup_tun_timeout_s", 20.0)),
        server=_load_iperf_side(data.get("server")),
        client=_load_iperf_side(data.get("client")),
    )


def _load_yaml_file(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if "extends" in raw:
        parent_name = raw.pop("extends")
        parent_path = path.parent / parent_name
        parent = _load_yaml_file(parent_path)
        raw = _deep_merge(parent, raw)
    return raw


_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_vars(raw: dict[str, Any]) -> dict[str, Any]:
    """Expand ${NAME} in every string of the profile (this project's addition).

    Values come from the environment first, then from the profile's (merged) ``vars:`` block; ${REPO} is the
    repository root. Lets one profile run on any laptop: e.g. ``OAI_DIR: ${REPO}/../openairinterface5g_gui``
    as the default, overridden with ``OAI_DIR=/opt/oai`` in the environment.
    """
    defaults = {str(k): str(v) for k, v in (raw.pop("vars", None) or {}).items()}
    defaults.setdefault("REPO", str(_repo_root()))
    resolved: dict[str, str] = {}

    def resolve(name: str, chain: tuple[str, ...] = ()) -> str:
        if name in resolved:
            return resolved[name]
        if name in chain:
            raise ValueError(f"profile variable cycle: {' -> '.join(chain + (name,))}")
        if name in os.environ:
            value = os.environ[name]
        elif name in defaults:
            value = defaults[name]
        else:
            raise KeyError(f"profile uses ${{{name}}} but neither the environment nor vars: defines it")
        value = _VAR_RE.sub(lambda m: resolve(m.group(1), chain + (name,)), value)
        resolved[name] = os.path.normpath(value) if "/" in value and "://" not in value else value
        return resolved[name]

    def walk(node: Any) -> Any:
        if isinstance(node, str):
            return _VAR_RE.sub(lambda m: resolve(m.group(1)), node)
        if isinstance(node, list):
            return [walk(v) for v in node]
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items()}
        return node

    return walk(raw)


def _apply_env(cfg: AppConfig) -> AppConfig:
    """Optional overrides: PYOAICSINET_GNB_HOST, PYOAICSINET_DATA_ROOT, etc."""
    mapping = {
        "PYOAICSINET_GNB_HOST": ("network", "gnb_host", str),
        "PYOAICSINET_UE_HOST": ("network", "ue_host", str),
        "PYOAICSINET_GNB_TELNET_PORT": ("network", "gnb_telnet_port", int),
        "PYOAICSINET_UE_TELNET_PORT": ("network", "ue_telnet_port", int),
        "PYOAICSINET_METRICS_PORT": ("network", "metrics_port", int),
        "PYOAICSINET_CSIRS_UDP_PORT": ("network", "csirs_udp_port", int),
        "PYOAICSINET_CSIRS_UDP_BIND": ("network", "csirs_udp_bind", str),
        "PYOAICSINET_DATA_ROOT": ("logging", "root_dir", str),
        "PYOAICSINET_BUILD_DIR": ("oai", "build_dir", str),
        "PYOAICSINET_DISPLAY": ("gui", "display", str),
        "PYOAICSINET_GNB_MODEL": ("oai", "gnb_model", str),  # handled below
        "PYOAICSINET_UE_MODEL": ("oai", "ue_model", str),
    }
    for env_key, (section, attr, caster) in mapping.items():
        if env_key not in os.environ:
            continue
        if attr == "gnb_model":
            cfg.oai.gnb.model_path = os.environ[env_key]
            continue
        if attr == "ue_model":
            cfg.oai.ue.model_path = os.environ[env_key]
            continue
        setattr(getattr(cfg, section), attr, caster(os.environ[env_key]))
    return cfg


def load_config(path: str | Path | None = None) -> AppConfig:
    if path is None:
        path = _repo_root() / "configs" / "offline_sim.yaml"
    path = Path(path)
    if not path.is_absolute():
        candidate = Path.cwd() / path
        path = candidate if candidate.exists() else _repo_root() / path
    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path}")

    raw = _expand_vars(_load_yaml_file(path))
    cfg = AppConfig(
        profile=raw.get("profile", path.stem),
        features=_from_mapping(FeaturesConfig, raw.get("features")),
        network=_from_mapping(NetworkConfig, raw.get("network")),
        oai=_load_oai(raw.get("oai")),
        iperf=_load_iperf(raw.get("iperf")),
        csi=_from_mapping(CsiConfig, raw.get("csi")),
        plot=_from_mapping(PlotConfig, raw.get("plot")),
        logging=_from_mapping(LoggingConfig, raw.get("logging")),
        devices=_from_mapping(DevicesConfig, raw.get("devices")),
        gui=_from_mapping(GuiConfig, raw.get("gui")),
        station=dict(raw.get("station") or {}),
    )
    return _apply_env(cfg)


def describe_features(cfg: AppConfig) -> list[str]:
    lines = [f"profile={cfg.profile}"]
    for f in fields(cfg.features):
        lines.append(f"  {f.name}={getattr(cfg.features, f.name)}")
    return lines
