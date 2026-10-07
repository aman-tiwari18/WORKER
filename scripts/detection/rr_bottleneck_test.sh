#!/bin/bash
# Round-robin coverage A/B test on THIS host only (worker-0), on the SSC_test exam.
#
# Runs master_pipeline with the same 7 analytics + camera types as the 13:30 group, on
# this worker's normal 1/5 share of SSC_test cameras, twice:
#   A = current behaviour   (6 streams per Camview server, every port probed on every visit)
#   B = fixes               (6 streams per DVR device, ≤ MAX_SERVER per Camview server,
#                            port-probe results remembered for the run)
# Each run logs "[RR-stats]" every 60 s: captured / attempts / busy skips and how many
# distinct cameras were captured at least once.
#
#   bash scripts/detection/rr_bottleneck_test.sh            # A then B, 8 min each
#   MINUTES=5 MAX_SERVER=30 bash scripts/detection/rr_bottleneck_test.sh
#   ONLY=B bash scripts/detection/rr_bottleneck_test.sh     # just one of them
#
# Logs: scripts/logs/rr_test_A.log, scripts/logs/rr_test_B.log
# Detections are reported under exam 43 (SSC_test), shift 159.
set -u
cd "$(dirname "$0")/../.."                        # script-server/
MINUTES=${MINUTES:-8}
MAX_SERVER=${MAX_SERVER:-40}
ONLY=${ONLY:-AB}
ANALYTICS="server entry_exit proximity phone crowd_zero crowd_five crowd_five_invigilator"

run() {   # $1 = name, rest = extra env
  local name=$1; shift
  local log=scripts/logs/rr_test_${name}${LOGTAG:-}.log
  echo "[$(date +%T)] run $name for ${MINUTES} min -> $log"
  env "$@" EXAM_ID=43 SHIFT_ID=159 LINK_TYPE=RTSP RR_STATS_EVERY_S=60 PYTHONPATH="$PWD" \
      CAMERA_CLASSIFICATIONS_SERVER='["Server Room"]' \
      CAMERA_CLASSIFICATIONS_ENTRY_EXIT='["Outside"]' \
      CAMERA_CLASSIFICATIONS_PROXIMITY='["Inside Lab"]' \
      CAMERA_CLASSIFICATIONS_PHONE='["Inside Lab", "Server Room"]' \
      CAMERA_CLASSIFICATIONS_CROWD_ZERO='["Inside Lab"]' \
      CAMERA_CLASSIFICATIONS_CROWD_FIVE='["Inside Lab"]' \
      CAMERA_CLASSIFICATIONS_CROWD_FIVE_INVIGILATOR='["Inside Lab"]' \
      timeout -s TERM --kill-after=30 $((MINUTES * 60)) \
      venv/bin/python -u scripts/detection/pipeline/master_pipeline.py --analytics $ANALYTICS > "$log" 2>&1
  echo "[$(date +%T)] run $name finished"
  grep -E "ROUND-ROBIN:|\[RR\] starting|\[RR-stats\]" "$log" | sed -E 's/^[0-9-]+ [0-9:,]+ - INFO - //'
  echo
}

[[ $ONLY == *A* ]] && run A RR_LIMIT_KEY=server PORT_PROBE_CACHE_S=0
[[ $ONLY == *A* && $ONLY == *B* ]] && { echo "waiting 60 s so the DVRs release run A's streams"; sleep 60; }
[[ $ONLY == *B* ]] && run B RR_LIMIT_KEY=device MAX_STREAMS_PER_DVR="${PER_DEVICE:-6}" MAX_STREAMS_PER_SERVER="$MAX_SERVER" PORT_PROBE_CACHE_S=1800
