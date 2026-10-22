# From Conventional CSI to Interoperable AI-Native Latent Feedback: live OpenAirInterface demo GUIs

Demo GUIs for the OAI 5G NR prototype with live switching between three CSI feedback scenarios:

1. **Legacy CSI Feedback**: 3GPP Type-I codebook, the UE reports a beam index (PMI).
2. **AI Feedback · Same Vendor**: UE encoder and gNB decoder from the same vendor, trained end to end.
3. **AI Feedback · Cross Vendor**: Vendor 1 UE, Vendor 2 gNB decoder with a linear transform at the gNB.

## Layout

```
common/              shared code
  oai/               OAI control: start/stop gNB and UE, telnet live switch, traffic, metrics (from pyoaicsinet, MIT)
  demo_control.py    the three scenarios on top of it (Legacy <-> AI on both sides, Same <-> Cross = gNB decoder swap)
  replay/            the UE's live channel replayed through all three scenarios with OAI's own model code
  style.py           palette, scenario identities, logos
same_laptop/         booth GUI: gNB, UE, core network and GUI on one laptop
  app.py             the live GUI
  mockup.py          the approved design, replaying recorded OTA data
separate_laptops/    gNB station + UE station (mockups; see separate_laptops/README.md)
zoom_over_5g/        tests: a Zoom call over the 5G link on the separate-laptop setup (see zoom_over_5g/README.md)
configs/             run profiles: same_laptop_base.yaml, demo_usrp.yaml (over the air), demo_rfsim.yaml
assets/              bundled fonts (Ubuntu Font Licence) and logos
data/mockups/        OTA replay data used by the mockups (regenerate with tools/export_mockup_*.py)
tools/               end-to-end checks and data export scripts
tests/               pytest suite (no radios needed; replay tests use the OTA recordings when present)
```

## Requirements

- OAI built from the `gui-live-switch` branch (worktree `../openairinterface5g_gui`, build
  `cmake_targets/ran_build_gui`, with `-DENABLE_TELNETSRV=ON`). Profiles find it through `${OAI_DIR}`
  (default `../openairinterface5g_gui`; set `OAI_DIR=/path` to use another tree).
- OAI CN5G running (`docker compose ... up -d`, containers oai-amf / oai-smf / oai-upf / oai-ext-dn).
- Passwordless sudo for the two ran_build_gui binaries (`/etc/sudoers.d/pyoaicsinet`).
- Python 3.10+, `./install.sh`. gcc (the replay compiles OAI's model code on first use).
- Never run the GUI while the OTA pipeline (ran_build_user) is running: the GUI stops every softmodem.

## Run

```
./install.sh
.venv/bin/python -m same_laptop.app                                  # over the air (configs/demo_usrp.yaml)
.venv/bin/python -m same_laptop.app --profile configs/demo_rfsim.yaml   # RF simulator, no radios
```
Start system, wait for the six steps to turn green, then press 1 / 2 / 3 or click a scenario. F11 for
fullscreen. Closing the window stops traffic, UE and gNB.

Mockups (no OAI needed): `python -m same_laptop.mockup`, `python -m separate_laptops.gnb_station.mockup`,
`python -m separate_laptops.ue_station.mockup`.

## Checks

```
.venv/bin/python -m pytest -q                                         # unit tests
.venv/bin/python -m tools.e2e_scenarios configs/demo_rfsim.yaml 10     # all scenario transitions, live
.venv/bin/python -m tools.e2e_replay configs/demo_rfsim.yaml 20        # replay == what the UE sent, bit for bit
.venv/bin/python -m tools.validate_replay <run02 csi/ue> <cache_manifest.json>   # replay == PyTorch evaluation
```

## Status

- Same-laptop GUI: live, verified in RF-sim (bring-up, all scenario switches, plots, channel replay).
  Over the air: profile ready (mirrors the OTA_Experiment_20260922 run commands), not yet run.
- Separate laptops: mockups only.
