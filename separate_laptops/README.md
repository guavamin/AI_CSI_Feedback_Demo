# Separate laptops: gNB station + UE station

No cable between the two sides: the radio link is over the air, and the UE's USRP runs on its own clock
(untested so far, see "Open items"). Each laptop runs its own GUI.

| | gNB station (`gnb_station/`) | UE station (`ue_station/`) |
|---|---|---|
| Runs | core network, gNB, downlink stream (sender) | UE, traffic receiver |
| Steps | 1 core network, 2 gNB, 3 UE attached, 4 find UE IP (from the core), 5 check UE receiver, 6 downlink stream, 7 feedback scenario | 1 UE (USRP, sync + CFO, attach), 2 UE IP, 3 receiver, 4 feedback mode (set by the gNB) |
| Shows | throughput / MCS / BLER (live + CDF), CQI, UL SNR, feedback received on PUCCH | ground-truth channel, reconstruction by replaying the gNB's decoder, cosine CDF, feedback sent, RX throughput, RSRP / SINR / CFO |

Scenario changes are made on the gNB station only. The gNB station sends the request to the UE station
through the 5G link itself (the UE's IP is known from step 4); both switch at the same radio frame and both
show a pop-up. If the UE station does not confirm, the gNB does not switch. Both stations keep a
boot-style event log for debugging.

Open items (not implemented): the station-to-station messages, UE on its internal clock (test with
`--clock-source 0 --time-source 0`), lead time for the scheduled switch over the 5G link, finding the UE IP
from the core network logs.

Mockups (replay of OTA data, not wired to OAI), from the repository root:

    .venv/bin/python -m separate_laptops.gnb_station.mockup     # keys 1/2/3: scenario + pop-up, Esc, F11
    .venv/bin/python -m separate_laptops.ue_station.mockup
