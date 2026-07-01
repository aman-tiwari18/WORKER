# Camera Upload Duplicate Prevention System

## Overview
Implemented a unique key-based duplicate prevention system for camera uploads via Excel. When uploading cameras for existing exams, the system now compares unique keys and skips duplicate entries, only inserting new cameras.

## Unique Key Strategy

### RTSP Cameras
**Unique Key:** `device_id + camera_no + tc_code`

- **device_id**: Primary identifier for the device
- **camera_no**: Camera number on that device
- **tc_code**: Test center code

**Example:**
- Device: `1000728`, Camera: `4`, TC: `TC001` → Key: `1000728_4_TC001`
- If this combination exists, the camera will be skipped during upload

### HLS Cameras
**Unique Key:** `tc_code + ch_code`

- **tc_code**: Test center code
- **ch_code**: Channel code (unique identifier for HLS streams)

**Example:**
- TC: `TC001`, Channel: `54703` → Key: `TC001_54703`
- If this combination exists, the camera will be skipped during upload

## Implementation Details

### Backend Changes (`CameraController.js`)

#### 1. Duplicate Detection Logic
```javascript
// For HLS
const existingKeys = new Set(
    existingCameras
        .filter(c => c.tc_code && c.ch_code)
        .map(c => `${c.tc_code}_${c.ch_code}`)
);

// For RTSP
const existingKeys = new Set(
    existingCameras
        .filter(c => c.device_id)
        .map(c => `${c.device_id}_${c.camera_no || ''}_${c.tc_code || ''}`)
);
```

#### 2. Filtering Process
- Fetch all existing cameras for the exam
- Create unique keys from existing records
- Compare incoming records against existing keys
- Filter out duplicates
- Insert only new records

#### 3. Response Message
```javascript
const message = skipped > 0
    ? `Successfully uploaded ${recordsToInsert.length} camera(s). 
       Skipped ${skipped} duplicate(s) 
       (${linkType === 'HLS' ? 'tc_code + ch_code' : 'device_id + camera_no + tc_code'} already exists).`
    : `Successfully uploaded ${recordsToInsert.length} camera(s).`;
```

### Frontend Changes (`Cameras.jsx`)

#### 1. Upload Dialog Information
Added clear information about duplicate detection:
```jsx
<Typography variant="caption">
  <strong>Duplicate Detection:</strong> 
  {isHLS 
    ? 'Cameras with same TC Code + Ch Code will be skipped.' 
    : 'Cameras with same Device ID + Camera No + TC Code will be skipped.'}
</Typography>
```

#### 2. Upload Result Display
Enhanced result alert to show:
- Success/Warning severity based on whether duplicates were found
- Number of cameras uploaded
- Number of duplicates skipped
- Which unique key combination was used

## Usage Workflow

### For Existing Exams

1. **Initial Upload**
   - Upload Excel with camera data
   - All valid cameras are inserted
   - Response: "Successfully uploaded X cameras"

2. **Subsequent Uploads**
   - Upload Excel with mix of new and existing cameras
   - System compares unique keys
   - Only new cameras are inserted
   - Response: "Successfully uploaded X cameras. Skipped Y duplicates (unique_key_fields already exists)"

### Example Scenarios

#### RTSP Example
**Existing Camera:**
- Device ID: `1000728`
- Camera No: `4`
- TC Code: `TC001`

**New Upload Contains:**
1. Device: `1000728`, Camera: `4`, TC: `TC001` → **SKIPPED** (duplicate)
2. Device: `1000728`, Camera: `5`, TC: `TC001` → **INSERTED** (new camera number)
3. Device: `1000648`, Camera: `4`, TC: `TC001` → **INSERTED** (new device)

#### HLS Example
**Existing Camera:**
- TC Code: `TC001`
- Ch Code: `54703`

**New Upload Contains:**
1. TC: `TC001`, Ch: `54703` → **SKIPPED** (duplicate)
2. TC: `TC001`, Ch: `54704` → **INSERTED** (new channel)
3. TC: `TC002`, Ch: `54703` → **INSERTED** (new test center)

## Benefits

1. **Prevents Duplicates**: No more duplicate camera entries in the database
2. **Flexible Updates**: Can re-upload entire Excel files without creating duplicates
3. **Clear Feedback**: Users know exactly how many cameras were added vs skipped
4. **Maintains Data Integrity**: Unique key combinations ensure logical uniqueness
5. **Handles Partial Data**: Works even when some fields are null/empty

## Technical Notes

### Null Handling
- Records with incomplete unique keys are still inserted (fail-safe)
- Empty strings are treated as empty in key generation
- RTSP: `camera_no` can be null (treated as empty string in key)
- HLS: Both `tc_code` and `ch_code` must exist for duplicate check

### Performance
- Single database query to fetch existing cameras
- In-memory Set for O(1) lookup performance
- Efficient even with thousands of cameras

### Database Constraints
- No database-level unique constraints added
- Application-level duplicate prevention
- Allows flexibility for edge cases

## Testing Checklist

- [x] Upload new cameras to empty exam → All inserted
- [x] Re-upload same Excel → All skipped
- [x] Upload mix of new and existing → Only new inserted
- [x] Upload with missing tc_code → Handled gracefully
- [x] Upload with missing camera_no (RTSP) → Handled gracefully
- [x] Upload with missing ch_code (HLS) → Handled gracefully
- [x] Verify correct count in success message
- [x] Verify correct unique key shown in message

## Future Enhancements

1. **Update Existing**: Option to update existing cameras instead of skipping
2. **Detailed Report**: Download Excel showing which rows were skipped/inserted
3. **Validation Preview**: Show duplicates before upload
4. **Bulk Update**: Allow updating specific fields for existing cameras
5. **Audit Log**: Track who uploaded/skipped which cameras

## Related Files

### Backend
- `cctv_backend/src/controllers/CameraController.js` - Main upload logic

### Frontend
- `cctv_frontend/src/pages/Cameras.jsx` - Upload UI
- `cctv_frontend/src/services/api.js` - API integration

## API Response Format

### Success with No Duplicates
```json
{
  "success": true,
  "message": "Successfully uploaded 15 cameras."
}
```

### Success with Duplicates
```json
{
  "success": true,
  "message": "Successfully uploaded 10 cameras. Skipped 5 duplicates (device_id + camera_no + tc_code already exists)."
}
```

### Error
```json
{
  "success": false,
  "message": "No valid records found. Required RTSP columns: Device ID, Server Address..."
}
```
