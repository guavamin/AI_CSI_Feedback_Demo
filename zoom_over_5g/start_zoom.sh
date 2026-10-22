#!/usr/bin/env bash
# Zoom over 5G, UE laptop: starts the Zoom desktop client inside the ue5g namespace, as you. Zoom then sees
# only the 5G link; screen, sound, camera and your Zoom login work as usual (they do not go through the network).
#
#   bash start_zoom.sh     as your normal user, after "ue5g_netns.sh follow" printed "ready"
#
# Zoom must not be running yet: starting it a second time only wakes the Zoom already running, which uses
# Wi-Fi. Quit Zoom first (also from the top bar / tray icon). Ending the test (ue5g_netns.sh down) closes it.
# Zoom's output: data/runs/zoom_over_5g/zoom_<time>.log.
set -euo pipefail

NS=ue5g
REPO=$(cd "$(dirname "$0")/.." && pwd)
RUNS=$REPO/data/runs/zoom_over_5g
ZOOM=${ZOOM:-$(command -v zoom || true)}

[[ $EUID -ne 0 ]] || { echo "run this as your normal user (it asks sudo only to enter the namespace)"; exit 1; }
[[ -n $ZOOM ]] || { echo "Zoom is not installed: see zoom_over_5g/README.md (the .deb from zoom.us)"; exit 1; }
[[ $(readlink -f "$ZOOM") != /snap/* ]] || {
    echo "$ZOOM is the snap Zoom, which does not start inside a namespace: install the .deb (README)"; exit 1; }
[[ -e /run/netns/$NS ]] || { echo "namespace $NS does not exist: run 'sudo bash ue5g_netns.sh follow' first"; exit 1; }

ns_inode=$(stat -L -c %i "/run/netns/$NS")
in_ue5g() { [[ $(readlink "/proc/$1/ns/net" 2>/dev/null) == "net:[$ns_inode]" ]]; }
for pid in $(pgrep -u "$USER" -x zoom || true); do
    if in_ue5g "$pid"; then echo "Zoom is already running in $NS (PID $pid)"; exit 0; fi
    echo "Zoom is running outside $NS (PID $pid), over Wi-Fi: quit it first (also from the tray icon)"; exit 1
done

# what Zoom needs from your desktop session (sudo starts with an empty environment)
vars=(HOME="$HOME" USER="$USER" LOGNAME="$USER" PATH="$PATH" LANG="${LANG:-C.UTF-8}")
for v in DISPLAY WAYLAND_DISPLAY XAUTHORITY XDG_RUNTIME_DIR DBUS_SESSION_BUS_ADDRESS XDG_SESSION_TYPE \
         XDG_CURRENT_DESKTOP XDG_DATA_DIRS QT_QPA_PLATFORM PULSE_SERVER; do
    [[ -z ${!v:-} ]] || vars+=("$v=${!v}")
done
[[ -n ${DISPLAY:-}${WAYLAND_DISPLAY:-} ]] || {
    echo "no desktop session here (DISPLAY not set): run it in a terminal on the laptop's desktop"; exit 1; }

mkdir -p "$RUNS"
log=$RUNS/zoom_$(date +%Y%m%d_%H%M%S).log
sudo true   # the password prompt now (if sudo asks for one), not behind the background start below
sudo ip netns exec "$NS" sudo -u "$USER" -- env -i "${vars[@]}" setsid "$ZOOM" > "$log" 2>&1 < /dev/null &

for _ in $(seq 20); do
    sleep 0.5
    for pid in $(pgrep -u "$USER" -x zoom || true); do
        if in_ue5g "$pid"; then
            echo "Zoom started in $NS (PID $pid): it uses 5G only. Log: ${log#"$REPO"/}"
            exit 0
        fi
    done
done
echo "Zoom did not start within 10 s: see ${log#"$REPO"/}"
exit 1
