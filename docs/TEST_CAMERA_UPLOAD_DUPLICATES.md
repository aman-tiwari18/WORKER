# Camera Upload Duplicate Prevention - Test Cases

## Test Scenarios

### Test 1: Upload to Empty Exam (Baseline)
**Setup:**
- Exam with 0 cameras
- Excel with 10 cameras

**Expected Result:**
- ✅ All 10 cameras inserted
- ✅ Message: "Successfully uploaded 10 cameras."
- ✅ No duplicates skipped

**SQL Verification:**
```sql
SELECT COUNT(*) FROM camera_links_rtsp WHERE exam_id = 1;
-- Expected: 10
```

---

### Test 2: Re-upload Same Excel (100% Duplicates)
**Setup:**
- Exam with 10 cameras (from Test 1)
- Same Excel with same 10 cameras

**Expected Result:**
- ✅ 0 cameras inserted
- ✅ 10 cameras skipped
- ✅ Message: "Successfully uploaded 0 cameras. Skipped 10 duplicates (device_id + camera_no + tc_code already exists)."

**SQL Verification:**
```sql
SELECT COUNT(*) FROM camera_links_rtsp WHERE exam_id = 1;
-- Expected: 10 (no change)
```

---

### Test 3: Upload Mix of New and Existing (Partial Duplicates)
**Setup:**
- Exam with 10 cameras
- Excel with 15 cameras (10 existing + 5 new)

**Expected Result:**
- ✅ 5 cameras inserted
- ✅ 10 cameras skipped
- ✅ Message: "Successfully uploaded 5 cameras. Skipped 10 duplicates (device_id + camera_no + tc_code already exists)."

**SQL Verification:**
```sql
SELECT COUNT(*) FROM camera_links_rtsp WHERE exam_id = 1;
-- Expected: 15 (10 + 5)
```

---

### Test 4: RTSP - Different Camera Numbers (Should Insert)
**Setup:**
- Existing: Device `1000728`, Camera `4`, TC `TC001`
- Upload: Device `1000728`, Camera `5`, TC `TC001`

**Expected Result:**
- ✅ New camera inserted (different camera_no)
- ✅ Unique keys: `1000728_4_TC001` vs `1000728_5_TC001`

---

### Test 5: RTSP - Different TC Codes (Should Insert)
**Setup:**
- Existing: Device `1000728`, Camera `4`, TC `TC001`
- Upload: Device `1000728`, Camera `4`, TC `TC002`

**Expected Result:**
- ✅ New camera inserted (different tc_code)
- ✅ Unique keys: `1000728_4_TC001` vs `1000728_4_TC002`

---

### Test 6: RTSP - Null Camera Number Handling
**Setup:**
- Existing: Device `1000728`, Camera `null`, TC `TC001`
- Upload: Device `1000728`, Camera `null`, TC `TC001`

**Expected Result:**
- ✅ Duplicate detected (both have null camera_no)
- ✅ Unique keys: `1000728__TC001` vs `1000728__TC001`
- ✅ Second upload skipped

---

### Test 7: HLS - Same TC and Channel (Should Skip)
**Setup:**
- Existing: TC `TC001`, Channel `54703`
- Upload: TC `TC001`, Channel `54703`

**Expected Result:**
- ✅ Duplicate detected
- ✅ Unique keys: `TC001_54703` vs `TC001_54703`
- ✅ Upload skipped

---

### Test 8: HLS - Different Channel (Should Insert)
**Setup:**
- Existing: TC `TC001`, Channel `54703`
- Upload: TC `TC001`, Channel `54704`

**Expected Result:**
- ✅ New camera inserted (different ch_code)
- ✅ Unique keys: `TC001_54703` vs `TC001_54704`

---

### Test 9: HLS - Missing TC Code (Fail-Safe Insert)
**Setup:**
- Existing: TC `TC001`, Channel `54703`
- Upload: TC `null`, Channel `54703`

**Expected Result:**
- ✅ New camera inserted (incomplete unique key, fail-safe)
- ✅ No duplicate check performed

---

### Test 10: HLS - Missing Channel Code (Fail-Safe Insert)
**Setup:**
- Existing: TC `TC001`, Channel `54703`
- Upload: TC `TC001`, Channel `null`

**Expected Result:**
- ✅ New camera inserted (incomplete unique key, fail-safe)
- ✅ No duplicate check performed

---

### Test 11: Large Upload Performance
**Setup:**
- Exam with 1000 existing cameras
- Excel with 2000 cameras (1000 duplicates + 1000 new)

**Expected Result:**
- ✅ 1000 cameras inserted
- ✅ 1000 cameras skipped
- ✅ Response time < 5 seconds
- ✅ Message: "Successfully uploaded 1000 cameras. Skipped 1000 duplicates..."

**Performance Verification:**
```javascript
// Should use Set for O(1) lookup
// Total complexity: O(n + m) where n=existing, m=new
```

---

### Test 12: Multiple Exams Isolation
**Setup:**
- Exam 1: Device `1000728`, Camera `4`, TC `TC001`
- Exam 2: Upload Device `1000728`, Camera `4`, TC `TC001`

**Expected Result:**
- ✅ Camera inserted in Exam 2 (different exam_id)
- ✅ Duplicate check is per-exam, not global

**SQL Verification:**
```sql
SELECT exam_id, device_id, camera_no, tc_code 
FROM camera_links_rtsp 
WHERE device_id = '1000728' AND camera_no = '4';
-- Expected: 2 rows (one per exam)
```

---

## Manual Testing Checklist

### RTSP Upload Tests
- [ ] Upload 10 new RTSP cameras → All inserted
- [ ] Re-upload same 10 cameras → All skipped
- [ ] Upload 5 existing + 5 new → 5 inserted, 5 skipped
- [ ] Upload camera with null camera_no → Handled correctly
- [ ] Upload camera with null tc_code → Handled correctly
- [ ] Verify unique key shown in success message
- [ ] Verify warning alert for duplicates

### HLS Upload Tests
- [ ] Upload 10 new HLS cameras → All inserted
- [ ] Re-upload same 10 cameras → All skipped
- [ ] Upload 5 existing + 5 new → 5 inserted, 5 skipped
- [ ] Upload camera with null ch_code → Handled correctly
- [ ] Upload camera with null tc_code → Handled correctly
- [ ] Verify unique key shown in success message
- [ ] Verify warning alert for duplicates

### UI/UX Tests
- [ ] Upload dialog shows duplicate detection info
- [ ] Success message shows correct counts
- [ ] Warning alert appears when duplicates found
- [ ] Alert shows which unique key was used
- [ ] Camera list refreshes after upload
- [ ] No console errors during upload

### Edge Cases
- [ ] Upload empty Excel → Error message
- [ ] Upload Excel with no valid rows → Error message
- [ ] Upload Excel with all invalid data → Error message
- [ ] Upload very large file (10,000 rows) → Performance acceptable
- [ ] Upload with special characters in fields → Handled correctly

---

## SQL Queries for Verification

### Check for Duplicates (RTSP)
```sql
-- Should return 0 rows if duplicate prevention works
SELECT 
    device_id, 
    camera_no, 
    tc_code, 
    COUNT(*) as duplicate_count
FROM camera_links_rtsp
WHERE exam_id = ?
GROUP BY device_id, camera_no, tc_code
HAVING COUNT(*) > 1;
```

### Check for Duplicates (HLS)
```sql
-- Should return 0 rows if duplicate prevention works
SELECT 
    tc_code, 
    ch_code, 
    COUNT(*) as duplicate_count
FROM camera_links_hls
WHERE exam_id = ?
GROUP BY tc_code, ch_code
HAVING COUNT(*) > 1;
```

### Count Cameras by Exam
```sql
-- Verify correct count after upload
SELECT 
    exam_id,
    COUNT(*) as camera_count
FROM camera_links_rtsp
GROUP BY exam_id;
```

### Find Cameras with Null Keys
```sql
-- RTSP: Find cameras with null in unique key fields
SELECT * FROM camera_links_rtsp
WHERE device_id IS NULL 
   OR camera_no IS NULL 
   OR tc_code IS NULL;

-- HLS: Find cameras with null in unique key fields
SELECT * FROM camera_links_hls
WHERE tc_code IS NULL 
   OR ch_code IS NULL;
```

---

## Expected Error Messages

### No File Selected
```json
{
  "success": false,
  "message": "No file uploaded"
}
```

### Empty Excel
```json
{
  "success": false,
  "message": "Excel file is empty"
}
```

### No Valid Records (RTSP)
```json
{
  "success": false,
  "message": "No valid records found. Required RTSP columns: Device ID, Server Address (or CCTV URL). Optional: Camera No, TC Code, Camera Name, Centre/Center Name, City, State, Classification..."
}
```

### No Valid Records (HLS)
```json
{
  "success": false,
  "message": "No valid records found. Make sure the Excel contains a 'CCTV URL' column."
}
```

---

## Success Messages

### All New Cameras
```json
{
  "success": true,
  "message": "Successfully uploaded 10 cameras."
}
```

### With Duplicates (RTSP)
```json
{
  "success": true,
  "message": "Successfully uploaded 5 cameras. Skipped 5 duplicates (device_id + camera_no + tc_code already exists)."
}
```

### With Duplicates (HLS)
```json
{
  "success": true,
  "message": "Successfully uploaded 5 cameras. Skipped 5 duplicates (tc_code + ch_code already exists)."
}
```

### All Duplicates
```json
{
  "success": true,
  "message": "Successfully uploaded 0 cameras. Skipped 10 duplicates (device_id + camera_no + tc_code already exists)."
}
```

---

## Automated Test Script (Optional)

```javascript
// test-camera-upload-duplicates.js
const axios = require('axios');
const FormData = require('form-data');
const fs = require('fs');

async function testCameraUpload() {
  const examId = 1;
  const token = 'your-auth-token';
  
  // Test 1: Upload new cameras
  console.log('Test 1: Uploading 10 new cameras...');
  const formData1 = new FormData();
  formData1.append('file', fs.createReadStream('./test-cameras-10.xlsx'));
  
  const res1 = await axios.post(
    `http://localhost:3000/api/exams/${examId}/cameras/upload`,
    formData1,
    { headers: { ...formData1.getHeaders(), Authorization: `Bearer ${token}` } }
  );
  
  console.log('Result:', res1.data.message);
  // Expected: "Successfully uploaded 10 cameras."
  
  // Test 2: Re-upload same file
  console.log('\nTest 2: Re-uploading same 10 cameras...');
  const formData2 = new FormData();
  formData2.append('file', fs.createReadStream('./test-cameras-10.xlsx'));
  
  const res2 = await axios.post(
    `http://localhost:3000/api/exams/${examId}/cameras/upload`,
    formData2,
    { headers: { ...formData2.getHeaders(), Authorization: `Bearer ${token}` } }
  );
  
  console.log('Result:', res2.data.message);
  // Expected: "Successfully uploaded 0 cameras. Skipped 10 duplicates..."
  
  // Test 3: Upload mix
  console.log('\nTest 3: Uploading 15 cameras (10 existing + 5 new)...');
  const formData3 = new FormData();
  formData3.append('file', fs.createReadStream('./test-cameras-15.xlsx'));
  
  const res3 = await axios.post(
    `http://localhost:3000/api/exams/${examId}/cameras/upload`,
    formData3,
    { headers: { ...formData3.getHeaders(), Authorization: `Bearer ${token}` } }
  );
  
  console.log('Result:', res3.data.message);
  // Expected: "Successfully uploaded 5 cameras. Skipped 10 duplicates..."
}

testCameraUpload().catch(console.error);
```

---

## Test Data Samples

### RTSP Test Excel (test-cameras-rtsp.xlsx)
```
Device ID | Camera No | TC Code | Camera Name | Centre Name | CCTV URL
----------|-----------|---------|-------------|-------------|----------
1000728   | 4         | TC001   | Cam 1       | Center A    | rtsp://...
1000728   | 5         | TC001   | Cam 2       | Center A    | rtsp://...
1000648   | 6         | TC002   | Cam 3       | Center B    | rtsp://...
```

### HLS Test Excel (test-cameras-hls.xlsx)
```
TC Code | Ch Code | Camera Name | Centre Name | CCTV URL
--------|---------|-------------|-------------|----------
TC001   | 54703   | Lab 1       | Center A    | https://...
TC001   | 54704   | Lab 2       | Center A    | https://...
TC002   | 54705   | Lab 3       | Center B    | https://...
```
