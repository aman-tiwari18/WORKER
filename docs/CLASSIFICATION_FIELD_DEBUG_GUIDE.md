# Classification Field Upload Debug Guide

## Issue
The classification field is showing empty when uploading RTSP camera Excel sheets, even though the column "Scene Classification" exists in the Excel file.

## Changes Made

### 1. Backend Controller Updates (`cctv_backend/src/controllers/CameraController.js`)

Added comprehensive header matching for classification field:
- "Classification"
- "Scene Classification" 
- "Room Classification"
- All case variations and with/without spaces/underscores

Added debug logging to track:
- Raw headers from Excel
- Classification values being extracted
- Sample records before database insertion

### 2. Migration Runner Created (`cctv_backend/src/migrations/run_009_migration.js`)

Created migration runner to ensure the `classification` column exists in the database.

**To run the migration:**
```bash
cd cctv_backend
node src/migrations/run_009_migration.js
```

### 3. Excel Header Debug Tool (`cctv_backend/test_excel_headers.js`)

Created a tool to analyze Excel files and see exactly what headers are being read.

**To use:**
```bash
cd cctv_backend
node test_excel_headers.js /path/to/your/excel/file.xlsx
```

This will show:
- All detected headers with exact character codes
- How headers are normalized
- Sample values from first 3 rows

## Debugging Steps

### Step 1: Verify Database Column Exists
```bash
cd cctv_backend
node src/migrations/run_009_migration.js
```

Expected output: "✓ Classification column already exists" or "✓ Successfully added classification column"

### Step 2: Analyze Your Excel File
```bash
cd cctv_backend
node test_excel_headers.js /path/to/your/rtsp_cameras.xlsx
```

Look for:
- Is "Scene Classification" in the headers list?
- What is the normalized version? (should be "sceneclassification")
- Are there any hidden characters or extra spaces?
- Do the sample values show the classification data?

### Step 3: Test Upload with Debug Logging

1. Restart the backend server to enable debug logging
2. Upload your Excel file through the frontend
3. Check the backend console logs for:
   ```
   [DEBUG] Row for device XXXXX:
   {
     rawHeaders: [...],
     classificationValue: "...",
     rawRow: {...}
   }
   ```
4. Check for:
   ```
   [DEBUG] Sample records to insert (first 3):
   [
     { device_id: "...", classification: "...", camera_name: "..." }
   ]
   ```

### Step 4: Verify Database After Upload

Connect to your database and check:
```sql
SELECT device_id, camera_name, classification 
FROM camera_links_rtsp 
WHERE exam_id = YOUR_EXAM_ID 
ORDER BY id DESC 
LIMIT 10;
```

## How the Header Matching Works

The `pick` function normalizes headers by:
1. Converting to lowercase
2. Removing all non-alphanumeric characters (spaces, dashes, underscores)

Examples:
- "Scene Classification" → "sceneclassification"
- "scene_classification" → "sceneclassification"  
- "SCENE CLASSIFICATION" → "sceneclassification"
- "Room Classification" → "roomclassification"

The backend tries to match against all these variations:
```javascript
pick(row,
    "Classification",
    "classification",
    "Scene Classification",
    "scene_classification",
    "scene classification",
    "SceneClassification",
    "sceneclassification",
    "SCENE CLASSIFICATION",
    "Room Classification",
    "room_classification",
    "room classification",
    "RoomClassification",
    "roomclassification",
    "ROOM CLASSIFICATION",
    "Room_Classification"
)
```

## Common Issues and Solutions

### Issue 1: Column name has extra spaces
**Solution:** The normalization removes all spaces, so this should work automatically.

### Issue 2: Column name has special characters
**Solution:** The normalization removes all special characters, so this should work automatically.

### Issue 3: Values are empty in Excel
**Solution:** The field is optional. If the cell is empty, it will be stored as `null` in the database.

### Issue 4: Database column doesn't exist
**Solution:** Run the migration: `node src/migrations/run_009_migration.js`

### Issue 5: Excel has merged cells or complex formatting
**Solution:** Use the test tool to see how xlsx library is parsing the file. You may need to simplify the Excel format.

## Expected Behavior

When uploading an RTSP camera list:
1. If "Scene Classification" column exists with values → values are saved to database
2. If "Scene Classification" column exists but empty → `null` is saved to database
3. If "Scene Classification" column doesn't exist → `null` is saved to database
4. No errors should occur in any case (field is optional)

## Next Steps

1. Run the migration to ensure database column exists
2. Use the test tool to analyze your Excel file
3. Upload the file and check debug logs
4. Verify data in database
5. Share the output from steps 1-4 if issue persists
