#!/usr/bin/env bash
# Raw CSI data collection with the gNB and the UE on separate laptops (legacy CSI, rank 1, CSI-RS every 80 ms):
# the same settings as the 10/13 and 10/15 captures (identical config files), with the GUI's OAI build.
#
#   gNB laptop (root):  bash collect.sh gnb <session> <run>
#   UE laptop  (root):  bash collect.sh ue  <session> <run>
#
#   <session>  e.g. outdoor_dynamic        <run>  capture (one run: the training recipe keeps its last block as the test set)
#   data:      ~/Desktop/InterDigital_Project/ota_sessions/<session>/<run>/   (the same path on both laptops)
#              UE:  csi/ue/csi_reports.csv + csi/ue/csi_rs_channels/H_*.bin  (the training data), ue.log
#              gNB: csi/gnb/*.csv, gnb.log
#   CLOCK=internal  for no external 10 MHz / PPS (default: external, the GPS references)
#
# Stop with Ctrl+C (UE first, then gNB). Starting the UE again continues in the same folder.
set -euo pipefail

ROLE=${1:?usage: collect.sh gnb|ue <session> <run>}
SESSION=${2:?usage: collect.sh gnb|ue <session> <run>}
RUN=${3:?usage: collect.sh gnb|ue <session> <run>}
CLOCK=${CLOCK:-external}
REPO=$(cd "$(dirname "$0")/.." && pwd)
BASE=$(dirname "$REPO")
OWNER=$(stat -c %U "$REPO")
OAI=$BASE/openairinterface5g_gui
BUILD=$OAI/cmake_targets/ran_build_gui/build
CONF=$OAI/targets/PROJECTS/GENERIC-NR-5GC/myconfig
DIR=$BASE/ota_sessions/$SESSION/$RUN

[[ $EUID -eq 0 ]] || { echo "run this from a root shell"; exit 1; }
[[ $ROLE == gnb || $ROLE == ue ]] || { echo "role must be gnb or ue"; exit 1; }
[[ $CLOCK == external || $CLOCK == internal ]] || { echo "CLOCK must be external or internal"; exit 1; }
for name in nr-softmodem nr-uesoftmodem; do
    if pgrep -x "$name" >/dev/null; then echo "$name is already running: close the demo GUI / stop it first"; exit 1; fi
done

mkdir -p "$DIR/csi/$ROLE" "$DIR/config_snapshot"
chown "$OWNER:" "$BASE/ota_sessions"  # created here as root: the user must be able to add sessions (unzip, mkdir)
chown -R "$OWNER:" "$BASE/ota_sessions/$SESSION"
if [[ $ROLE == gnb ]]; then
    cp -n "$CONF/gnb.sa.band78.51prb.usrpb200.conf" "$DIR/config_snapshot/"
    ARGS=(./nr-softmodem -O "$DIR/config_snapshot/gnb.sa.band78.51prb.usrpb200.conf"
          --log_config.global_log_options utc_time -E --ai-fb-force-rank1 1 --print-csi-debug 0
          --ai-fb-impl-mode 0 --ai-fb-pucch-replace 0 --csi-record-path "$DIR/csi/gnb"
          "--RUs.[0].clock_src" "$CLOCK" "--RUs.[0].time_src" "$CLOCK")
else
    cp -n "$CONF/ue.conf" "$DIR/config_snapshot/"
    C=$([[ $CLOCK == external ]] && echo 1 || echo 0)
    ARGS=(./nr-uesoftmodem -C 3609120000 -r 51 --numerology 1 --ssb 234 --ue-fo-compensation -E
          --uicc0.imsi 001010000000001 --if_freq 2500000000 --ue-nb-ant-rx 2 --ue-nb-ant-tx 2
          --clock-source "$C" --time-source "$C" --ue-rxgain 106 --ai-fb-force-rank1 1 --print-csi-debug 0
          --ai-fb-impl-mode 0 --ai-fb-pucch-replace 0 --csi-record-path "$DIR/csi/ue"
          -O "$DIR/config_snapshot/ue.conf")
fi
# what was run, for later (appended on every start: a UE restart adds a line)
{
    echo "$(date -u +%FT%TZ) host=$(hostname) role=$ROLE clock=$CLOCK oai=$(git -C "$OAI" rev-parse --short HEAD)"
    echo "  cd $BUILD && ${ARGS[*]}"
} >> "$DIR/commands_$ROLE.txt"
chown "$OWNER:" "$DIR/commands_$ROLE.txt"
echo "recording to $DIR (clock $CLOCK); stop with Ctrl+C"
cd "$BUILD"
"${ARGS[@]}" 2>&1 | tee -a "$DIR/$ROLE.log"
