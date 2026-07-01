# Camera Upload Duplicate Prevention Flow

## Upload Process Flow

```
┌─────────────────────────────────────────────────────────────────┐
│                     USER UPLOADS EXCEL FILE                      │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│              BACKEND: Parse Excel & Extract Records              │
│  • Read all rows from Excel                                      │
│  • Normalize headers (case-insensitive)                          │
│  • Extract required fields based on link type                    │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Validate Required Fields                      │
│  RTSP: device_id, cctv_url required                             │
│  HLS:  cctv_url required                                        │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ▼
                    ┌────────┴────────┐
                    │  Link Type?     │
                    └────┬───────┬────┘
                         │       │
                    HLS  │       │  RTSP
                         │       │
         ┌───────────────▼       ▼───────────────┐
         │                                        │
         ▼                                        ▼
┌─────────────────────┐              ┌─────────────────────────┐
│   HLS Unique Key    │              │    RTSP Unique Key      │
│  tc_code + ch_code  │              │ device_id + camera_no   │
│                     │              │      + tc_code          │
└──────────┬──────────┘              └───────────┬─────────────┘
           │                                     │
           │                                     │
           └──────────────┬──────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│          Fetch Existing Cameras from Database                    │
│  • Query by exam_id                                              │
│  • Get relevant fields for unique key generation                 │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│              Generate Unique Keys for Existing Cameras           │
│  • Create Set of existing keys                                   │
│  • Format: "field1_field2_field3"                               │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│              Compare Each New Record Against Existing            │
│                                                                   │
│  FOR EACH record in validRecords:                               │
│    ├─ Generate unique key from record                           │
│    ├─ Check if key exists in Set                                │
│    ├─ If EXISTS → Skip (add to skipped count)                   │
│    └─ If NEW → Add to recordsToInsert                           │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│                  Bulk Insert New Records Only                    │
│  • Insert recordsToInsert into database                          │
│  • Skip duplicate records                                        │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Generate Response Message                     │
│                                                                   │
│  IF skipped > 0:                                                │
│    "Successfully uploaded X cameras.                             │
│     Skipped Y duplicates (unique_key_fields already exists)."   │
│  ELSE:                                                           │
│    "Successfully uploaded X cameras."                            │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│                    FRONTEND: Display Result                      │
│  • Show success/warning alert                                    │
│  • Display count of uploaded cameras                             │
│  • Display count of skipped duplicates                           │
│  • Show which unique key was used                                │
│  • Reload camera list                                            │
└─────────────────────────────────────────────────────────────────┘
```

## Unique Key Generation Examples

### RTSP Example

```
Excel Row:
┌──────────────┬────────────┬─────────┬──────────────┐
│  Device ID   │ Camera No  │ TC Code │  CCTV URL    │
├──────────────┼────────────┼─────────┼──────────────┤
│  1000728     │     4      │  TC001  │  rtsp://...  │
└──────────────┴────────────┴─────────┴──────────────┘
                      │
                      ▼
            Unique Key Generation
                      │
                      ▼
        "1000728_4_TC001"
                      │
                      ▼
        Check if exists in database
                      │
         ┌────────────┴────────────┐
         │                         │
    EXISTS                      NEW
         │                         │
         ▼                         ▼
      SKIP                     INSERT
```

### HLS Example

```
Excel Row:
┌──────────┬──────────┬──────────────┬──────────────┐
│ TC Code  │ Ch Code  │ Camera Name  │  CCTV URL    │
├──────────┼──────────┼──────────────┼──────────────┤
│  TC001   │  54703   │  Lab 1       │  https://... │
└──────────┴──────────┴──────────────┴──────────────┘
                 │
                 ▼
       Unique Key Generation
                 │
                 ▼
          "TC001_54703"
                 │
                 ▼
   Check if exists in database
                 │
    ┌────────────┴────────────┐
    │                         │
EXISTS                      NEW
    │                         │
    ▼                         ▼
  SKIP                     INSERT
```

## Comparison Logic

### Set-Based Lookup (O(1) Performance)

```javascript
// Step 1: Build existing keys Set
const existingKeys = new Set([
  "1000728_4_TC001",
  "1000728_5_TC001",
  "1000648_6_TC002"
]);

// Step 2: Check each new record
const newRecords = [
  { device_id: "1000728", camera_no: "4", tc_code: "TC001" },  // Duplicate
  { device_id: "1000728", camera_no: "6", tc_code: "TC001" },  // New
  { device_id: "1000999", camera_no: "1", tc_code: "TC003" }   // New
];

// Step 3: Filter
const toInsert = newRecords.filter(record => {
  const key = `${record.device_id}_${record.camera_no}_${record.tc_code}`;
  return !existingKeys.has(key);  // O(1) lookup
});

// Result: 2 records to insert, 1 skipped
```

## Edge Cases Handled

### 1. Missing Optional Fields

```
RTSP Record:
device_id: "1000728"
camera_no: null          ← Optional field
tc_code: "TC001"

Generated Key: "1000728__TC001"
                        ↑
                  Empty string for null
```

### 2. Incomplete Unique Key

```
HLS Record:
tc_code: "TC001"
ch_code: null           ← Required for unique key

Action: INSERT (fail-safe, no duplicate check)
Reason: Cannot generate complete unique key
```

### 3. All Fields Present

```
RTSP Record:
device_id: "1000728"
camera_no: "4"
tc_code: "TC001"

Generated Key: "1000728_4_TC001"
Action: Check against existing keys
```

## Performance Characteristics

```
┌─────────────────────────┬──────────────┬─────────────────┐
│      Operation          │ Complexity   │   Performance   │
├─────────────────────────┼──────────────┼─────────────────┤
│ Fetch existing cameras  │ O(n)         │ Single DB query │
│ Build Set of keys       │ O(n)         │ Linear scan     │
│ Check each new record   │ O(m)         │ m lookups       │
│ Set lookup per record   │ O(1)         │ Constant time   │
│ Total                   │ O(n + m)     │ Very efficient  │
└─────────────────────────┴──────────────┴─────────────────┘

Where:
  n = number of existing cameras
  m = number of new records in Excel
```

## User Experience Flow

```
1. User clicks "Upload Excel"
   ↓
2. Selects file with 100 cameras
   ↓
3. Clicks "Upload"
   ↓
4. Backend processes:
   • 60 cameras already exist (skipped)
   • 40 cameras are new (inserted)
   ↓
5. User sees alert:
   ⚠️ "Successfully uploaded 40 cameras.
       Skipped 60 duplicates 
       (device_id + camera_no + tc_code already exists)."
   ↓
6. Camera list refreshes showing all cameras
```

## Database State Changes

### Before Upload
```sql
SELECT COUNT(*) FROM camera_links_rtsp WHERE exam_id = 1;
-- Result: 60 cameras
```

### After Upload (100 records in Excel)
```sql
SELECT COUNT(*) FROM camera_links_rtsp WHERE exam_id = 1;
-- Result: 100 cameras (60 existing + 40 new)
```

### Duplicate Prevention Verified
```sql
-- No duplicates exist
SELECT device_id, camera_no, tc_code, COUNT(*) as count
FROM camera_links_rtsp 
WHERE exam_id = 1
GROUP BY device_id, camera_no, tc_code
HAVING count > 1;
-- Result: 0 rows (no duplicates)
```
