#!/bin/bash
# Persistent-stream capacity test on THIS host only (worker-0), on the SSC_test exam.
#
# Instead of round-robin (open -> 2 frames -> close, again and again), every camera gets
# ONE stream kept open for the whole run, like Monitor Tampering does, and a frame is
# sampled every 1/SAMPLE_FPS seconds. Up to STREAMS cameras are opened at once.
#
#   bash scripts/detection/persistent_stream_test.sh
#   STREAMS=400 MINUTES=4 SAMPLE_FPS=0.5 bash scripts/detection/persistent_stream_test.sh
#
# Every 20 s it records: streams connected (distinct cameras), cameras failing,
# frames analysed, CPU load, free memory, GPU, network. A watchdog stops the run if
# free memory < MIN_FREE_GB or real CPU busy > MAX_CPU% for 2 readings in a row (this host
# also runs the DB/backend). Load average is shown but not used: it spikes when ~800
# producer processes start at once even though the CPU is mostly idle.
# Log: scripts/logs/persistent_test_<STREAMS>.log ; summary: ..._<STREAMS>.summary
set -u
cd "$(dirname "$0")/../.."                        # script-server/
STREAMS=${STREAMS:-800}
MINUTES=${MINUTES:-6}
SAMPLE_FPS=${SAMPLE_FPS:-0.25}
MIN_FREE_GB=${MIN_FREE_GB:-15}
ANALYTICS=${ANALYTICS:-crowd_zero}        # space-separated, e.g. "crowd_zero furniture inversion tamper"
CLASSES=${CLASSES:-'["Inside Lab"]'}       # camera types, applied to every analytic
MAX_CPU=${MAX_CPU:-92}
GRACE_S=${GRACE_S:-120}            # ignore CPU for the first GRACE_S s (all producers starting at once)
LOG=scripts/logs/persistent_test_${STREAMS}${LOGTAG:-}.log
SUM=scripts/logs/persistent_test_${STREAMS}${LOGTAG:-}.summary
NIC=$(ip route get 8.8.8.8 | grep -oP 'dev \K\S+')

echo "[$(date +%T)] persistent-stream test: [$ANALYTICS] up to $STREAMS streams, $SAMPLE_FPS frames/s each, $MINUTES min -> $LOG" | tee "$SUM"
# Per-analytic camera types: an exported CAMERA_CLASSIFICATIONS_<ANALYTIC> wins, else $CLASSES.
CLS_ENV=(); for a in $ANALYTICS; do v="CAMERA_CLASSIFICATIONS_${a^^}"; [ -z "${!v:-}" ] && CLS_ENV+=("$v=$CLASSES"); done
env EXAM_ID=43 SHIFT_ID=159 LINK_TYPE=RTSP PYTHONPATH="$PWD" "${CLS_ENV[@]}" \
    DSS_STREAM_TYPE="${DSS_STREAM_TYPE:-1}" CONTINUOUS_SCRIPTS="${ANALYTICS// /,}" MAX_CONCURRENT="$STREAMS" SAMPLE_FPS="$SAMPLE_FPS" \
    UNIFIED_STATS_EVERY_S=30 \
    timeout -s TERM --kill-after=30 $((MINUTES * 60)) \
    venv/bin/python -u scripts/detection/pipeline/master_pipeline.py --analytics $ANALYTICS > "$LOG" 2>&1 &
RUN=$!
START=$(date +%s)

rx0=$(cat /sys/class/net/$NIC/statistics/rx_bytes); t0=$(date +%s)
printf "%-8s %6s %10s %8s %6s %7s %7s %10s %10s  %s\n" time elapsed connected failing cpu% load1 freeGB gpu_mem gpu_util "net_in" | tee -a "$SUM"
read -r _ u n sy i w q sq st _ < /proc/stat; cpu_t0=$((u+n+sy+i+w+q+sq+st)); cpu_i0=$((i+w)); hot=0
while kill -0 $RUN 2>/dev/null; do
  sleep 20
  now=$(date +%s); rx=$(cat /sys/class/net/$NIC/statistics/rx_bytes)
  mbps=$(( (rx - rx0) * 8 / 1000000 / (now - t0 > 0 ? now - t0 : 1) )); rx0=$rx; t0=$now
  conn=$(grep "Connected successfully" "$LOG" | sed -E 's/.*→ //' | sort -u | wc -l)
  fail=$(tail -n 4000 "$LOG" | grep "All URLs failed" | sed -E 's/.*→ ([0-9_]+).*/\1/' | sort -u | wc -l)
  read -r _ u n sy i w q sq st _ < /proc/stat; ct=$((u+n+sy+i+w+q+sq+st)); ci=$((i+w))
  cpu=$(( 100 - 100 * (ci - cpu_i0) / (ct - cpu_t0 > 0 ? ct - cpu_t0 : 1) )); cpu_t0=$ct; cpu_i0=$ci
  load=$(cut -d' ' -f1 /proc/loadavg); free=$(free -g | awk '/Mem/{print $7}')
  gpu=$(nvidia-smi --query-gpu=memory.used,utilization.gpu --format=csv,noheader,nounits | tr -d ' ')
  printf "%-8s %5ss %10s %8s %6s %7s %7s %10s %10s  %s\n" "$(date +%T)" "$(( $(date +%s) - $(stat -c %Y "$SUM") ))" "$conn" "$fail" "$cpu" "$load" "$free" "${gpu%,*}MiB" "${gpu#*,}%" "${mbps}Mbps" | tee -a "$SUM"
  if [ $(( $(date +%s) - START )) -ge "$GRACE_S" ] && [ "$cpu" -gt "$MAX_CPU" ]; then hot=$((hot + 1)); else hot=0; fi
  if [ "$free" -lt "$MIN_FREE_GB" ] || [ "$hot" -ge 2 ]; then
    echo "[$(date +%T)] WATCHDOG: free=${free}G cpu=${cpu}% (x$hot) — stopping the test" | tee -a "$SUM"
    kill -TERM $RUN; break
  fi
done
wait $RUN 2>/dev/null
# Producer/detector children can outlive a stopped pipeline (orphaned): stop any left over.
sleep 5
for p in $(pgrep -f "multiprocessing|master_pipeline"); do
  tr '\0' '\n' < /proc/$p/environ 2>/dev/null | grep -q "^MAX_CONCURRENT=$STREAMS$" && \
  tr '\0' '\n' < /proc/$p/environ 2>/dev/null | grep -q "^EXAM_ID=43$" && kill -9 "$p" 2>/dev/null && echo "cleaned leftover pid $p" | tee -a "$SUM"
done
echo "[$(date +%T)] finished" | tee -a "$SUM"
echo "--- detector frame stats" | tee -a "$SUM"
grep -E "\[Unified\] last" "$LOG" | sed -E 's/^.*\[Unified\] //' | tee -a "$SUM"
echo "--- per-analytic activity (log lines mentioning each)" | tee -a "$SUM"
for a in $ANALYTICS; do echo "  $a: started=$(grep -ciE "Started (shared worker for: $a|UNIFIED detector worker.*$a)" "$LOG") saved=$(grep -ciE "($a|${a}_detection).*(saved|SAVED)" "$LOG") errors=$(grep -ciE "Traceback|Error.*$a" "$LOG")" | tee -a "$SUM"; done
echo "--- streams: $(grep 'Connected successfully' "$LOG" | sed -E 's/.*→ //' | sort -u | wc -l) cameras connected at least once; $(grep 'All URLs failed' "$LOG" | sed -E 's/.*→ ([0-9_]+).*/\1/' | sort -u | wc -l) cameras failed at least once" | tee -a "$SUM"
