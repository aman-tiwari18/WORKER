# RTSP Camera Classification Field Support

## Problem
When uploading RTSP camera lists via Excel, there was no option to include the "Classification" (or "Room Classification") field. This field is important for filtering cameras by detection scripts (e.g., "Inside Classroom", "Strong Room", "Entry/Exit", "Biometric").

## Solution
Added support for the classification field in RTSP camera uploads, making it optional (can be null if not provided).

---

## Changes Made

### 1. Backend: CameraController.js

**File:** `cctv_backend/src/controllers/CameraController.js`

Added classification field extraction for RTSP cameras:

```javascript
// RTSP
const device_id = pick(row, "Device ID", "device_id", ...);
const camera_name = pick(row, "Camera Name", "camera_name");
const camera_no = pick(row, "Camera No", "camera_no", ...);
const cctv_url = pick(row, "Server Address", "CCTV URL", ...);

// ← ADDED: Extract classification from multiple possible column names
const classification = pick(
    row,
    "Classification",
    "classification",
    "Room Classification",
    "room_classification",
    "room classification"
) || null;

if (!device_id || !cctv_url) continue;

validRecords.push({
    exam_id: examId,
    device_id,
    centre_name: pick(row, "Centre Name", ...),
    tc_code: pick(row, "TC Code", ...),
    camera_name,
    camera_no: camera_no || null,
    city: pick(row, "City", ...),
    state: pick(row, "State", ...),
    cctv_url,
    classification,  // ← ADDED to record
    is_active: true,
});
```

**Updated error message** to mention classification field:

```javascript
const missingColumnsMessage =
    linkType === "RTSP"
        ? `No valid records found. Required RTSP columns: Device ID, Server Address (or CCTV URL). 
           Optional: Camera No, TC Code, Camera Name, Centre/Center Name, City, State, 
           Classification (or Room Classification). 
           Detected headers: ${detectedHeaders.join(", ")}`
        : "No valid records found. Make sure the Excel contains a 'CCTV URL' column.";
```

### 2. Frontend: Cameras.jsx

**File:** `cctv_frontend/src/pages/Cameras.jsx`

Updated the upload dialog info message:

```javascript
<Alert severity="info" sx={{ mb: 2 }}>
  {isHLS
    ? 'Expected HLS columns: TC Code, Centre Name, City, State, Camera Name, ch_no, CCTV URL, Camera Category, Classification (optional)'
    : 'Expected RTSP columns: Device ID, TC Code, Camera Name, Centre Name, City, State, Server Address, Classification (or Room Classification - optional)'}
  <br /><strong>Download the template below to get the exact format.</strong>
</Alert>
```

---

## Supported Column Names

The backend now recognizes these column names for classification (case-insensitive):

- `Classification`
- `classification`
- `Room Classification`
- `room_classification`
- `room classification`

**Note:** The field is **optional** - if not provided or empty, it will be stored as `null`.

---

## Excel Format Example

### RTSP Camera Upload Format:

| Camera No | TC Code | Camera Name | Centre Name | City | State | Server Address | Room Classification | Status | Frame Link |
|-----------|---------|-------------|-------------|------|-------|----------------|---------------------|--------|------------|
| 1 | 270610 | CLASS ROOM 1 | PC JABIN PU COLLEGE | VIJAYANAGARPHUBALLI | KARNATAKA | 01.camview.in | Inside Classroom | Online | View Frame |
| 2 | 270610 | CLASS ROOM 3 | PC JABIN PU COLLEGE | VIJAYANAGARPHUBALLI | KARNATAKA | 01.camview.in | Inside Classroom | Online | View Frame |
| 3 | 270610 | STRONG ROOM | PC JABIN PU COLLEGE | VIJAYANAGARPHUBALLI | KARNATAKA | 01.camview.in | Strong Room | Online | View Frame |
| 4 | 270610 | ENTRY/EXIT | PC JABIN PU COLLEGE | VIJAYANAGARPHUBALLI | KARNATAKA | 01.camview.in | Entry/Exit | Online | View Frame |

### Classification Values (Examples):

- `Inside Classroom` - For invigilator, proximity, phone, crowd_zero, crowd_five detection
- `Strong Room` - For intruder, strongroom, qp_box detection
- `Entry/Exit` - For crowd, entry_exit detection
- `Biometric` - For crowd_biometric detection
- `Server Room` - For server detection
- *(empty/null)* - Camera will be processed by scripts that accept all classifications

---

## How It Works

### Upload Process:
1. User uploads Excel file with RTSP camera data
2. Backend reads the file and extracts data from each row
3. For each row, it looks for classification in multiple possible column names
4. If classification column exists and has a value → stores it
5. If classification column is empty or doesn't exist → stores `null`
6. Cameras are inserted into the database with classification field

### Detection Script Filtering:
When detection scripts run, they filter cameras based on classification:

```python
# From camera_loader.py
SCRIPT_CLASSIFICATION_MAP = {
    "proximity":       ["inside classroom", "inside_classroom", "classroom"],
    "crowd":           ["entry/exit", "entry_exit", "entryexit"],
    "crowd_biometric": ["biometric"],
    "intruder":        ["strong room", "strongroom", "strong_room"],
    "strongroom":      ["strong room", "strongroom", "strong_room"],
    "qp_box":          ["strong room", "strongroom", "strong_room"],
    # ... etc
}
```

Cameras with `null` classification are included by default (unless script has specific requirements).

---

## Benefits

✅ **Flexible Upload** - Classification field is optional, won't break existing uploads
✅ **Better Filtering** - Detection scripts can target specific camera types
✅ **Multiple Column Names** - Supports "Classification" or "Room Classification"
✅ **Backward Compatible** - Existing uploads without classification still work
✅ **Consistent with HLS** - Both HLS and RTSP now support classification field

---

## Testing

### Test Upload with Classification:
1. Create Excel with RTSP cameras
2. Add "Room Classification" column with values like "Inside Classroom", "Strong Room", etc.
3. Upload via Cameras page
4. Verify classification is saved in database
5. Run detection scripts and verify they filter cameras correctly

### Test Upload without Classification:
1. Create Excel with RTSP cameras
2. Don't include classification column (or leave it empty)
3. Upload via Cameras page
4. Verify cameras are saved with `null` classification
5. Verify detection scripts still process these cameras

---

## Files Modified

1. ✅ `cctv_backend/src/controllers/CameraController.js` - Added classification field extraction for RTSP
2. ✅ `cctv_frontend/src/pages/Cameras.jsx` - Updated upload dialog info message

---

## Summary

✅ **RTSP cameras now support classification field** during Excel upload
✅ **Field is optional** - can be null if not provided
✅ **Multiple column names supported** - "Classification" or "Room Classification"
✅ **Detection scripts filter correctly** based on classification
✅ **Backward compatible** - existing uploads still work

The classification field helps detection scripts target the right cameras! 🎉
