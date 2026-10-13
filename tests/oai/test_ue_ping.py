"""UE link ping verification before iperf."""

from __future__ import annotations

from unittest.mock import patch

from common.oai.net.ue_ip import verify_ue_ping


def test_verify_ue_ping_uses_dn_when_container_running():
    with patch(
        "common.oai.net.ue_ip._docker_container_running", return_value=True
    ), patch(
        "common.oai.net.ue_ip._run_ping",
        return_value=(True, "3 packets transmitted, 3 received"),
    ) as run:
        ok, detail = verify_ue_ping("10.0.0.9", duration_s=3.0, iface="oaitun_ue1")
    assert ok is True
    assert "DN" in detail
    cmd = run.call_args.args[0]
    assert cmd[:3] == ["docker", "exec", "oai-ext-dn"]
    assert "10.0.0.9" in cmd
    assert "-w" in cmd


def test_verify_ue_ping_falls_back_to_host():
    with patch(
        "common.oai.net.ue_ip._docker_container_running", return_value=False
    ), patch(
        "common.oai.net.ue_ip._run_ping",
        return_value=(True, "2 packets transmitted, 2 received"),
    ) as run:
        ok, detail = verify_ue_ping(
            "10.0.0.9",
            duration_s=3.0,
            iface="oaitun_ue1",
            docker_container="oai-ext-dn",
        )
    assert ok is True
    assert "host" in detail
    cmd = run.call_args.args[0]
    assert cmd[0] == "ping"
    assert "-I" in cmd
    assert "oaitun_ue1" in cmd
