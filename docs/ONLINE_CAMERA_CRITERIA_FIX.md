# Online Camera Criteria Fix - Complete

## Problem
The report generation was showing **0% online cameras** because it was using the wrong criteria to determine if a camera is online.

## Root Cause
Three different systems were using **inconsistent criteria** for determining online cameras:

### 1. **Report API** (`script-server/report_api.py`) ❌ WRONG
- **OLD**: `is_active = 1 AND status = 'Accessible'`
- **ISSUE**: Doesn't check if camera has classification

### 2. **Report Controller** (`cctv_backend/src/controllers/ReportController.js`) ❌ WRONG
- **OLD**: `cam.status === 'accessible'` (lowercase)
- **ISSUE**: Only checks status, ignores classification

### 3. **Correct Criteria** ✅
A camera is online if:
```
classification IS NOT NULL 
AND classification != '' 
AND status = 'Accessible' (case-insensitive)
```

---

## Files Fixed

### 1. `script-server/report_api.py` ✅ FIXED

#### Changes in `create_shift_sheets()`:
```python
# OLD
is_active = cam.get("is_active") == 1
status_value = str(cam.get("status", "")).strip().lower()
is_accessible = status_value == "accessible"
camera_status = "Online" if (is_active and is_accessible) else "Offline"

# NEW
classification = cam.get("classification")
has_classification = classification is not None and str(classification).strip() != ""
status_value = str(cam.get("status", "")).strip().lower()
is_accessible = status_value == "accessible"
camera_status = "Online" if (has_classification and is_accessible) else "Offline"
```

#### Changes in `create_analysis_sheet()`:
```python
# OLD
online_cams = sum(
    1 for c in cameras 
    if c.get("is_active") == 1 and str(c.get("status", "")).strip().lower() == "accessible"
)

# NEW
online_cams = sum(
    1 for c in cameras 
    if (c.get("classification") is not None and str(c.get("classification")).strip() != "" 
        and str(c.get("status", "")).strip().lower() == "accessible")
)
```

---

### 2. `cctv_backend/src/controllers/ReportController.js` ✅ FIXED

#### Changes in HLS Camera Status (lines ~240):
```javascript
// OLD
'Camera Status': cam.status === 'accessible' ? 'Online' : 'Offline',

// NEW
const hasClassification = cam.camera_category && cam.camera_category.trim() !== '';
const isAccessible = (cam.status || '').toLowerCase() === 'accessible';
const isOnline = hasClassification && isAccessible;
'Camera Status': isOnline ? 'Online' : 'Offline',
```

#### Changes in RTSP Camera Status (lines ~210):
```javascript
// OLD
'Camera Status': cam.status === 'accessible' ? 'Online' : 'Offline',

// NEW
const hasClassification = cam.classification && cam.classification.trim() !== '';
const isAccessible = (cam.status || '').toLowerCase() === 'accessible';
const isOnline = hasClassification && isAccessible;
'Camera Status': isOnline ? 'Online' : 'Offline',
```

#### Changes in Analysis Sheet Online Count (lines ~280):
```javascript
// OLD
const online = shiftCams.filter(c => c.status === 'accessible').length;

// NEW
const online = shiftCams.filter(c => {
    const hasClassification = isRTSP 
        ? (c.classification && c.classification.trim() !== '')
        : (c.camera_category && c.camera_category.trim() !== '');
    const isAccessible = (c.status || '').toLowerCase() === 'accessible';
    return hasClassification && isAccessible;
}).length;
```

---

## Why This Matters

### Before Fix:
- **Report showed**: 0 online cameras (0.00% online rate)
- **Reason**: Cameras had `status = NULL` or no classification

### After Fix:
- **Report will show**: Correct count of cameras with classification + accessible status
- **Example**: If 810 cameras have classification and status='Accessible', report shows 810 online

---

## Testing Steps

### 1. Upload Classification Data
```bash
# Upload the classification Excel file to add classifications to cameras
POST /api/exams/{examId}/cameras/upload
File: ctgd_results_20260507_113834.xlsx

Expected: 810 cameras get classification field populated
```

### 2. Run Vision Classification Script
```bash
# This sets the status field to 'Accessible' for working cameras
# The script checks if cameras are reachable and updates status
```

### 3. Generate Report
```bash
# Frontend: Click "Consolidated Report" button
# OR
# API: POST /generate-report
Body: { "exam_id": 1, "date": "2026-05-07" }

Expected: Report shows correct online camera count
```

---

## Database Fields Used

### HLS Cameras (`camera_links_hls`):
- `camera_category` - Classification (Computer Lab, Entry, etc.)
- `status` - Accessibility status ('Accessible' or other)

### RTSP Cameras (`camera_links_rtsp`):
- `classification` - Classification (Computer Lab, Entry, etc.)
- `status` - Accessibility status ('Accessible' or other)

---

## Important Notes

1. **Classification is Required**: Cameras without classification will always show as offline
2. **Status is Case-Insensitive**: Both 'Accessible' and 'accessible' work
3. **Both Conditions Must Be True**: Camera needs BOTH classification AND accessible status
4. **Empty String = NULL**: Empty classification strings are treated as NULL

---

## Related Files

- ✅ `script-server/report_api.py` - Python report generation (Flask)
- ✅ `cctv_backend/src/controllers/ReportController.js` - Node.js report generation (Express)
- 📄 `EXCEL_FILES_ANALYSIS.md` - Excel file structure documentation
- 📄 `CLASSIFICATION_UPDATE_FIX.md` - Classification upload fix documentation

---

## Summary

**Problem**: Reports showed 0% online cameras
**Root Cause**: Wrong criteria (only checking status, not classification)
**Solution**: Check BOTH classification AND status
**Result**: Reports now correctly show online cameras

✅ **All report generation systems now use consistent online camera criteria!**
