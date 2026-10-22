# Zoom over 5G: test log

One entry per test, newest last. Raw records: `data/runs/zoom_over_5g/` (name the files you used).

Template:

```
## YYYY-MM-DD  <short title>
Setup:     OTA / RF-sim; models; where the laptops were; other side of the call (phone, laptop)
Steps:     what was run (follow / up, any change from the README)
Result:    check OK?  Zoom joined?  CLEAN at the end?
Numbers:   per scenario: Zoom video resolution / frame rate / packet loss (Settings -> Statistics),
           DL Mbit/s and BLER (gnb_monitor), UE monitor DL / UL
Notes:     what went wrong, what to change
Records:   data/runs/zoom_over_5g/...
```

## 2026-10-21  Scripts checked in a container (no radios, no Zoom)

Setup: privileged Ubuntu 24.04 container on the gNB laptop. A small program played the OAI UE: it created
`oaitun_ue1` the way OAI does (tun, IP /24, flags POINTOPOINT + NOARP without MULTICAST, OAI's ip rules and the
route in table 10000) and kept reading packets from its handle. A stand-in program named `zoom` played Zoom.

Result:
- `follow`: `oaitun_ue1` moved into `ue5g`; ping and DNS sent from inside `ue5g` reached the UE's handle (OAI keeps
  working after the move); DNS inside `ue5g` = 1.1.1.1, 8.8.8.8.
- UE restart (old UE exits, new one with another IP): the new `oaitun_ue1` was moved in within ~2 s.
- Ctrl+C (`down`): the program inside `ue5g` was stopped; `oaitun_ue1` came back with OAI's IP, flags, MTU 1500
  and table-10000 route; `ue5g` and `/etc/netns/ue5g` removed; **CLEAN** against the snapshot. A second `down`
  does nothing.
- `start_zoom.sh`: refuses root, a missing namespace, a Zoom already running outside `ue5g` and a terminal without a
  desktop session; a normal start ran the stand-in as the user, with the desktop variables, seeing only `lo` and
  `oaitun_ue1`; a second start found it; `down` closed it.
- `monitor`: DL / UL Mbit/s from the interface counters (to the CSV). `gnb_monitor.py`: 10.0 Mbit/s from a
  simulated `nrMAC_stats.log` growing 10 Mbit/s, note column filled.
- Found and fixed on the way: a failing `chown` or a missing firewall tool could stop `down` before its cleanup.

Not tested yet: the real OAI UE, the real Zoom client, the UE station with the test profile, over the air.
