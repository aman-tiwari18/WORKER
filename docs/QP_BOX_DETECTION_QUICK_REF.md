# QP Box Detection - Quick Reference

## What It Does
Detects when Question Paper (QP) boxes are **MISSING** from strong room cameras. Alerts when white/grey/brown boxes are not visible in the frame.

## Files
1. **HLS Worker**: `script-server/scripts/detection/workers/qp_box_det_worker.py`
2. **RTSP Script**: `script-server/scripts/detection/qp_box_det.py`

## Quick Start

### Run RTSP Detection
```bash
cd script-server/scripts/detection
python qp_box_det.py
```

### Use HLS Worker (in master_pipeline)
```python
from workers.qp_box_det_worker import qp_box_det_worker_hls
qp_box_det_worker_hls(camera_configs)
```

## Key Settings

| Setting | Value | Description |
|---------|-------|-------------|
| Target Scenes | Server Room, Strong Room | Only these cameras monitored |
| Colors Detected | White, Grey, Brown | QP box colors |
| Box Size Range | 5,000 - 200,000 pixels | Valid box area |
| Cooldown | 240 seconds | Time between alerts per camera |
| Rate Limit | 25/minute | Max alerts globally |
| Confidence | 0.45 | YOLO detection threshold |

## Detection Logic
1. ✅ **YOLO finds box-like object** (backpack, handbag, suitcase, book)
2. ✅ **Size is 5K-200K pixels**
3. ✅ **Color matches white/grey/brown** (>30% match)
4. ✅ **QP Box Present** → No alert
5. ❌ **QP Box NOT Found** → **ALERT & SAVE**

## Output
- **Saved to**: `results/tcs_detections/qp_box_missing/`
- **Image shows**: "QP BOX NOT DETECTED!" warning
- **DB Report**: Automatic for both HLS and RTSP

## Environment Variables
```bash
export EXAM_ID=123              # Required
export SHIFT_ID=1               # Optional (HLS)
export LINK_TYPE=HLS            # HLS or RTSP
```

## Troubleshooting

### No Detections?
- Check camera is in "Server Room" or "Strong Room"
- Verify YOLO model exists at `models/yolov10m.pt`
- Check if QP box colors match (white/grey/brown)

### Too Many Alerts?
- Increase `CONFIDENCE` from 0.45 to 0.55
- Narrow color ranges in `COLOR_RANGES`
- Increase `MIN_BOX_AREA` threshold

### Missed Detections?
- Decrease `CONFIDENCE` to 0.35
- Widen color ranges
- Decrease `MIN_BOX_AREA` threshold
- Check box size is within 5K-200K pixels

## Color Ranges (HSV)
```python
white: [0,0,180] to [180,30,255]   # Bright, low saturation
grey:  [0,0,80] to [180,50,180]    # Medium brightness
brown: [10,50,50] to [20,255,200]  # Orange-brown hue
```

## Architecture
- **HLS**: GPU-batched worker (efficient for many cameras)
- **RTSP**: Multi-threaded standalone (independent processing)

## Status
✅ Ready for testing with real strong room footage
