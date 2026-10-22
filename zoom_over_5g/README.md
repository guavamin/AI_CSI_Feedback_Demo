# Zoom over 5G (tests)

A Zoom call from the UE laptop that goes over the OAI 5G link, with the AI CSI feedback live, on the
separate-laptops setup. Tests only: the GUI code and the profiles in `configs/` are not changed.

```
UE laptop                                                    gNB laptop
  ue5g namespace:   Zoom -> oaitun_ue1 -> OAI UE -> B210 ))) ((( B210 -> gNB -> UPF (docker) -> Wi-Fi -> Internet -> Zoom
  normal network:   UE station <- Wi-Fi / Tailscale ------------------------> gNB station   (Legacy / AI switch requests)
```

The 5G interface `oaitun_ue1` is moved into its own network namespace `ue5g`. Zoom, started inside it, sees only
the 5G link. The rest of the laptop (Wi-Fi, Tailscale, the station link) does not see it and stays as it is, so
the gNB station's switch requests never queue behind the video.

AI CSI feedback changes the gNB's downlink precoding: it can only improve the video the UE laptop **receives**.
The video sent from the UE laptop's camera goes uplink and is not affected.

## What changes, and what does not

Nothing changes on the gNB laptop. On the UE laptop, `ue5g_netns.sh` changes only these, and `down` (or Ctrl+C
on `follow`) undoes all of them:

| Change | Where | Removed by |
|---|---|---|
| namespace `ue5g` | kernel, `/run/netns/ue5g` | `down`; also a reboot |
| `oaitun_ue1` moved into `ue5g`, with its IP, MTU 1400, default route | inside `ue5g` only | `down` gives it back as OAI set it up (IP, MTU 1500, OAI's route in table 10000) |
| `/etc/netns/ue5g/resolv.conf` (DNS 1.1.1.1, 8.8.8.8, the core's DNS for the UE) | disk, read only for `ue5g` | `down` |

Not touched: Wi-Fi, the default route, `/etc/resolv.conf`, firewall, Tailscale, OAI, the GUI, the profiles in
`configs/`. `down` compares the network setup with a snapshot taken before `up` and prints **CLEAN** or the
differences (both snapshots are kept in `data/runs/zoom_over_5g/`).

The UE station runs with `station_ue_zoom_usrp.yaml`: the normal profile with one change. While `oaitun_ue1` is in
`ue5g` the station cannot see the UE's IP; normally it would take that for a lost PDU session and restart the UE
after ~20 s. With this profile it waits. A UE that exits is still restarted, and `follow` moves the new
`oaitun_ue1` in again.

## Once, on the UE laptop

1. This branch (from the repository root, normal user):

       git fetch
       git checkout zoom-over-5g

   The station code on this branch is the same as on `master`.
2. Zoom desktop client, the `.deb` (the snap version does not start inside a namespace):

       wget https://zoom.us/client/latest/zoom_amd64.deb
       sudo apt install ./zoom_amd64.deb

   Start it once normally, sign in, then **quit** it (also from the top bar / tray icon).
3. Tailscale must not use an exit node: `tailscale status` shows no "exit node" line (clear it with
   `sudo tailscale set --exit-node=`).

On the gNB laptop: `git fetch` and `git checkout zoom-over-5g` too (for `gnb_monitor.py`).

## Run a test

All commands from the repository root. `<gNB>` / `<UE>` = which laptop.

1. `<gNB>` gNB station as usual:

       env UE_STATION=<UE laptop> .venv/bin/python -m separate_laptops.gnb_station.app

2. `<UE>` UE station with the test profile:

       .venv/bin/python -m separate_laptops.ue_station.app --profile zoom_over_5g/station_ue_zoom_usrp.yaml

3. `<gNB>` **Start system**. Wait until the UE station shows the UE's IP and the gNB station shows traffic.
4. `<UE>` in a terminal:

       sudo bash zoom_over_5g/ue5g_netns.sh follow

   It moves `oaitun_ue1` into `ue5g` and checks route, ping, DNS and HTTPS to zoom.us from inside, then prints
   `ready`. Leave it running: it moves the interface in again after a UE restart.
   From now on the stations show the UE without an IP and the gNB station sends no iperf: expected, the downlink
   is left to Zoom (see "What the stations show").
5. `<UE>` in a second terminal, as your normal user (not root):

       bash zoom_over_5g/start_zoom.sh

   It asks for your sudo password and starts Zoom inside `ue5g`. Join a meeting. The other side: a phone or
   another laptop on normal internet.
6. Watch (optional, each in its own terminal):
   - `<UE>` `sudo bash zoom_over_5g/ue5g_netns.sh monitor`: Zoom's 5G traffic, DL and UL Mbit/s
   - `<gNB>` `.venv/bin/python -m zoom_over_5g.gnb_monitor`: DL Mbit/s, MCS, BLER, CQI the gNB sends to the UE.
     Type a note + Enter (e.g. `AI`) right after each switch: it goes into the CSV.
   - Zoom: Settings -> Statistics (audio / video: resolution, frame rate, latency, jitter, packet loss)
7. `<gNB>` switch Legacy / AI / Cross on the gNB station as usual; the call keeps running.
8. Finish: Ctrl+C in the `follow` terminal. It closes Zoom, gives `oaitun_ue1` back, removes `ue5g` and its DNS
   file, and prints **CLEAN** (or what differs). The station sees the IP again and iperf resumes. Stop the system
   on the gNB station as usual. Next time, start the UE station without `--profile` for the normal demo.

Write what you saw in `TEST_LOG.md`.

`up` / `down` instead of `follow`: set up once / finish, without following UE restarts.
`sudo bash zoom_over_5g/ue5g_netns.sh status`: where `oaitun_ue1` is, what runs in `ue5g`, anything left over.

## Is Zoom really on 5G?

Both laptops are on the same Wi-Fi, so Zoom's public IP is the same either way: it proves nothing. Instead:
- `monitor` on the UE laptop shows the call's traffic on `oaitun_ue1` (a video call: about 1-4 Mbit/s DL);
- `gnb_monitor` on the gNB laptop shows the same downlink leaving the gNB;
- stop the UE (or unplug its B210): the call freezes and Zoom says it is reconnecting.

## What the stations show during the test

- UE station: the "UE IP" step is not done, and no traffic receiver runs.
- gNB station: traffic "waiting for the UE's IP"; the plots and CDFs pause (they only count iperf traffic).
  Use `gnb_monitor` for the downlink instead.
- After a UE restart, the station sees the new IP for ~2 s before `follow` moves it: a short iperf burst is normal.
- Scenario switches work as usual: they need both softmodems and the link between the stations, not traffic.

## Problems

| Symptom | Likely cause |
|---|---|
| `check`: ping fails | 5G link down, or the gNB laptop's core / Wi-Fi has no internet (`docker exec oai-ext-dn ping 1.1.1.1` on the gNB laptop) |
| `check`: ping OK, DNS fails | DNS blocked on the gNB laptop's network; try another DNS in `ue5g_netns.sh` (`DNS=`) |
| `start_zoom.sh`: "running outside ue5g" | Zoom was already open (over Wi-Fi): quit it, also from the tray icon |
| `start_zoom.sh`: "did not start" | see `data/runs/zoom_over_5g/zoom_<time>.log` |
| Zoom: no camera / sound | check them in a normal Zoom first (Zoom's settings); they do not depend on the network |
| Zoom keeps reconnecting | the 5G link drops (UE station events), or the venue / campus Wi-Fi blocks Zoom on the gNB laptop |
| `down` prints differences | something else changed the network during the test (Wi-Fi reconnect, Tailscale peers): see the lines |

If anything is left over (e.g. after a crash): `sudo bash zoom_over_5g/ue5g_netns.sh down`. A reboot clears all
of it except the DNS file, which `down` removes.

## Dry run without radios (RF simulator)

Same steps with `configs/station_gnb_rfsim.yaml` on the gNB station and
`zoom_over_5g/station_ue_zoom_rfsim.yaml` on the UE station (`GNB_STATION=<gNB laptop>`). This checks the
plumbing (Zoom over the 5G path, the cleanup) but not the AI gain: in RF-sim the channel is flat and AI CSI
does worse than Legacy.

## Files

| File | Laptop | What |
|---|---|---|
| `ue5g_netns.sh` | UE (root) | namespace: `follow`, `up`, `down`, `check`, `status`, `monitor` |
| `start_zoom.sh` | UE (user) | starts Zoom inside `ue5g` |
| `station_ue_zoom_usrp.yaml`, `station_ue_zoom_rfsim.yaml` | UE | UE station profiles: no UE restart while the IP is in `ue5g` |
| `gnb_monitor.py` | gNB | downlink to the UE once a second, from `nrMAC_stats.log` (read-only) |
| `TEST_LOG.md` | | results of each test |

Records (git-ignored): `data/runs/zoom_over_5g/` — network snapshots, traffic CSVs, Zoom's log, gNB CSVs.
