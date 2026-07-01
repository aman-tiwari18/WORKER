# Detection Cooldown Periods - Updated Configuration

## Overview
Updated cooldown periods across all detection workers to reduce the volume of detections and align with analytics frequency requirements.

## Cooldown Period Changes

| Detection Type | Worker File | Old Cooldown | New Cooldown | Reason |
|----------------|-------------|--------------|--------------|--------|
| **QP Box Detection** | `qp_box_det_worker.py` | 240s (4 min) | **300s (5 min)** | Reduce detection volume |
| **Intruder Detection** | `intruder_worker.py` | **0s (NONE!)** ⚠️ | **60s (1 min)** | Match analytics requirement (1 min frequency) |
| **Crowd Entry/Exit** | `crowd_worker.py` | 240s (4 min) | **600s (10 min)** | Match analytics requirement (10 min frequency) |
| **Crowd Biometric** | `crowd_det_biometric_worker.py` | 240s (4 min) | **600s (10 min)** | Match analytics requirement (10 min frequency) |
| **Entry/Exit Movement** | `entry_exit_worker.py` | 180s (3 min) | **120s (2 min)** | Match analytics requirement (2 min frequency) |
| **Invigilator** | `invigilator_worker.py` | 180s (3 min) | **420s (7 min)** | Match analytics requirement (7 min frequency) |
| **Close Proximity** | `close_proximity_worker.py` | 180s (3 min) | **120s (2 min)** | Match analytics requirement (2 min frequency) |
| **Mobile Phone** | `phone_worker.py` | 480s (8 min) | **600s (10 min)** | Reduce false positives |
| **Candidate Count < 5** | `crowd_zero_det_worker.py` | 240s (4 min) | **360s (6 min)** | Reduce detection volume |
| **Crowd Threshold** | `crowd_five_det_worker.py` | 240s (4 min) | **120s (2 min)** | Code is the source of truth; doc previously claimed 360s. Worker default is `cooldown_time=120` — kept in the 2026-05 accuracy overhaul to surface low-occupancy events promptly. |
| **Camera Inversion** | `camera_inversion_worker.py` | 120s (2 min) | **240s (4 min)** | Reduce false positives |

## Analytics Requirements Alignment

Based on the analytics table from the requirements:

### Strong Room Cameras
1. **Intruder Detection**
   - Required Frequency: **1 Minute**
   - Cooldown Set: **60 seconds (1 min)** ✅
   - Time Window: 07:00:00 - 13:30:00

2. **QP Box Detection**
   - Required Frequency: Not specified
   - Cooldown Set: **300 seconds (5 min)**
   - Time Window: 07:00:00 - 13:30:00

### Entry/Exit Cameras
1. **Crowd Detection**
   - Required Frequency: **10 Minutes / 5 Minutes**
   - Cooldown Set: **600 seconds (10 min)** ✅
   - Time Window: 11:00:00 - 14:00:00

2. **Movement Detection**
   - Required Frequency: **2 Minutes**
   - Cooldown Set: **120 seconds (2 min)** ✅
   - Time Window: 14:00:00 - 18:00:00

### Biometric Cameras
1. **Crowd Detection**
   - Required Frequency: **10 Minutes / 5 Minutes**
   - Cooldown Set: **600 seconds (10 min)** ✅
   - Time Window: 11:00:00 - 14:00:00

### Inside Classroom Cameras
1. **Invigilator Not Moving**
   - Required Frequency: **7 Minutes**
   - Cooldown Set: **420 seconds (7 min)** ✅
   - Time Window: 14:00:00 - 18:00:00

2. **Close Proximity**
   - Required Frequency: **2 Minutes**
   - Cooldown Set: **120 seconds (2 min)** ✅
   - Time Window: 14:00:00 - 18:00:00

3. **Mobile Phone Detection**
   - Required Frequency: **1.5 Minutes**
   - Cooldown Set: **600 seconds (10 min)** ⚠️ (Conservative to reduce false positives)
   - Time Window: 14:00:00 - 18:00:00

4. **Candidate Count < 5**
   - Required Frequency: Not specified
   - Cooldown Set: **120 seconds (2 min)**
   - Time Window: 14:00:00 - 18:00:00

## Impact Analysis

### Expected Detection Volume Reduction

**Before Changes:**
- Intruder: **Unlimited** (no cooldown) → Could generate hundreds per hour
- Crowd Entry/Exit: ~15 detections/hour
- Crowd Biometric: ~15 detections/hour
- Phone: ~7.5 detections/hour

**After Changes:**
- Intruder: **Max 60 detections/hour** (1 per minute)
- Crowd Entry/Exit: **Max 6 detections/hour** (1 per 10 minutes)
- Crowd Biometric: **Max 6 detections/hour** (1 per 10 minutes)
- Phone: **Max 6 detections/hour** (1 per 10 minutes)

### Total Reduction
- **Intruder:** 🔻 Massive reduction (from unlimited to controlled)
- **Crowd:** 🔻 60% reduction (15 → 6 per hour)
- **Phone:** 🔻 20% reduction (7.5 → 6 per hour)
- **Overall:** 🔻 Significant reduction in total detection volume

## Configuration Details

### How Cooldown Works

```python
def _gpu_batch_worker(stop_event, save_folder, cooldown_time=300):
    # cooldown_time is in seconds
    
    state = item.get('state', {})
    now = time.time()
    
    # Check if enough time has passed since last save
    if now - state.get("last_save", 0) < cooldown_time:
        continue  # Skip this detection
    
    # Save detection and update timestamp
    state["last_save"] = now
    cv2.imwrite(path, annotated_frame)
```

**Key Points:**
- Cooldown is **per camera** (tracked by device_id/camera_no)
- Timer starts **after each saved detection**
- Detections still run continuously, but saves are throttled
- State is maintained in memory during worker lifetime

## Customization Guide

### To Adjust Cooldown for a Specific Detection:

1. **Open the worker file:**
   ```bash
   nano script-server/scripts/detection/workers/{worker_name}_worker.py
   ```

2. **Find the `_gpu_batch_worker` function:**
   ```python
   def _gpu_batch_worker(stop_event, save_folder, cooldown_time=300):
   ```

3. **Change the default value:**
   ```python
   # Example: Change to 5 minutes (300 seconds)
   def _gpu_batch_worker(stop_event, save_folder, cooldown_time=300):
   
   # Example: Change to 10 minutes (600 seconds)
   def _gpu_batch_worker(stop_event, save_folder, cooldown_time=600):
   ```

4. **Restart the detection service**

### Quick Reference - Seconds to Minutes

| Seconds | Minutes | Common Use |
|---------|---------|------------|
| 60 | 1 min | High-priority alerts |
| 90 | 1.5 min | Mobile phone detection |
| 120 | 2 min | Movement, proximity |
| 180 | 3 min | General monitoring |
| 240 | 4 min | Moderate frequency |
| 300 | 5 min | Standard frequency |
| 360 | 6 min | Low frequency |
| 420 | 7 min | Invigilator monitoring |
| 480 | 8 min | Very low frequency |
| 600 | 10 min | Crowd detection |

## Monitoring Recommendations

### 1. Track Detection Rates
```bash
# Count detections per hour for each type
grep "SAVED" /path/to/logs | grep "[Intruder]" | wc -l
grep "SAVED" /path/to/logs | grep "[Crowd]" | wc -l
grep "SAVED" /path/to/logs | grep "[CrowdBiometric]" | wc -l
```

### 2. Monitor Cooldown Skips
Add logging to track how many detections are skipped due to cooldown:
```python
if now - state.get("last_save", 0) < cooldown_time:
    logger.debug(f"[{script_name}] Skipped due to cooldown")
    continue
```

### 3. Database Query for Detection Volume
```sql
-- Detections per hour by type
SELECT 
    folder,
    DATE_FORMAT(created_at, '%Y-%m-%d %H:00:00') as hour,
    COUNT(*) as detection_count
FROM detections
WHERE created_at >= NOW() - INTERVAL 24 HOUR
GROUP BY folder, hour
ORDER BY hour DESC, detection_count DESC;
```

## Troubleshooting

### Too Many Detections Still?
**Increase cooldown time:**
- Current: 300s → Try: 600s (double it)
- Current: 120s → Try: 240s (double it)

### Missing Important Detections?
**Decrease cooldown time:**
- Current: 600s → Try: 300s (half it)
- Current: 420s → Try: 210s (half it)

### Different Cooldown for Different Cameras?
Currently, cooldown is global per detection type. To implement per-camera cooldown:
1. Store cooldown in camera configuration
2. Pass it through the frame packet
3. Use it in the worker logic

## Testing Checklist

After updating cooldown periods:

- [ ] Restart all detection workers
- [ ] Monitor logs for first hour
- [ ] Count detections per camera type
- [ ] Verify cooldown is working (check timestamps)
- [ ] Check for missed critical events
- [ ] Verify database entries have correct timestamps
- [ ] Monitor disk space usage (should decrease)
- [ ] Check alert system is not overwhelmed
- [ ] Validate with stakeholders on detection frequency
- [ ] Document any further adjustments needed

## Rollback Instructions

If you need to revert to old cooldown values:

```bash
# Backup current files
cp script-server/scripts/detection/workers/*.py /backup/location/

# Use git to revert (if using version control)
git checkout HEAD -- script-server/scripts/detection/workers/*_worker.py

# Or manually edit each file back to old values
```

**Old Values Reference:**
- QP Box: 240s
- Intruder: 0s (no cooldown)
- Crowd: 240s
- Entry/Exit: 180s
- Invigilator: 180s
- Phone: 480s
- Close Proximity: 180s
- Crowd Zero: 240s
- Crowd Five: 240s
- Camera Inversion: 120s

## Notes

- Cooldown periods are **independent per camera**
- Cooldown timer resets **after each saved detection**
- Detection inference still runs continuously (only saves are throttled)
- Cooldown does not affect real-time monitoring displays
- Database load will be significantly reduced
- Storage requirements will decrease proportionally
- Alert fatigue should be reduced for operators
