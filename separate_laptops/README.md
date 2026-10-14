# Separate laptops: gNB station + UE station

No cable between the two sides: the 5G link is over the air, both USRPs take 10 MHz / PPS from GPS, and the
laptops talk over any IP path (same Wi-Fi, a travel router, or Tailscale across the internet) for control
only. Each laptop runs its own window, in the booth GUI's style, showing only what its side knows.

| | gNB station (`gnb_station/`) | UE station (`ue_station/`) |
|---|---|---|
| Runs | core network (docker), gNB, downlink sender | UE, traffic receiver |
| Start | **Start system**: gNB on air, then asks the UE station to start its UE, then traffic | **Start UE** (or started by the gNB station) |
| Scenario | chosen here (click a card or 1 / 2 / 3) | follows the gNB station (pop-up) |
| Plots | downlink throughput, MCS, BLER (time + CDF) | received throughput, DL decode failures, SINR |
| Channel tab | channel reconstructed from the latents the gNB received; the beam it transmits per 4-RB group | channel the UE measures; its best beam per 4-RB group (Legacy: plus the PMI it reports) |

How the pieces fit:
- `common/station_link.py`: JSON lines over TCP, port 9200, shared key (`STATION_KEY`). The UE station
  listens; the gNB station polls its status twice a second.
- The Legacy / AI switch is scheduled for the same radio frame on both sides (read from the gNB, 100 frames =
  1 s ahead, enough for a relayed Tailscale link); the UE's half goes through the link. If either side refuses
  or cannot be reached, both are cancelled. Same <-> Cross is a gNB-only decoder swap.
- The UE station keeps its UE up: it restarts the UE when it exits (USRP overflow abort) or loses its PDU
  session; the gNB station restarts the traffic when the UE is back.
- The gNB records the AI latents it receives (OAI `gui-live-switch`, `gnb_ai_fb_latents.csv`); the gNB
  station decodes them with the decoder the gNB used. No channel data crosses the link.

## Set up a laptop (once)

Put this repository at `~/Desktop/InterDigital_Project/ai-csi-feedback-demo`, then from a root shell:

    bash ~/Desktop/InterDigital_Project/ai-csi-feedback-demo/separate_laptops/setup_station.sh

It installs OAI's dependencies, UHD 4.6.0 (built from source, as on the first laptop), clones and builds OAI
(`gui-live-switch`) next to this repository, checks the models (C == PyTorch), adds the sudo rule for the
softmodems and creates the Python environment. Tested in a clean Ubuntu 24.04 container. Then, with the
B210 plugged in: `uhd_find_devices`.

## Run

On the UE laptop, then on the gNB laptop (normal user, from the repository root):

    .venv/bin/python -m separate_laptops.ue_station.app
    env UE_STATION=<UE laptop: Tailscale name or IP> .venv/bin/python -m separate_laptops.gnb_station.app

`STATION_KEY` must be the same on both (default in `configs/station_usrp.yaml`). RF simulator instead of
radios: `--profile configs/station_{gnb,ue}_rfsim.yaml` (UE side: `GNB_STATION=<gNB laptop>`).

Without a window: `.venv/bin/python -m separate_laptops.headless {gnb,ue} --profile ...`.
Mockups on recorded data: `.venv/bin/python -m separate_laptops.{gnb,ue}_station.mockup`.

## Tests

    .venv/bin/python -m tools.e2e_stations                    # both stations on this machine, RF sim, 150 ms link delay
    .venv/bin/python -m tools.e2e_stations --radio usrp       # both B210s on this machine

Start, three scenario switches confirmed on both sides, each side's data and channel view, a UE crash and its
recovery, Stop. 2026-10-14: RF sim 33/33; over the air (both B210s on one laptop, 100 ms one-way link delay)
33/33.
