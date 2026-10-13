"""Unit tests for lab wiring (launcher argv, MAC stats parse, config)."""

from pathlib import Path

from common.oai.config import load_config
from common.oai.net.metrics_ingest import MacStatsIngest
from common.oai.net.softmodem_launcher import SoftmodemLauncher

FIXTURES = Path(__file__).resolve().parent / "fixtures"


SAMPLE_MAC_STATS = """
UE RNTI 1234 CU-UE-ID 0 in-sync
UE 1234: CQI 11, RI 1, PMI (42,0)
UE 1234: dlsch_rounds 10/1/0/0, dlsch_errors 2, pucch0_DTX 0, BLER 0.01234 MCS (0) 10 CCE fail 0
UE 1234: MAC:    TX         125000 RX          8000 bytes
"""


def test_lab_config_loads():
    cfg = load_config(FIXTURES / "lab_rfsim.yaml")
    assert cfg.profile == "lab_rfsim"
    assert cfg.features.softmodem_control is True
    assert cfg.features.simulate_metrics is False
    assert cfg.features.live_mode_telnet is True
    assert cfg.features.mode_switch_relaunch is False
    assert cfg.oai.gnb.binary == "nr-softmodem"
    assert cfg.oai.ue.binary == "nr-uesoftmodem"
    assert "csinet" in cfg.oai.gnb.mode_args
    assert "--ai-fb-pucch-replace" in cfg.oai.gnb.mode_args["csinet"]
    assert "--telnetsrv" in cfg.oai.gnb.base_args
    assert "--ai-fb-live-mode-switch" in cfg.oai.gnb.base_args
    assert cfg.oai.gnb.base_args[cfg.oai.gnb.base_args.index("--ai-fb-live-mode-switch") + 1] == "1"


def test_build_argv_gnb_csinet(tmp_path):
    cfg = load_config(FIXTURES / "lab_rfsim.yaml")
    cfg.oai.use_sudo = False
    cfg.oai.build_dir = str(tmp_path)
    (tmp_path / "nr-softmodem").write_text("")
    launcher = SoftmodemLauncher(cfg, tmp_path / "logs")
    argv = launcher.build_argv("gnb", "csinet")
    assert argv[0].endswith("nr-softmodem")
    assert "-O" in argv
    assert "--rfsim" in argv
    assert "--ai-fb-pucch-replace" in argv
    idx = argv.index("--ai-fb-pucch-replace")
    assert argv[idx + 1] == "1"
    assert "--ai-fb-model-path" in argv


def test_build_argv_ue_legacy(tmp_path):
    cfg = load_config(FIXTURES / "lab_rfsim.yaml")
    cfg.oai.use_sudo = True
    cfg.oai.build_dir = str(tmp_path)
    launcher = SoftmodemLauncher(cfg, tmp_path / "logs")
    argv = launcher.build_argv("ue", "legacy")
    assert argv[:2] == ["sudo", "-n"]
    assert "-C" in argv
    assert "--band" in argv
    assert "-E" not in argv
    assert "--if_freq" not in argv
    idx = argv.index("--ai-fb-pucch-replace")
    assert argv[idx + 1] == "0"
    assert "--ai-fb-impl-mode" in argv
    assert argv[argv.index("--ai-fb-impl-mode") + 1] == "6"


def test_lab_usrp_config_and_argv(tmp_path):
    cfg = load_config(FIXTURES / "lab_usrp.yaml")
    assert cfg.profile == "lab_usrp"
    assert cfg.oai.rfsim is False
    assert cfg.features.softmodem_control is True
    cfg.oai.use_sudo = False
    cfg.oai.build_dir = str(tmp_path)
    (tmp_path / "nr-softmodem").write_text("")
    (tmp_path / "nr-uesoftmodem").write_text("")
    launcher = SoftmodemLauncher(cfg, tmp_path / "logs")

    gnb = launcher.build_argv("gnb", "csinet")
    assert "--rfsim" not in gnb
    assert "--rfsimulator.[0].serveraddr" not in gnb
    assert "-E" in gnb
    assert "--continuous-tx" in gnb
    assert gnb[gnb.index("--clock-source") + 1] == "1"
    assert gnb[gnb.index("--time-source") + 1] == "1"
    assert "--telnetsrv" in gnb
    assert "--ai-fb-live-mode-switch" in gnb
    assert gnb[gnb.index("--ai-fb-pucch-replace") + 1] == "1"
    assert "--ai-fb-model-path" in gnb

    ue = launcher.build_argv("ue", "legacy")
    assert "--rfsim" not in ue
    assert "-E" in ue
    assert "--telnetsrv" in ue
    assert ue[ue.index("--telnetsrv.listenport") + 1] == "9091"
    assert ue[ue.index("--telnetsrv.shrmod") + 1] == "ciUE"
    assert ue[ue.index("--if_freq") + 1] == "2500000000"
    assert ue[ue.index("--usrp-args") + 1] == "serial=31F3CC3"
    assert ue[ue.index("--clock-source") + 1] == "1"
    assert ue[ue.index("--time-source") + 1] == "1"
    assert ue[ue.index("--ai-fb-pucch-replace") + 1] == "0"


def test_apply_mode_via_telnet_order(monkeypatch):
    from common.oai.net import mode_control as mc

    calls: list[tuple[str, int, str]] = []

    class FakeProbe:
        def __init__(self, host, port, timeout_s=2.0):
            self.host = host
            self.port = port

        def reachable(self):
            return True

        def send_command(self, command):
            calls.append((self.host, self.port, command))
            return "aifb: pucch_replace 0 -> 1\n"

    monkeypatch.setattr(mc, "TelnetProbe", FakeProbe)
    mc.apply_mode_via_telnet(
        mode="csinet",
        gnb_host="127.0.0.1",
        gnb_port=9093,
        ue_host="127.0.0.1",
        ue_port=9091,
    )
    # Enabling AI: gNB (9093) first so it stops booking HARQ-ACK into CSI PUCCHs, then UE (9091).
    assert calls[0] == ("127.0.0.1", 9093, "aifb pucch_replace 1")
    assert calls[1] == ("127.0.0.1", 9091, "aifb pucch_replace 1")
    calls.clear()
    mc.apply_mode_via_telnet(
        mode="legacy",
        gnb_host="127.0.0.1",
        gnb_port=9093,
        ue_host="127.0.0.1",
        ue_port=9091,
    )
    # Disabling AI: UE first so it never sends the 52-bit payload into a legacy-sized PUCCH.
    assert calls[0] == ("127.0.0.1", 9091, "aifb pucch_replace 0")
    assert calls[1] == ("127.0.0.1", 9093, "aifb pucch_replace 0")
    assert mc.pucch_replace_for_mode("legacy") == 0
    assert mc.pucch_replace_for_mode("csinet") == 1


def test_usrp_start_requires_oai_device_lib(tmp_path):
    cfg = load_config(FIXTURES / "lab_usrp.yaml")
    cfg.oai.use_sudo = False
    cfg.oai.build_dir = str(tmp_path)
    (tmp_path / "nr-softmodem").write_text("")
    launcher = SoftmodemLauncher(cfg, tmp_path / "logs")
    try:
        launcher.start("gnb", "legacy")
        assert False, "expected missing liboai_device.so to fail"
    except FileNotFoundError as exc:
        assert "liboai_device.so" in str(exc)


def test_iperf_argv_from_lab_config():
    cfg = load_config(FIXTURES / "lab_rfsim.yaml")
    assert cfg.features.iperf_control is True
    assert cfg.iperf.ue_iface == "oaitun_ue1"
    assert cfg.iperf.server.argv[:3] == ["iperf", "-s", "-u"]
    assert "{ue_ip}" in cfg.iperf.server.argv
    assert cfg.iperf.client.argv[:3] == ["docker", "exec", "oai-ext-dn"]
    assert "-it" not in cfg.iperf.client.argv
    assert cfg.iperf.client.argv[-1] == "{ue_ip}"
    from common.oai.net.iperf_control import IperfLauncher

    launcher = IperfLauncher(cfg, Path("/tmp/iperf_test_logs"))
    srv = launcher.build_argv("server", ue_ip="10.0.0.19")
    cli = launcher.build_argv("client", ue_ip="10.0.0.19")
    # stdbuf may wrap argv for live console line buffering
    assert "iperf" in srv
    assert srv[-1] == "10.0.0.19"
    assert "-B" in srv
    assert "docker" in cli
    assert cli[-1] == "10.0.0.19"
    assert "-c" in cli


def test_iperf_console_buffer(tmp_path):
    from common.oai.net.iperf_control import IperfLauncher

    cfg = load_config(FIXTURES / "lab_rfsim.yaml")
    launcher = IperfLauncher(cfg, tmp_path)
    launcher._append_console("[  3]  0.0- 1.0 sec  11.9 MBytes  99.8 Mbits/sec")
    launcher._append_console("")
    text = launcher.console_text()
    assert "99.8 Mbits/sec" in text
    launcher.clear_console()
    assert launcher.console_text() == ""


def test_discover_ue_ipv4_from_ip_output(monkeypatch):
    from common.oai.net import ue_ip

    class FakeProc:
        returncode = 0
        stdout = (
            "513: oaitun_ue1    inet 10.0.0.19/24 scope global oaitun_ue1\\n"
        )

    def fake_run(*args, **kwargs):
        return FakeProc()

    monkeypatch.setattr(ue_ip, "_ioctl_ipv4", lambda iface: None)
    monkeypatch.setattr(ue_ip.subprocess, "run", fake_run)
    assert ue_ip.discover_ue_ipv4("oaitun_ue1") == "10.0.0.19"


def test_softmodem_process_names():
    cfg = load_config(FIXTURES / "lab_rfsim.yaml")
    launcher = SoftmodemLauncher(cfg, Path("/tmp/softmodem_test_logs"))
    names = launcher.softmodem_process_names()
    assert "nr-softmodem" in names
    assert "nr-uesoftmodem" in names


def test_kill_stray_softmodems_invokes_pkill(monkeypatch, tmp_path):
    cfg = load_config(FIXTURES / "lab_rfsim.yaml")
    cfg.oai.use_sudo = True
    launcher = SoftmodemLauncher(cfg, tmp_path)
    calls: list[list[str]] = []

    class FakeResult:
        returncode = 1  # no matching processes
        stdout = ""
        stderr = ""

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return FakeResult()

    monkeypatch.setattr(
        "common.oai.net.softmodem_launcher.subprocess.run", fake_run
    )
    monkeypatch.setattr(
        "common.oai.net.softmodem_launcher.time.sleep", lambda *_: None
    )
    launcher.kill_stray_softmodems()
    # sudo pkill for each name × TERM then KILL
    assert any(c[:4] == ["sudo", "-n", "pkill", "-TERM"] for c in calls)
    assert any(c[:4] == ["sudo", "-n", "pkill", "-KILL"] for c in calls)
    assert any("-x" in c and "nr-softmodem" in c for c in calls)
    assert any("-x" in c and "nr-uesoftmodem" in c for c in calls)


def test_mac_stats_parse(tmp_path):
    path = tmp_path / "nrMAC_stats.log"
    path.write_text(SAMPLE_MAC_STATS)
    ingest = MacStatsIngest(path)
    s1 = ingest.poll()
    assert s1 is not None
    assert s1.attached is True
    assert s1.rnti == "1234"
    assert s1.cqi == 11
    assert s1.ri == 1
    assert s1.pmi_x1 == 42
    assert abs(s1.bler - 0.01234) < 1e-9
    assert s1.tx_bytes == 125000
    assert s1.throughput_fresh is False

    # Same file again (no new TX) must not invent a 0 Mbps sample
    import time

    time.sleep(0.12)
    s_stale = ingest.poll()
    assert s_stale is not None
    assert s_stale.throughput_fresh is False
    assert s_stale.tx_bytes == 125000

    # Second dump with more TX bytes → fresh throughput estimate
    path.write_text(
        SAMPLE_MAC_STATS.replace("TX         125000", "TX         250000")
    )
    time.sleep(0.12)
    s2 = ingest.poll()
    assert s2 is not None
    assert s2.tx_bytes == 250000
    assert s2.throughput_fresh is True
    assert s2.throughput_mbps == s2.throughput_mbps  # not NaN
    assert s2.throughput_mbps > 0.0
