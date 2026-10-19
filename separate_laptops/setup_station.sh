#!/usr/bin/env bash
# One-time setup of a station laptop for the separate-laptops demo (Ubuntu 22.04 / 24.04, USRP B210).
#
# Put this repository on the laptop first (e.g. ~/Desktop/InterDigital_Project/ai-csi-feedback-demo), then
# from a root shell:
#     bash <repo>/separate_laptops/setup_station.sh
#
# Steps (each one skips what is already done, so the script can simply be run again):
#   1. system packages; OAI's dependencies (asn1c pinned to OAI's commit, in /opt/asn1c); UHD 4.6.0 from
#      source with OAI's TDD patch, the version of the first laptop (OAI's installer has no UHD package
#      path for Ubuntu 24.04); the USRP firmware images
#   2. OAI, branch gui-live-switch, next to this repository (../openairinterface5g_gui), built in
#      cmake_targets/ran_build_gui: nr-softmodem, nr-uesoftmodem and their plugins
#   3. model check: OAI's model code against the PyTorch golden vectors, for every demo model
#   4. passwordless sudo for the GUI user, for exactly the two softmodems and the commands that stop them
#   5. the GUI's Python environment and its tests
#
# Settings (environment): OAI_URL (git URL or local path), OAI_BRANCH, GUI_USER (default: owner of this
# repository), SKIP_UHD=1 (keep the UHD already installed), SKIP_SUDOERS=1.
set -euo pipefail

REPO=$(cd "$(dirname "$0")/.." && pwd)
BASE=$(dirname "$REPO")
OAI_DIR=$BASE/openairinterface5g_gui
OAI_URL=${OAI_URL:-https://github.com/guavamin/OAI_CSINET.git}
OAI_BRANCH=${OAI_BRANCH:-gui-live-switch}
GUI_USER=${GUI_USER:-$(stat -c %U "$REPO")}
BUILD=$OAI_DIR/cmake_targets/ran_build_gui/build
UHD_VERSION=4.6.0.0
MODELS=(capture_20261013_B/model1_packed_p16 capture_20261013_B/model2_packed_p16
        capture_20261013_B/model2_decoder_for_model1_source capture_20261013_B/model1_decoder_for_model2_source)

step() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
as_user() { sudo -u "$GUI_USER" -H "$@"; }

[[ $EUID -eq 0 ]] || { echo "run this from a root shell"; exit 1; }
[[ $GUI_USER != root ]] || { echo "the repository is owned by root: set GUI_USER to the desktop user"; exit 1; }
. /etc/os-release
echo "Ubuntu $VERSION_ID, GUI user $GUI_USER, OAI in $OAI_DIR"
export DEBIAN_FRONTEND=noninteractive
export GIT_PAGER=cat PAGER=cat  # OAI's installer prints git diffs / logs: never stop in a pager

step "1/5 system packages"
apt-get update -q
apt-get install -y -q git sudo lsb-release software-properties-common build-essential cmake ninja-build \
    python3-venv python3-pip iperf iproute2 pciutils

step "2/5 OAI $OAI_BRANCH"
if [[ ! -d $OAI_DIR/.git && ! -f $OAI_DIR/.git ]]; then
    as_user git clone --branch "$OAI_BRANCH" --single-branch "$OAI_URL" "$OAI_DIR"
else
    echo "already there: $OAI_DIR ($(as_user git -C "$OAI_DIR" log --oneline -1))"
fi
cd "$OAI_DIR/cmake_targets"
if [[ ! -x /opt/asn1c/bin/asn1c ]] || ! ldconfig -p | grep -q libuhd; then
    # OAI's own installer; the UHD part needs these two variables to build 4.6.0 from source
    if [[ ${SKIP_UHD:-0} == 1 ]]; then
        ./build_oai -I
    else
        UHD_VERSION=$UHD_VERSION BUILD_UHD_FROM_SOURCE=1 ./build_oai -I -w USRP
    fi
fi
uhd_config_info --version
ls /usr/local/share/uhd/images/usrp_b210_fpga.bin >/dev/null 2>&1 || /usr/local/lib/uhd/utils/uhd_images_downloader.py -t b2xx
as_user cmake -S "$OAI_DIR" -B "$BUILD" -GNinja -DOAI_USRP=ON -DENABLE_TELNETSRV=ON \
    -DASN1C_EXEC=/opt/asn1c/bin/asn1c >/dev/null
as_user ninja -C "$BUILD" nr-softmodem nr-uesoftmodem oai_usrpdevif telnetsrv telnetsrv_ciUE telnetsrv_5Gue \
    telnetsrv_ci telnetsrv_enb telnetsrv_bearer telnetsrv_rrc telnetsrv_o1 params_libconfig ldpc ldpc_orig dfts coding \
    rfsimulator

step "3/5 model check (OAI C code == PyTorch golden vectors)"
T=$(mktemp -d)
gcc -O2 -o "$T/test_ai_fb_transformer" "$OAI_DIR/tests/ai_fb/test_ai_fb_transformer.c" \
    "$OAI_DIR/openair2/LAYER2/NR_MAC_COMMON/ai_fb_transformer.c" -I"$OAI_DIR" -I"$OAI_DIR/openair2" \
    -I"$OAI_DIR/common/utils" -I"$OAI_DIR/common/utils/T" -I"$BUILD/common/utils/T" \
    -I"$OAI_DIR/openair2/LAYER2/NR_MAC_COMMON" -lm
for m in "${MODELS[@]}"; do
    b=$OAI_DIR/models/ai_fb_transformer/$m
    if "$T/test_ai_fb_transformer" "$b.bin" "$b.bin.golden.bin" "$b.preprocess.bin" >"$T/out" 2>&1; then
        echo "PASS $m"
    else
        tail -5 "$T/out"; echo "FAIL $m"; exit 1
    fi
done
rm -rf "$T"

step "4/5 passwordless sudo for $GUI_USER (softmodems and their stop commands only)"
if [[ ${SKIP_SUDOERS:-0} != 1 ]]; then
    # sudo applies the LAST matching rule: this file must sort after the others (an admin rule such as
    # "user ALL=(ALL) ALL" in a later file would ask for a password again)
    rule=/etc/sudoers.d/zz-pyoaicsinet
    rm -f /etc/sudoers.d/pyoaicsinet  # earlier name of this rule
    MODPROBE=$(PATH=/usr/sbin:/usr/bin:/sbin:/bin command -v modprobe)
    IP=$(PATH=/usr/sbin:/usr/bin:/sbin:/bin command -v ip)  # the same lookup as the GUI's "Bring up CAN"
    cat >"$rule.tmp" <<EOF
# ai-csi-feedback-demo: the GUI starts and stops the OAI softmodems without a password,
# and (UE station, Robot tab) brings up the robot's USB-to-CAN interface
$GUI_USER ALL=(root) NOPASSWD: $BUILD/nr-softmodem, $BUILD/nr-uesoftmodem, \\
    /usr/bin/pkill -TERM -x nr-softmodem, /usr/bin/pkill -KILL -x nr-softmodem, \\
    /usr/bin/pkill -TERM -x nr-uesoftmodem, /usr/bin/pkill -KILL -x nr-uesoftmodem, \\
    /usr/bin/kill -TERM -*, /usr/bin/kill -KILL -*, \\
    $MODPROBE gs_usb, $IP link set can0 up type can bitrate 500000
EOF
    chmod 440 "$rule.tmp"
    visudo -cf "$rule.tmp" && mv "$rule.tmp" "$rule"
    # a real test, not a listing: an allowed, harmless command (no such process group) must not ask for a password
    # ("--" matters: procps kill reads "-TERM -<n>" as its own group 0)
    if sudo -u "$GUI_USER" sudo -n /usr/bin/kill -TERM -- -2147483 2>&1 | grep -q "password is required"; then
        echo "sudo still asks $GUI_USER for a password: a later rule overrides $rule:"
        sudo -u "$GUI_USER" sudo -n -l
        exit 1
    fi
    echo "ok: $rule ($GUI_USER starts and stops the softmodems without a password)"
fi

if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
    # an active firewall drops the downlink traffic arriving on the UE's 5G interface (UE station) and the
    # station link if it does not come over Tailscale; both rules are harmless on the gNB laptop
    ufw allow in on oaitun_ue1 >/dev/null && echo "ufw: allowed incoming traffic on the UE's 5G interface (oaitun_ue1)"
    ufw status | grep -q "tailscale0" || echo "ufw: the station link (TCP 9200) must be allowed: 'ufw allow in on tailscale0'" \
        "for Tailscale, or 'ufw allow 9200/tcp' on a local network"
fi

step "5/5 GUI Python environment"
cd "$REPO"
as_user ./install.sh
as_user .venv/bin/pip install -q -r requirements-dev.txt
as_user .venv/bin/pip install -q -r requirements-robot.txt  # Robot tab (AgileX SDK); harmless without a robot
as_user .venv/bin/python -m pytest -q

step "done"
echo "OAI build: $BUILD"
echo "GUI:       cd $REPO && .venv/bin/python -m separate_laptops.ue_station.mockup   (as $GUI_USER)"
