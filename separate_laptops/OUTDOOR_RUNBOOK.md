# Outdoor dynamic run, two laptops: collect → train → run live

| | gNB laptop (EN4202544UL) | UE laptop (en4202546ul) |
|---|---|---|
| Hardware | B210 **serial 31F3CC3** + GPS 10 MHz/PPS, core network (docker), GPU for training | the other B210 + GPS 10 MHz/PPS, robot's USB-to-CAN adapter; rides on the robot |
| Does | gNB, training | UE, robot, data recording |

Session name used below: **`outdoor_dynamic`**. If you record another session, replace it everywhere (e.g.
`outdoor_dynamic_2`); the trained models will then be called `capture_outdoor_dynamic_2_B`.

**root** = your root shell. **user** = normal terminal, as kengminl. All commands are bash.
Never run a demo GUI while collecting: the GUIs stop every softmodem on their laptop.

---

## 0. Before going out

**0.1 Update the code (user, both laptops)**
```bash
cd /home/kengminl/Desktop/InterDigital_Project/ai-csi-feedback-demo && git pull
cd /home/kengminl/Desktop/InterDigital_Project/openairinterface5g_gui && git pull
```
(UE laptop: if `setup_station.sh` has not been run since 2026-10-19, run it once as root:
`bash /home/kengminl/Desktop/InterDigital_Project/ai-csi-feedback-demo/separate_laptops/setup_station.sh`)

**0.2 Hardware**: B210 `31F3CC3` on the gNB laptop, the other B210 on the UE laptop, GPS references connected and
locked (outdoors, a few minutes after power-up), antennas on, robot charged, CAN adapter between UE laptop and robot.

**0.3 Checks**

gNB laptop (root):
```bash
uhd_find_devices                                              # one B210, serial 31F3CC3
docker ps --format '{{.Names}}' | grep -cE 'oai-(amf|smf|upf)'  # must print 3
pgrep -a nr-softmodem; pgrep -a nr-uesoftmodem                 # must print nothing
```
If the core is down: `cd /home/kengminl/Desktop/InterDigital_Project/openairinterface5g_gui/doc/tutorial_resources/oai-cn5g && docker compose up -d`

UE laptop (root):
```bash
uhd_find_devices                                              # one B210, NOT 31F3CC3
pgrep -a nr-softmodem; pgrep -a nr-uesoftmodem                 # must print nothing
modprobe gs_usb; ip link set can0 up type can bitrate 500000  # "Device or resource busy" = already up
ip -details -statistics link show can0                        # run twice: RX packets must grow (robot on, cable in)
```

---

## 1. Data collection (legacy CSI)

Two runs, each the same steps:

| Run | Purpose | Stop at |
|---|---|---|
| `02_train` | training | **≥ 21,000 files** (≥ 28 min) |
| `01_test` | independent test, never trained on | **≥ 7,500 files** (~10 min) |

Same motion in both runs; in `02_train` keep repeating the same route / pattern for the whole time.

**1.1 gNB laptop, terminal 1 (root): gNB.** Wait ~10 s (log shows the reference `LOCKED` and the radio running).
```bash
bash /home/kengminl/Desktop/InterDigital_Project/ai-csi-feedback-demo/separate_laptops/collect.sh gnb outdoor_dynamic 02_train
```

**1.2 UE laptop, terminal 1 (user): robot.** Starts driving at once; Ctrl+C stops it (a second Ctrl+C exits at once).
Change speed (m/s, ≤ 0.3) / `--leg` (s per direction) / `--pause` as needed; `--turn 0.2` for an arc.
```bash
mkdir -p /home/kengminl/Desktop/InterDigital_Project/ota_sessions/outdoor_dynamic/02_train
cd /home/kengminl/Desktop/InterDigital_Project/ai-csi-feedback-demo
.venv/bin/python separate_laptops/ue_station/robot_driver.py --speed 0.15 --leg 12 --pause 1 --cycles 0 \
    | tee -i -a /home/kengminl/Desktop/InterDigital_Project/ota_sessions/outdoor_dynamic/02_train/robot.jsonl
```

**1.3 UE laptop, terminal 2 (root): UE.** Attached when the log shows `TUN Interface oaitun_ue1 successfully configured`.
```bash
bash /home/kengminl/Desktop/InterDigital_Project/ai-csi-feedback-demo/separate_laptops/collect.sh ue outdoor_dynamic 02_train
```
If the UE dies before the target (log ends with `ERROR_CODE_OVERFLOW` / `readBlockSize`): run the same command again;
it continues in the same folder.

**1.4 UE laptop, terminal 3 (user): count.**
```bash
watch -n 5 "find /home/kengminl/Desktop/InterDigital_Project/ota_sessions/outdoor_dynamic/02_train/csi/ue/csi_rs_channels -name 'H_*.bin' | wc -l"
```

**1.5 Stop at the target**: Ctrl+C in UE terminal 2 (UE), then UE terminal 1 (robot), then gNB terminal 1 (gNB).

**1.6 Run `01_test`**: steps 1.1 – 1.5 again with `01_test` in place of `02_train` (all four commands), stop at
≥ 7,500 files.

**1.7 Give the data to your user account (root, both laptops)**
```bash
chown -R kengminl:kengminl /home/kengminl/Desktop/InterDigital_Project/ota_sessions/outdoor_dynamic
```

**1.8 Check before packing up (UE laptop, user)**: rows = files, no long gaps, SINR sensible, and any robot stop.
```bash
cd /home/kengminl/Desktop/InterDigital_Project/ai-csi-feedback-demo
.venv/bin/python -m tools.capture_prepare check /home/kengminl/Desktop/InterDigital_Project/ota_sessions/outdoor_dynamic/02_train/csi/ue
.venv/bin/python -m tools.capture_prepare check /home/kengminl/Desktop/InterDigital_Project/ota_sessions/outdoor_dynamic/01_test/csi/ue
```
"rows without a file" must be 0. A listed STOP is fine (left out in 2.2), but if the run is mostly stops, record again.

**1.9 Pack for Dropbox (UE laptop, user)**, then upload `~/outdoor_dynamic_ue.tar.gz` (~0.3 GB):
```bash
tar -czf ~/outdoor_dynamic_ue.tar.gz -C /home/kengminl/Desktop/InterDigital_Project/ota_sessions outdoor_dynamic
```

---

## 2. Training (gNB laptop)

**2.1 Unpack (user)**: download `outdoor_dynamic_ue.tar.gz` from Dropbox to `~/Downloads`, then
```bash
mkdir -p /home/kengminl/Desktop/InterDigital_Project/ota_sessions
tar -xzf ~/Downloads/outdoor_dynamic_ue.tar.gz -C /home/kengminl/Desktop/InterDigital_Project/ota_sessions
```
(it lands next to the gNB's own logs of the same session.)

**2.2 Check and build the training view (user)**: `--auto` leaves out robot stops (± 2 s); the recording is not changed.
```bash
cd /home/kengminl/Desktop/InterDigital_Project/ai-csi-feedback-demo
.venv/bin/python -m tools.capture_prepare make /home/kengminl/Desktop/InterDigital_Project/ota_sessions/outdoor_dynamic/02_train/csi/ue --auto
```
Prints the rows kept; should stay ≥ 18,753 (the recipe's size; fewer still works, with smaller blocks).

**2.3 Train (root, ~50 min, GPU)**
```bash
cd /home/kengminl/Desktop/InterDigital_Project/InterDigital_Interoperability_Implementation
/home/kengminl/Desktop/InterDigital_Project/ID_project/bin/python -m interoperability.run_capture_retrain \
    --study capture_outdoor_dynamic \
    --dataset /home/kengminl/Desktop/InterDigital_Project/ota_sessions/outdoor_dynamic/02_train/csi/ue_train \
    2>&1 | tee /home/kengminl/Desktop/InterDigital_Project/ota_sessions/outdoor_dynamic/retrain.log
```
Interrupted? Run the same command again: finished stages are skipped. At the end of the log: four `PASS` lines
(C == PyTorch) and the validation NMSE (reference: 10/15 dynamic, own −10.06 dB, cross-vendor route −9.79 dB).

**2.4 Give the outputs to your user account (root)**
```bash
chown -R kengminl:kengminl \
    /home/kengminl/Desktop/InterDigital_Project/InterDigital_Interoperability_Implementation/results/interoperability/capture_outdoor_dynamic \
    /home/kengminl/Desktop/InterDigital_Project/openairinterface5g_gui/models/ai_fb_transformer/capture_outdoor_dynamic_B \
    /home/kengminl/Desktop/InterDigital_Project/ota_sessions/outdoor_dynamic/retrain.log
```

**2.5 Compare on the test run (user)**: new models vs the 10/15 dynamic ones, on `01_test`:
```bash
cd /home/kengminl/Desktop/InterDigital_Project/ai-csi-feedback-demo
T=/home/kengminl/Desktop/InterDigital_Project/ota_sessions/outdoor_dynamic/01_test/csi/ue
.venv/bin/python -m tools.evaluate_models $T ../openairinterface5g_gui/models/ai_fb_transformer/capture_outdoor_dynamic_B --every 5
.venv/bin/python -m tools.evaluate_models $T ../openairinterface5g_gui/models/ai_fb_transformer/capture_20261015_B --every 5
```
Beam gain closer to 0 dB and higher beam cosine = better (Legacy is listed for reference).

**2.6 Pack the models for Dropbox (user)**, then upload `~/capture_outdoor_dynamic_B.tar.gz` (~3.5 MB):
```bash
tar -czf ~/capture_outdoor_dynamic_B.tar.gz -C /home/kengminl/Desktop/InterDigital_Project/openairinterface5g_gui/models/ai_fb_transformer capture_outdoor_dynamic_B
```
(The UE itself only loads `model1_packed_p16.bin`, the Vendor 1 encoder; the whole folder is small and keeps the
same layout on both laptops.)

---

## 3. Install the model on the UE laptop (user)

Download `capture_outdoor_dynamic_B.tar.gz` from Dropbox to `~/Downloads`, then
```bash
tar -xzf ~/Downloads/capture_outdoor_dynamic_B.tar.gz -C /home/kengminl/Desktop/InterDigital_Project/openairinterface5g_gui/models/ai_fb_transformer
ls -l /home/kengminl/Desktop/InterDigital_Project/openairinterface5g_gui/models/ai_fb_transformer/capture_outdoor_dynamic_B/model1_packed_p16.bin
```

---

## 4. Run live with the new model (user, both laptops)

**UE laptop** (from the desktop):
```bash
cd /home/kengminl/Desktop/InterDigital_Project/ai-csi-feedback-demo
env MODELS=/home/kengminl/Desktop/InterDigital_Project/openairinterface5g_gui/models/ai_fb_transformer/capture_outdoor_dynamic_B \
    .venv/bin/python -m separate_laptops.ue_station.app
```
**gNB laptop** (from the desktop):
```bash
cd /home/kengminl/Desktop/InterDigital_Project/ai-csi-feedback-demo
env UE_STATION=en4202546ul \
    MODELS=/home/kengminl/Desktop/InterDigital_Project/openairinterface5g_gui/models/ai_fb_transformer/capture_outdoor_dynamic_B \
    .venv/bin/python -m separate_laptops.gnb_station.app
```
Then:
1. **USRP clock reference → External** in both windows (GPS references), before starting.
2. gNB window: wait for the green Wi-Fi dot, **Start system**; the UE window starts its UE by itself.
3. UE window, **Robot** tab: Bring up CAN if red, the same pattern as in the recording, **Start robot**.
4. gNB window: switch Legacy / Same vendor / Cross vendor (cards or keys 1 / 2 / 3).
5. Finish: **Stop robot**, then **Stop** in the gNB window (stops both sides).

Logs: `ai-csi-feedback-demo/data/runs/` on each laptop (`gui_logs/`, `softmodem_logs/`, `robot_logs/`).

---

## If something goes wrong

| Symptom | Likely cause / fix |
|---|---|
| UE stays at "searching SSB" / Sync | gNB not transmitting: its log stops after "setting rx channel"? Stop, power-cycle the B210 (USB, ~10 s), start again. Also GPS not locked yet. |
| UE window plots empty, receiver "no downlink data" | UE laptop firewall: root `ufw allow in on oaitun_ue1` |
| Robot: "no messages from the robot on can0" | robot off or CAN cable out; `ip -details -statistics link show can0` RX must grow |
| `sudo: a password is required` from a GUI | another sudoers file sorts after ours: root `ls /etc/sudoers.d/`, ours must be `zz-pyoaicsinet` |
| gNB window "UE station not reachable" | UE window not open, or Tailscale down: `tailscale ping en4202546ul` |
| `git pull` asks for a password | use a GitHub personal access token as the password |
