# Exam ID in Detection Filenames Fix

## Problem
Detection image filenames were showing `exam0` instead of the correct exam ID (e.g., `exam123`). This affected all detection scripts when saving images.

Example of incorrect filename:
```
qp_box_12345_1_exam0_20240502_143022_a1b2c3d4.jpg
                  ↑ Should be actual exam_id, not 0
```

## Root Cause
The `camera_loader.py` utility was **not including `exam_id` and `shift_id`** in the camera configuration dictionaries that are passed to the detection workers.

### Flow of Data:
1. **camera_loader.py** queries database with `exam_id` and `shift_id`
2. Returns list of camera configs to master_pipeline
3. master_pipeline passes configs to workers
4. Workers extract `exam_id` from config to create filenames

**The problem:** Step 2 was missing - `exam_id` and `shift_id` were used for the query but not included in the returned camera configs.

## Solution
Added `exam_id` and `shift_id` to the camera configuration dictionaries in `camera_loader.py`.

### Changes Made

**File: `script-server/scripts/detection/utils/camera_loader.py`**

#### For HLS Cameras:
```python
# BEFORE
cameras.append({
    "cctv_url":       row["cctv_url"],
    "camera_id":      ch_code,
    "ch_no":          ch_code,
    "camera_id_pk":   str(row.get("id", "")),
    "classification": str(row.get("classification", "")).strip(),
    "link_type":      "HLS",
    "script_code":    script_code,
    "camera_name":    str(row.get("camera_name", "")).strip(),
    "centre_name":    str(row.get("centre_name", "")).strip(),
    "tc_code":        str(row.get("tc_code", "")).strip(),
})

# AFTER
cameras.append({
    "cctv_url":       row["cctv_url"],
    "camera_id":      ch_code,
    "ch_no":          ch_code,
    "camera_id_pk":   str(row.get("id", "")),
    "classification": str(row.get("classification", "")).strip(),
    "link_type":      "HLS",
    "script_code":    script_code,
    "camera_name":    str(row.get("camera_name", "")).strip(),
    "centre_name":    str(row.get("centre_name", "")).strip(),
    "tc_code":        str(row.get("tc_code", "")).strip(),
    "exam_id":        exam_id,      # ← ADDED
    "shift_id":       shift_id,     # ← ADDED
})
```

#### For RTSP Cameras:
```python
# BEFORE
cameras.append({
    "cctv_url":       row["cctv_url"],
    "camera_id":      device_id,
    "camera_id_pk":   db_id,
    "camera_no":      camera_no,
    "classification": str(row.get("classification", "")).strip(),
    "link_type":      "RTSP",
    "script_code":    script_code,
    "camera_name":    str(row.get("camera_name", "")).strip(),
    "centre_name":    str(row.get("centre_name", "")).strip(),
    "tc_code":        str(row.get("tc_code", "")).strip(),
})

# AFTER
cameras.append({
    "cctv_url":       row["cctv_url"],
    "camera_id":      device_id,
    "camera_id_pk":   db_id,
    "camera_no":      camera_no,
    "classification": str(row.get("classification", "")).strip(),
    "link_type":      "RTSP",
    "script_code":    script_code,
    "camera_name":    str(row.get("camera_name", "")).strip(),
    "centre_name":    str(row.get("centre_name", "")).strip(),
    "tc_code":        str(row.get("tc_code", "")).strip(),
    "exam_id":        exam_id,      # ← ADDED
    "shift_id":       shift_id,     # ← ADDED
})
```

## How It Works Now

### Data Flow:
1. **Execution created** with `exam_id=123`, `shift_id=456`, `script_code='qp_box'`
2. **camera_loader.py** queries database and creates camera configs:
   ```python
   {
       "camera_id": "12345",
       "ch_no": "1",
       "exam_id": 123,        # ← Now included
       "shift_id": 456,       # ← Now included
       "script_code": "qp_box",
       # ... other fields
   }
   ```
3. **master_pipeline.py** passes config to workers
4. **Workers** extract `exam_id` from config:
   ```python
   exam_id = cfg.get("exam_id", "0")  # Now gets 123 instead of defaulting to "0"
   ```
5. **Filename created** with correct exam_id:
   ```
   qp_box_12345_1_exam123_20240502_143022_a1b2c3d4.jpg
                      ↑ Correct exam_id!
   ```

## Affected Workers
All detection workers now receive correct `exam_id` and `shift_id`:

✅ qp_box_det_worker.py
✅ close_proximity_worker.py
✅ crowd_five_det_worker.py
✅ intruder_worker.py
✅ invigilator_worker.py
✅ server_room_worker.py
✅ strongroom_worker.py
✅ entry_exit_worker.py
✅ camera_tampering_worker.py
✅ crowd_det_biometric_worker.py
✅ phone_worker.py
✅ blind_spot_worker.py
✅ camera_inversion_worker.py
✅ crowd_zero_det_worker.py
✅ crowd_worker.py

## Testing
To verify the fix:

1. Start a detection script execution with a known exam_id (e.g., 123)
2. Wait for detections to be saved
3. Check the detection image filenames in the results folder
4. Verify filename contains `exam123` instead of `exam0`

Example correct filename:
```
HLS:  qp_box_ch54703_exam123_20240502_143022_a1b2c3d4.jpg
RTSP: qp_box_12345_1_exam123_20240502_143022_a1b2c3d4.jpg
```

## Files Modified
- `script-server/scripts/detection/utils/camera_loader.py`

## Summary
✅ `exam_id` and `shift_id` now included in camera configs
✅ All workers receive correct exam_id
✅ Detection filenames show correct exam_id instead of 0
✅ Both HLS and RTSP cameras fixed
✅ All 15+ detection workers benefit from this fix
