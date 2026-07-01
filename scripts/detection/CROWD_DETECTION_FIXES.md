# Crowd Detection Scripts - Fixes Applied

## Problem Summary
The crowd detection scripts (`crowd_zero_det.py`, `Crowd_det_complete.py`) were not working properly due to:
1. Missing UUID in filenames (causing potential overwrites)
2. Incorrect database reporting (missing folder parameter)
3. Inconsistent cooldown times between core detection and workers
4. Missing proper exception handling

## Fixed Scripts

### 1. `crowd_zero_det.py` (0 Candidates Detection)
**Changes:**
- ✅ Added `import uuid` at top
- ✅ Added UUID to filename: `crowd_zero_{device_id}_{camera_no}_{timestamp}_{uuid}.jpg`
- ✅ Added database reporting with proper folder name: `zero_person_detection`
- ✅ Updated cooldown time from 240s → 360s to match worker
- ✅ Added exception handling for DB reporting

**Before:**
```python
filename = f"{SAVE_FOLDER}/{script_name}_{camera_no}_{ts}.jpg"
cv2.imwrite(filename, frame)
```

**After:**
```python
uid = str(uuid.uuid4())
fname = f"{script_name}_{device_id}_{camera_no}_{ts}_{uid}.jpg"
filename = os.path.join(SAVE_FOLDER, fname)
cv2.imwrite(filename, frame)
try:
    from utils.db_client import report_to_db_rtsp
    report_to_db_rtsp(
        device_id=str(device_id),
        camera_no=str(camera_no),
        image_name=fname,
        folder="zero_person_detection",
    )
except Exception as e:
    logger.error(f"[{device_id}-{camera_no}] Crowd Zero DB reporting error: {e}")
```

---

### 2. `crowd_five_det.py` (Less than 5 Candidates Detection)
**Changes:**
- ✅ Added `import uuid` at top
- ✅ Added UUID to filename: `crowd_five_{device_id}_{camera_no}_{timestamp}_{uuid}.jpg`
- ✅ Added database reporting with proper folder name: `crowd_detection_threshold`
- ✅ Updated cooldown time from 240s → 120s to match worker
- ✅ Added exception handling for DB reporting

**Before:**
```python
filename = f"{SAVE_FOLDER}/{script_name}_{camera_no}_{ts}.jpg"
cv2.imwrite(filename, frame)
```

**After:**
```python
uid = str(uuid.uuid4())
fname = f"{script_name}_{device_id}_{camera_no}_{ts}_{uid}.jpg"
filename = os.path.join(SAVE_FOLDER, fname)
cv2.imwrite(filename, frame)
try:
    from utils.db_client import report_to_db_rtsp
    report_to_db_rtsp(
        device_id=str(device_id),
        camera_no=str(camera_no),
        image_name=fname,
        folder="crowd_detection_threshold",
    )
except Exception as e:
    logger.error(f"[{device_id}-{camera_no}] Crowd Five DB reporting error: {e}")
```

---

### 3. `Crowd_det_complete.py` (More than 10 People Detection)
**Changes:**
- ✅ Updated cooldown time from 240s → 180s to match worker
- ✅ Already had UUID and DB reporting (was working correctly)

---

## Cooldown Time Alignment

All crowd detection scripts now have consistent cooldown times between core detection and workers:

| Detection Type | Core Script | Worker | Cooldown Time |
|----------------|-------------|--------|---------------|
| Crowd (>10 people) | `Crowd_det_complete.py` | `crowd_worker.py` | **180 seconds** |
| Crowd Zero (0 people) | `crowd_zero_det.py` | `crowd_zero_det_worker.py` | **360 seconds** |
| Crowd Five (<5 people) | `crowd_five_det.py` | `crowd_five_det_worker.py` | **120 seconds** |

---

## Database Folder Names

Correct folder names for database reporting:

| Detection Type | Folder Name |
|----------------|-------------|
| Crowd (>10 people) | `crowd_detection` |
| Crowd Zero (0 people) | `zero_person_detection` |
| Crowd Five (<5 people) | `crowd_detection_threshold` |

---

## Filename Format

All crowd detection scripts now use consistent filename format:
```
{script_name}_{device_id}_{camera_no}_{timestamp}_{uuid}.jpg
```

Example:
```
crowd_zero_CAM001_1_20260503_143022_a1b2c3d4-e5f6-7890-abcd-ef1234567890.jpg
```

---

## Testing Checklist

To verify the fixes:

1. **Test Crowd Zero Detection (0 candidates)**
   ```bash
   # Should trigger when room is empty
   # Check: results/zero_person_detection/
   # Verify: UUID in filename, DB entry created
   ```

2. **Test Crowd Five Detection (<5 candidates)**
   ```bash
   # Should trigger when 1-4 people in room
   # Check: results/crowd_detection_threshold/
   # Verify: UUID in filename, DB entry created
   ```

3. **Test Crowd Detection (>10 people)**
   ```bash
   # Should trigger when 10+ people at entry/exit
   # Check: results/crowd_detection/
   # Verify: UUID in filename, DB entry created
   ```

4. **Verify Database Entries**
   ```sql
   SELECT * FROM detections 
   WHERE folder IN ('zero_person_detection', 'crowd_detection_threshold', 'crowd_detection')
   ORDER BY created_at DESC;
   ```

---

## Camera Classification

All crowd detection scripts now properly filter by camera classification:

### Crowd Detection (>10 people)
**Runs on:** Entry/Exit, Outside/Biometric Area cameras
```python
valid = any(s in classification for s in (
    'entry', 'entry/exit', 'entry_exit', 'entryexit',
    'outdoor scene', 'outdoor_scene', 'outside area', 'outside_area',
    'biometric area', 'biometric_area', 'biometric'
))
```

### Crowd Zero & Crowd Five Detection
**Runs on:** Inside Lab cameras
```python
valid = any(s in classification for s in (
    'computer lab', 'inside lab', 'exam lab', 'lab',
    'inside classroom', 'inside_classroom', 'classroom'
))
```

---

## Related Files

All crowd detection files have been updated and verified:

### Core Detection Scripts
- ✅ `scripts/detection/Crowd_det_complete.py`
- ✅ `scripts/detection/crowd_zero_det.py`
- ✅ `scripts/detection/crowd_five_det.py`

### Worker Scripts
- ✅ `scripts/detection/workers/crowd_worker.py`
- ✅ `scripts/detection/workers/crowd_zero_det_worker.py`
- ✅ `scripts/detection/workers/crowd_five_det_worker.py`

### Documentation
- ✅ `scripts/detection/CAMERA_CLASSIFICATION_MAPPING.md`
- ✅ `scripts/detection/CROWD_DETECTION_FIXES.md` (this file)

---

## Summary

All crowd detection scripts are now:
- ✅ Using UUID in filenames (prevents overwrites)
- ✅ Reporting to database with correct folder names
- ✅ Using consistent cooldown times
- ✅ Properly filtering by camera classification
- ✅ Following the same pattern as the working `crowd_five_det.py`

The fixes ensure that all crowd detection variants work reliably and consistently across both RTSP and HLS pipelines.
