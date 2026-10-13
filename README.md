# From Conventional CSI to Interoperable AI-Native Latent Feedback: live OpenAirInterface demo GUIs

Demo GUIs for the OAI 5G NR prototype with live switching between three CSI feedback scenarios:

1. **Legacy CSI Feedback**: 3GPP Type-I codebook, the UE reports a beam index (PMI).
2. **AI Feedback · Same Vendor**: UE encoder and gNB decoder from the same vendor, trained end to end.
3. **AI Feedback · Cross Vendor**: Vendor 1 UE, Vendor 2 gNB decoder with a linear transform at the gNB.

## Layout

```
common/              shared look (palette, scenario identities), later: OAI launch/stop, telnet switch, log parsing
same_laptop/         booth GUI: gNB, UE, core network and GUI on one laptop
separate_laptops/    two station GUIs, one per laptop (see separate_laptops/README.md)
  gnb_station/
  ue_station/
assets/              bundled fonts (Ubuntu Font Licence) and logos
configs/             run profiles (to come)
data/mockups/        OTA replay data used by the mockups (regenerate with tools/)
docs/screenshots/    current mockup screenshots
tools/               export scripts for the mockup data (need InterDigital_Interoperability_Implementation)
```

## Install and run

```
./install.sh
.venv/bin/python -m same_laptop.mockup                    # keys 1/2/3 or click a scenario, F11 fullscreen
.venv/bin/python -m separate_laptops.gnb_station.mockup
.venv/bin/python -m separate_laptops.ue_station.mockup
```
Run from the repository root.

## Status

All three GUIs are **mockups**: they replay OTA measurements from 2026-09-24 (model set B) and are not yet
wired to OAI. Next: shared OAI control in `common/`, then the real same-laptop GUI, then the two stations.
The OAI side (live scheduled Legacy/AI switching over telnet) is on the `gui-live-switch` branch of the
OAI worktree.
