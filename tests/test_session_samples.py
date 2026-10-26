"""CDFs and average bars cover the whole session; the time plots keep only the last hour."""

from types import SimpleNamespace

from common.demo_control import DemoController, Sample
from common.oai.config import load_config
from common.style import REPO
from same_laptop.app import scenario_values


class NoRadio:
    """Stand-in for the OAI controller: DemoController.view() only reads a snapshot (no softmodem is touched)."""

    csirs_record_root = ""

    def get_snapshot(self):
        return SimpleNamespace(gnb_boot_stage="", ue_bringup_stage="", gnb_running=False, ue_running=False,
                               ue_attached=False, ue_ip="", traffic_stage="idle", traffic_desired=False,
                               iperf_client_running=False)


def sample(t, key, tput):
    return Sample(t=t, scenario=key, tput_mbps=tput, bler=0.1, mcs=9.0, cqi=15.0, ul_snr_db=30.0, rsrp_dbm=-80.0)


def test_long_run_in_one_scenario_keeps_the_other_scenarios():
    demo = DemoController(load_config(REPO / "configs/demo_rfsim.yaml"), controller=NoRadio())
    with demo._lock:  # 4400 s of Legacy, then 4000 s of Same vendor (the 2026-10-26 run)
        for t in range(4400):
            demo._add_sample(sample(t, "legacy", 6.0))
        for t in range(4400, 8400):
            demo._add_sample(sample(t, "same", 8.0))
    view = demo.view()
    assert len(view.samples) == 3600  # time plots: the last hour only
    assert all(s.scenario == "same" for s in view.samples)
    legacy = scenario_values(view, "legacy", "tput_mbps")
    assert legacy.size == 4400 and legacy.mean() == 6.0  # still in the CDFs and the average bars
    assert scenario_values(view, "same", "tput_mbps").size == 4000


def test_views_without_session_pool_fall_back_to_samples():
    view = SimpleNamespace(samples=[sample(0, "legacy", 5.0), sample(1, "same", 7.0)])  # e.g. the mockups
    assert scenario_values(view, "same", "tput_mbps").tolist() == [7.0]
