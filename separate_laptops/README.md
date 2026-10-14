# Separate laptops: gNB station + UE station

No cable between the two sides: the radio link is over the air, both USRPs take 10 MHz / PPS from GPS, and
the two laptops talk over Wi-Fi (control path only). Each laptop runs its own GUI, in the booth GUI's style,
and shows only what its own side can know.

| | gNB station (`gnb_station/`) | UE station (`ue_station/`) |
|---|---|---|
| Runs | core network, gNB, downlink traffic sender | UE, traffic receiver |
| Bring-up | Core, gNB, USRP, UE attached, Wi-Fi link to the UE station, Traffic | USRP, Sync, Attach, IP, Receiver, Wi-Fi link to the gNB station |
| Scenario | chosen here (click a card or 1 / 2 / 3); the request goes to the UE station over Wi-Fi, both switch at the same frame | follows the gNB station (pop-up when a request arrives) |
| Live plots | downlink throughput, MCS, BLER (time + CDF) | received throughput, decode failures, SINR (time + CDF) |
| Channel tab | channel reconstructed from the feedback it received (AI only), the beam it transmits per 4-RB group, both over recent reports | channel the UE measures (all four RX-TX links), its best beam per 4-RB group (Legacy: plus the PMI it reports), both over recent reports |

The UE's "best beam" and the gNB's "transmitted beam" use the same axes: side by side, they show how close
the feedback gets the gNB to the ideal beam (Legacy: one codebook beam for the whole band; AI: one per group).

Mockups (recorded OTA data, simulated bring-up, nothing talks to OAI), from the repository root:

    .venv/bin/python -m separate_laptops.gnb_station.mockup     # click Start, click a scenario card (or 1/2/3)
    .venv/bin/python -m separate_laptops.ue_station.mockup      # click Start; 1/2/3 simulate a gNB-station request

Esc hides a pop-up, F11 toggles fullscreen, `--no-history` starts the plots empty instead of with 3 recorded
minutes. Both mockups are built on `same_laptop/app.py` (header, cards, plots, themes).

Not implemented yet: the station-to-station protocol over Wi-Fi, the real station apps (OAI wiring per side).
