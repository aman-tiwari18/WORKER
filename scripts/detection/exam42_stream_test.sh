#!/bin/bash
# Kept-open (persistent) substream test of exam 42's real analytics, on THIS host only (worker-0),
# against the SSC_test copy of exam 42's cameras (exam 43). Camera types and alert frequencies are
# exam 42's own assignments (5 Oct). Where one analytic has several assignments with different
# camera types (crowd, entry_exit, phone) the union is used, since one process takes one list.
# monitor_tamper is left out: its RF-DETR model does not load when started by hand on worker-0.
#
#   bash scripts/detection/exam42_stream_test.sh                 # 1000 streams, 8 min
#   STREAMS=1200 MINUTES=6 bash scripts/detection/exam42_stream_test.sh
cd "$(dirname "$0")/../.."
export UNIFIED_PERSON_MODEL=finetuned                          # production person model (yolo26m)
export CAMERA_CLASSIFICATIONS_CROWD='["Entry/Exit", "Inside Lab", "Outside", "Biometric"]'
export CAMERA_CLASSIFICATIONS_CROWD_FIVE='["Inside Lab"]'
export CAMERA_CLASSIFICATIONS_CROWD_FIVE_INVIGILATOR='["Inside Lab"]'
export CAMERA_CLASSIFICATIONS_CROWD_ZERO='["Inside Lab"]'
export CAMERA_CLASSIFICATIONS_ENTRY_EXIT='["Outside", "Entry/Exit"]'
export CAMERA_CLASSIFICATIONS_INTRUDER='["Inside Lab", "Server Room"]'
export CAMERA_CLASSIFICATIONS_INVERSION='["Inside Lab", "Server Room", "Outside"]'
export CAMERA_CLASSIFICATIONS_INVIGILATOR='["Inside Lab"]'
export CAMERA_CLASSIFICATIONS_PHONE='["Outside", "Inside Lab", "Server Room"]'
export CAMERA_CLASSIFICATIONS_PROXIMITY='["Inside Lab"]'
export CAMERA_CLASSIFICATIONS_SERVER='["Server Room"]'
# Alert frequency (s between saved alerts per camera) = custom_frequency_minutes of the assignments.
export COOLDOWN_TIME_CROWD=120 COOLDOWN_TIME_CROWD_FIVE=120 COOLDOWN_TIME_CROWD_FIVE_INVIGILATOR=420 \
       COOLDOWN_TIME_CROWD_ZERO=600 COOLDOWN_TIME_ENTRY_EXIT=120 COOLDOWN_TIME_INTRUDER=120 \
       COOLDOWN_TIME_INVIGILATOR=420 COOLDOWN_TIME_PHONE=120 COOLDOWN_TIME_PROXIMITY=120 COOLDOWN_TIME_SERVER=120
export COOLDOWN_TIME=600                                       # read by the inversion worker
ANALYTICS="crowd crowd_five crowd_five_invigilator crowd_zero entry_exit intruder inversion invigilator phone proximity server" \
STREAMS=${STREAMS:-1000} MINUTES=${MINUTES:-8} DSS_STREAM_TYPE=${DSS_STREAM_TYPE:-2} LOGTAG=${LOGTAG:-_exam42} \
  bash scripts/detection/persistent_stream_test.sh
