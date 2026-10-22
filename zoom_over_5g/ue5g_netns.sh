#!/usr/bin/env bash
# Zoom over 5G, UE laptop: gives the 5G link (oaitun_ue1) its own network namespace "ue5g". Only the programs
# started inside it (start_zoom.sh) use 5G; Wi-Fi, Tailscale and the station link stay exactly as they are.
#
#   bash ue5g_netns.sh follow    set up (waits for the UE's IP), then move oaitun_ue1 into ue5g again after
#                                every UE restart; Ctrl+C = down
#   bash ue5g_netns.sh up        set up once (the UE is attached, oaitun_ue1 has its IP)
#   bash ue5g_netns.sh down      stop the programs in ue5g (Zoom), give oaitun_ue1 back as OAI configured it,
#                                remove ue5g and its DNS file, compare the network setup with the one before up
#   bash ue5g_netns.sh check     ping, DNS and HTTPS to zoom.us from inside ue5g
#   bash ue5g_netns.sh status    where oaitun_ue1 is, programs in ue5g, anything left over
#   bash ue5g_netns.sh monitor   5G traffic of the programs in ue5g once a second (also to a CSV); Ctrl+C stops
#
# Run from a root shell (sudo -i, or: sudo bash ue5g_netns.sh ...). What it changes, and nothing else:
#   - namespace ue5g (kernel memory, /run/netns/ue5g)
#   - oaitun_ue1 moved into it, with the UE's IP, MTU 1400 and a default route (inside ue5g only)
#   - /etc/netns/ue5g/resolv.conf: DNS for ue5g only (the core's DNS servers for the UE)
# "down" undoes all three. A reboot clears everything except the DNS file; "status" finds it, "down" removes it.
# State while set up: /run/zoom_over_5g (RAM). Records (snapshots, traffic CSV): data/runs/zoom_over_5g/.
set -euo pipefail

NS=ue5g
IFACE=${IFACE:-oaitun_ue1}
MTU=${MTU:-1400}                 # leaves room for the GTP-U header between gNB and UPF
DNS=(1.1.1.1 8.8.8.8)            # the core's DNS servers for the UE (SMF ue_dns)
OAI_TABLE=10000                  # OAI's route table for the UE (setup_ue_ipv4_route): restored by down
WAIT_S=${WAIT_S:-300}            # up / follow: how long to wait for the UE's IP
REPO=$(cd "$(dirname "$0")/.." && pwd)
OWNER=$(stat -c %U "$REPO")
RUNS=$REPO/data/runs/zoom_over_5g
STATE=/run/zoom_over_5g
NETNS_ETC=/etc/netns/$NS

say() { printf '%s  %s\n' "$(date +%T)" "$*"; }
in_ns() { ip netns exec "$NS" "$@"; }
ns_exists() { [[ -e /run/netns/$NS ]]; }
iface_in_root() { ip link show dev "$IFACE" &>/dev/null; }
iface_in_ns() { ns_exists && in_ns ip link show dev "$IFACE" &>/dev/null; }
# "addr/prefix" of the iface; arguments = prefix command (in_ns) or none (normal setup)
ipv4_of() { "$@" ip -4 -o addr show dev "$IFACE" 2>/dev/null | awk '{print $4; exit}'; }
# program names of a list of PIDs (one per line)
names_of() { [[ -z $1 ]] || ps -o comm= -p "$(paste -sd, <<<"$1")" | sort -u | paste -sd' '; }

records_dir() {
    mkdir -p "$RUNS"
    chown "$OWNER:" "$RUNS" "$(dirname "$RUNS")" 2>/dev/null || true
}

# The laptop's own network setup. Lines about oaitun_ue1 and OAI's table are left out: OAI adds and removes
# them with every UE start, so they are not ours to compare. Route expiry timers and firewall counters change
# by themselves and are dropped too.
snapshot() {
    {
        echo "## interfaces"; ip -o link show | awk -F': ' '{print $2}' | sed 's/@.*//' | sort
        echo "## addresses";  ip -o addr show | awk '{print $2, $3, $4}' | sort
        echo "## routes";     ip route show table all | sed -E 's/ expires [0-9]+sec//' | sort
                              ip -6 route show table all | sed -E 's/ expires [0-9]+sec//' | sort
        echo "## rules";      ip rule show; ip -6 rule show
        echo "## namespaces"; ls /run/netns 2>/dev/null || true
        echo "## /etc/netns"; ls -A /etc/netns 2>/dev/null || true
        echo "## resolv.conf"; readlink -f /etc/resolv.conf; cat /etc/resolv.conf
        echo "## firewall"
        if command -v nft >/dev/null; then
            nft list ruleset 2>/dev/null | sed -E 's/counter packets [0-9]+ bytes [0-9]+/counter/' || true
        elif command -v iptables-save >/dev/null; then
            iptables-save 2>/dev/null | sed -E -e '/^#/d' -e 's/\[[0-9]+:[0-9]+\]//' || true
        else
            echo "(no nft or iptables-save on this laptop)"
        fi
    } | grep -v -e "$IFACE" -e "lookup $OAI_TABLE" -e "table $OAI_TABLE"
}

need_root() { [[ $EUID -eq 0 ]] || { echo "run this from a root shell (sudo bash $0 $*)"; exit 1; }; }

wait_for_ip() {
    local t0=$SECONDS
    say "waiting for the UE's IP on $IFACE (UE attached, PDU session up) ..."
    until iface_in_root && [[ -n $(ipv4_of) ]]; do
        if iface_in_ns; then say "$IFACE is already in $NS"; return 0; fi
        (( SECONDS - t0 < WAIT_S )) || { say "no IP on $IFACE after ${WAIT_S}s: is the UE attached?"; exit 1; }
        sleep 1
    done
    sleep 2   # let OAI finish its own setup of the new interface (its ip rule / route commands run in background)
}

# Once per test: snapshot of the setup before anything is changed, the namespace and its DNS file.
prepare() {
    mkdir -p "$STATE"
    if [[ ! -f $STATE/before.txt ]]; then
        snapshot > "$STATE/before.txt"
        [[ -d /etc/netns ]] && touch "$STATE/etc_netns_existed"
    fi
    if ! ns_exists; then
        ip netns add "$NS"
        in_ns ip link set lo up   # apps (Zoom too) talk to themselves over 127.0.0.1
        say "namespace $NS created"
    fi
    mkdir -p "$NETNS_ETC"
    printf 'nameserver %s\n' "${DNS[@]}" > "$NETNS_ETC/resolv.conf"
}

# oaitun_ue1 from the normal setup into ue5g. Moving an interface clears its address and takes it down, so
# both are set again inside ue5g. OAI keeps its handle to the interface and goes on reading and writing it.
move_in() {
    local cidr mtu
    cidr=$(ipv4_of)
    mtu=$(cat "/sys/class/net/$IFACE/mtu" 2>/dev/null) || mtu=1500
    [[ -n $cidr ]] || { say "$IFACE has no IP"; return 1; }
    echo "$cidr $mtu" > "$STATE/iface"   # what OAI set: given back by down
    ip link set "$IFACE" netns "$NS" || { say "could not move $IFACE into $NS"; return 1; }
    in_ns ip addr add "$cidr" dev "$IFACE" &&
        in_ns ip link set "$IFACE" mtu "$MTU" up &&
        in_ns ip route add default dev "$IFACE" || { say "could not configure $IFACE in $NS"; return 1; }
    say "$IFACE ($cidr) is in $NS: programs started there use 5G only"
}

# oaitun_ue1 back into the normal setup, configured as OAI left it (address, MTU, OAI's route in its table).
move_out() {
    local cidr mtu=1500 saved_cidr=""
    cidr=$(ipv4_of in_ns)
    [[ -f $STATE/iface ]] && read -r saved_cidr mtu < "$STATE/iface"
    cidr=${cidr:-$saved_cidr}
    in_ns ip link set "$IFACE" netns 1 || { say "could not move $IFACE back"; return 1; }  # 1 = PID 1's: the normal one
    { [[ -z $cidr ]] || ip addr replace "$cidr" dev "$IFACE"; } &&
        ip link set "$IFACE" mtu "$mtu" up &&
        ip route replace default dev "$IFACE" table "$OAI_TABLE" || { say "could not configure $IFACE"; return 1; }
    say "$IFACE ($cidr) is back in the normal setup, as OAI configured it"
}

check() {
    ns_exists || { echo "namespace $NS does not exist: run up or follow first"; return 1; }
    local ok=0
    say "check from inside $NS:"
    if in_ns ip route get 1.1.1.1 2>/dev/null | grep -q "dev $IFACE"; then echo "  route   : via $IFACE  OK"
    else echo "  route   : no route via $IFACE  FAIL"; ok=1; fi
    local out
    if out=$(in_ns ping -c 3 -W 2 -q 1.1.1.1 2>/dev/null); then
        echo "  ping    : 1.1.1.1 $(awk -F/ '/rtt/ {print "avg " $5 " ms"}' <<<"$out")  OK"
    else echo "  ping    : 1.1.1.1 no answer  FAIL"; ok=1; fi
    if out=$(in_ns getent ahostsv4 zoom.us); then echo "  DNS     : zoom.us -> $(awk '{print $1; exit}' <<<"$out")  OK"
    else echo "  DNS     : zoom.us not resolved  FAIL"; ok=1; fi
    # any HTTP answer (even an error page) means TLS to Zoom's servers works
    if in_ns timeout 15 python3 -c '
import urllib.error, urllib.request
try:
    urllib.request.urlopen("https://zoom.us", timeout=10)
except urllib.error.HTTPError:
    pass' 2>/dev/null
    then echo "  HTTPS   : https://zoom.us  OK"
    else echo "  HTTPS   : https://zoom.us failed  FAIL"; ok=1; fi
    return $ok
}

compare() {
    [[ -f $STATE/before.txt ]] || { say "no snapshot from up: nothing to compare"; return 0; }
    local stamp; stamp=$(date +%Y%m%d_%H%M%S)
    snapshot > "$STATE/after.txt"
    records_dir
    cp "$STATE/before.txt" "$RUNS/netns_${stamp}_before.txt"
    cp "$STATE/after.txt" "$RUNS/netns_${stamp}_after.txt"
    chown "$OWNER:" "$RUNS/netns_${stamp}_before.txt" "$RUNS/netns_${stamp}_after.txt" 2>/dev/null || true
    if diff -q "$STATE/before.txt" "$STATE/after.txt" >/dev/null; then
        say "CLEAN: the network setup is the same as before up (saved: data/runs/zoom_over_5g/netns_${stamp}_*.txt)"
    else
        say "DIFFERENT from before up. This script does not touch these; check whether something else changed"
        say "them during the test (Wi-Fi reconnect, Tailscale peers, docker):"
        diff "$STATE/before.txt" "$STATE/after.txt" | sed 's/^/    /' || true
    fi
}

down() {
    if ns_exists; then
        local pids
        pids=$(ip netns pids "$NS")
        if [[ -n $pids ]]; then
            say "stopping the programs in $NS: $(names_of "$pids")"
            # shellcheck disable=SC2086
            kill $pids 2>/dev/null || true
            for _ in 1 2 3 4 5 6 7 8 9 10; do [[ -z $(ip netns pids "$NS") ]] && break; sleep 0.5; done
            pids=$(ip netns pids "$NS")
            # shellcheck disable=SC2086
            [[ -z $pids ]] || kill -9 $pids 2>/dev/null || true
        fi
        if iface_in_ns; then move_out || true; fi
        ip netns del "$NS"
        say "namespace $NS removed"
    fi
    if [[ -d $NETNS_ETC ]]; then
        rm -rf "$NETNS_ETC"
        say "$NETNS_ETC removed"
    fi
    if [[ -d /etc/netns && ! -f $STATE/etc_netns_existed ]] && [[ -z $(ls -A /etc/netns) ]]; then
        rmdir /etc/netns
    fi
    compare
    rm -rf "$STATE"
}

status() {
    local where="not present (UE not attached)"
    if iface_in_ns; then where="in $NS, $(ipv4_of in_ns)"
    elif iface_in_root; then where="in the normal setup, $(ipv4_of)"; fi
    echo "$IFACE          : $where"
    if ns_exists; then
        echo "namespace $NS   : present"
        echo "programs in $NS : $(names_of "$(ip netns pids "$NS")")"
        if iface_in_ns; then
            echo "5G traffic       : DL $(in_ns cat "/sys/class/net/$IFACE/statistics/rx_bytes") bytes," \
                 "UL $(in_ns cat "/sys/class/net/$IFACE/statistics/tx_bytes") bytes since the UE started"
        fi
    else
        echo "namespace $NS   : not present"
    fi
    echo "DNS file         : $([[ -e $NETNS_ETC ]] && echo "$NETNS_ETC (removed by down)" || echo none)"
    echo "test state       : $([[ -d $STATE ]] && echo "set up (run down to finish)" || echo none)"
}

monitor() {
    records_dir
    local csv; csv=$RUNS/traffic_$(date +%Y%m%d_%H%M%S).csv
    echo "time_utc,dl_mbps,ul_mbps,ue_ip" > "$csv"
    chown "$OWNER:" "$csv" 2>/dev/null || true
    say "5G traffic of the programs in $NS (DL = to this laptop, UL = from it); CSV: ${csv#"$REPO"/}"
    printf '%-10s %9s %9s  %s\n' time "DL Mbit/s" "UL Mbit/s" "UE IP"
    local rx0="" tx0="" t0=""
    while sleep 1; do
        local rx tx t ip
        if iface_in_ns; then
            rx=$(in_ns cat "/sys/class/net/$IFACE/statistics/rx_bytes")
            tx=$(in_ns cat "/sys/class/net/$IFACE/statistics/tx_bytes")
            ip=$(ipv4_of in_ns); t=$(date +%s.%N)
            if [[ -n $rx0 && $rx -ge $rx0 ]]; then
                local dl ul
                dl=$(awk -v a="$rx0" -v b="$rx" -v s="$t0" -v e="$t" 'BEGIN {printf "%.2f", (b - a) * 8 / (e - s) / 1e6}')
                ul=$(awk -v a="$tx0" -v b="$tx" -v s="$t0" -v e="$t" 'BEGIN {printf "%.2f", (b - a) * 8 / (e - s) / 1e6}')
                printf '%-10s %9s %9s  %s\n' "$(date +%T)" "$dl" "$ul" "$ip"
                echo "$(date -u +%FT%T.%3NZ),$dl,$ul,$ip" >> "$csv"
            fi
            rx0=$rx; tx0=$tx; t0=$t
        else
            printf '%-10s %9s %9s  %s\n' "$(date +%T)" - - "$IFACE not in $NS (UE restarting?)"
            echo "$(date -u +%FT%T.%3NZ),,," >> "$csv"
            rx0=""
        fi
    done
}

follow() {
    trap 'echo; say "finishing"; down; exit 0' INT TERM
    wait_for_ip
    prepare
    iface_in_ns || move_in || { say "finishing"; down; exit 1; }
    check || say "check failed: see above (the 5G link, the core's internet access, or DNS)"
    say "ready: start Zoom with start_zoom.sh (as your user). Following UE restarts; Ctrl+C = down"
    local seen=0
    while sleep 1; do
        # a UE restart creates a new oaitun_ue1 in the normal setup: move it in once its IP is stable
        if iface_in_root && [[ -n $(ipv4_of) ]]; then
            if (( ++seen >= 2 )); then
                say "UE restarted: new $IFACE ($(ipv4_of))"
                move_in || say "will try again"
                seen=0
            fi
        else
            seen=0
        fi
    done
}

cmd=${1:-}
case $cmd in
    up)      need_root "$@"; wait_for_ip; prepare; iface_in_ns || move_in || exit 1; check || true ;;
    down)    need_root "$@"; down ;;
    follow)  need_root "$@"; follow ;;
    check)   need_root "$@"; check ;;
    status)  need_root "$@"; status ;;
    monitor) need_root "$@"; monitor ;;
    *) echo "usage: $0 follow|up|down|check|status|monitor"; exit 1 ;;
esac
