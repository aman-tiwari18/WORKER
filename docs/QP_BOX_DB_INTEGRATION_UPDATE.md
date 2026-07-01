# QP Box Detection - Database Integration Update

## Changes Made

Updated the RTSP standalone script (`qp_box_det.py`) to use database integration instead of Excel files, matching the pattern used by other detection scripts like `close_proximity.py`.

## What Changed

### ❌ Removed
- Excel file dependency (`pandas` import)
- `EXCEL_FILE` configuration
- Excel-based camera loading
- Excel status tracking and updates
- DataFrame manipulation

### ✅ Added
- Database client integration (`from db_client import get_rtsp_cameras`)
- Direct database camera fetching
- Streamlined camera configuration loading
- Consistent with other detection scripts

## Updated Files

### 1. `script-server/scripts/detection/qp_box_det.py`

**Key Changes:**

1. **Imports**
   ```python
   # Removed
   import pandas as pd
   
   # Added
   from db_client import get_rtsp_cameras
   ```

2. **Configuration**
   ```python
   # Removed
   EXCEL_FILE = os.path.join(PROJECT_ROOT, "data/sheets/new_sheet1.xlsx")
   ```

3. **Camera Loading** - Now uses database:
   ```python
   def load_camera_config():
       """Load camera configurations from database filtered by target scenes."""
       exam_id = os.environ.get("EXAM_ID")
       
       if not exam_id:
           logging.error("EXAM_ID environment variable is required")
           return []
       
       exam_id_int = int(exam_id)
       rows = get_rtsp_cameras(exam_id_int)
       
       # Filter for Strong Room or Server Room cameras
       camera_configs = []
       for row in rows:
           classification = str(row.get("classification") or "")
           if classification in TARGET_SCENES:
               # Build camera config from DB row
               ...
       
       return camera_configs
   ```

4. **Camera Detection Function** - Simplified signature:
   ```python
   # Before
   def start_camera_detection(device_id, camera_no, token, df, index):
   
   # After
   def start_camera_detection(device_id, camera_no, token, cctv_url):
   ```

5. **Main Function** - No Excel operations:
   ```python
   def main():
       # Load from database
       camera_configs = load_camera_config()
       
       # No Excel initialization or updates
       # Direct camera processing
       ...
   ```

### 2. `QP_BOX_DETECTION_COMPLETE.md`
- Updated documentation to reflect database integration
- Removed Excel-related sections
- Added database environment variables

## Benefits

### 1. **Consistency**
- Matches pattern used by `close_proximity.py` and other detection scripts
- Uses centralized `db_client` module
- Consistent error handling and logging

### 2. **Simplified Architecture**
- No Excel file dependency
- Direct database queries
- Fewer moving parts

### 3. **Better Scalability**
- Database handles concurrent access
- No file locking issues
- Real-time camera updates

### 4. **Improved Reliability**
- Single source of truth (database)
- No file I/O errors
- Automatic camera filtering by classification

## Database Schema

The script queries the `camera_links_rtsp` table:

```sql
SELECT
    id, device_id, exam_id, centre_name, tc_code,
    camera_name, camera_no, city, state, cctv_url,
    is_active, classification, status
FROM camera_links_rtsp
WHERE exam_id = ? AND is_active = 1
```

Filters results by `classification` field matching `TARGET_SCENES`:
- "Server Room"
- "Strong Room"

## Usage

### Before (Excel-based)
```bash
# Required Excel file at: data/sheets/new_sheet1.xlsx
cd script-server/scripts/detection
python qp_box_det.py
```

### After (Database-based)
```bash
# Required: EXAM_ID environment variable
export EXAM_ID=123
cd script-server/scripts/detection
python qp_box_det.py
```

## Environment Variables

### Required
- `EXAM_ID`: Exam identifier (integer)

### Optional
- `DB_HOST`: Database host (default: "localhost")
- `DB_PORT`: Database port (default: 3306)
- `DB_USER`: Database user (default: "root")
- `DB_PASSWORD`: Database password
- `DB_NAME`: Database name (default: "cctv_monitoring")

## Testing

Run diagnostics to verify no errors:
```bash
# No syntax errors found
✅ script-server/scripts/detection/qp_box_det.py: No diagnostics found
```

## Migration Notes

### For Existing Deployments

1. **Ensure Database Access**
   - Verify database credentials in environment
   - Check `camera_links_rtsp` table exists
   - Confirm cameras have `classification` field populated

2. **Update Environment**
   ```bash
   export EXAM_ID=<your_exam_id>
   export DB_HOST=localhost
   export DB_USER=root
   export DB_PASSWORD=<your_password>
   ```

3. **Remove Excel Dependency**
   - No longer need `data/sheets/new_sheet1.xlsx`
   - Can remove pandas if not used elsewhere

4. **Verify Camera Classifications**
   ```sql
   SELECT device_id, camera_no, classification 
   FROM camera_links_rtsp 
   WHERE exam_id = ? 
     AND classification IN ('Server Room', 'Strong Room')
     AND is_active = 1;
   ```

## Compatibility

- ✅ Works with existing `db_client.py` module
- ✅ Uses same DB reporting functions
- ✅ Compatible with HLS worker (already uses DB)
- ✅ Matches pattern of other detection scripts

## Status

✅ **COMPLETE** - RTSP script now uses database integration, consistent with other detection scripts.
