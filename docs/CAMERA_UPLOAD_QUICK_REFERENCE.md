# Camera Upload Duplicate Prevention - Quick Reference

## 🔑 Unique Keys at a Glance

| Link Type | Unique Key Formula | Example |
|-----------|-------------------|---------|
| **RTSP** | `device_id + camera_no + tc_code` | `1000728_4_TC001` |
| **HLS** | `tc_code + ch_code` | `TC001_54703` |

---

## 📋 Quick Decision Tree

```
Is this camera already in the exam?
│
├─ RTSP: Check device_id + camera_no + tc_code
│  ├─ Match found? → SKIP (duplicate)
│  └─ No match? → INSERT (new camera)
│
└─ HLS: Check tc_code + ch_code
   ├─ Match found? → SKIP (duplicate)
   └─ No match? → INSERT (new camera)
```

---

## 💡 Common Scenarios

### ✅ Will Be Inserted (New Camera)

| Scenario | RTSP Example | HLS Example |
|----------|--------------|-------------|
| **Different device** | Device `1000999` vs `1000728` | - |
| **Different camera number** | Camera `5` vs `4` | - |
| **Different TC code** | TC `TC002` vs `TC001` | TC `TC002` vs `TC001` |
| **Different channel** | - | Channel `54704` vs `54703` |

### ⏭️ Will Be Skipped (Duplicate)

| Scenario | RTSP Example | HLS Example |
|----------|--------------|-------------|
| **Exact match** | All 3 fields match | Both fields match |
| **Re-upload same file** | All cameras match | All cameras match |

---

## 📊 Response Messages

### Success (No Duplicates)
```
✅ Successfully uploaded 10 cameras.
```

### Warning (With Duplicates) - RTSP
```
⚠️ Successfully uploaded 5 cameras. 
   Skipped 5 duplicates (device_id + camera_no + tc_code already exists).
```

### Warning (With Duplicates) - HLS
```
⚠️ Successfully uploaded 5 cameras. 
   Skipped 5 duplicates (tc_code + ch_code already exists).
```

### All Duplicates
```
⚠️ Successfully uploaded 0 cameras. 
   Skipped 10 duplicates (unique_key_fields already exists).
```

---

## 🎯 User Workflow

### Step 1: Prepare Excel
- Use correct column headers
- Fill required fields
- Download template if unsure

### Step 2: Upload
- Select exam
- Click "Upload Excel"
- Choose file
- (HLS only) Select shift/date if needed

### Step 3: Review Result
- Check success/warning message
- Note count of uploaded cameras
- Note count of skipped duplicates
- Verify camera list updated

---

## 🔍 Troubleshooting

### Problem: All cameras skipped
**Cause:** All cameras already exist  
**Solution:** Check if you're uploading to the correct exam

### Problem: Expected new cameras but skipped
**Cause:** Unique key matches existing camera  
**Solution:** Verify device_id/camera_no/tc_code (RTSP) or tc_code/ch_code (HLS)

### Problem: Duplicate cameras in database
**Cause:** Uploaded before this feature was implemented  
**Solution:** Manually remove duplicates, then use this feature going forward

---

## 📝 Excel Column Requirements

### RTSP Required Columns
- ✅ Device ID
- ✅ CCTV URL (or Server Address)

### RTSP Optional Columns
- Camera No
- TC Code
- Camera Name
- Centre Name
- City
- State
- Classification

### HLS Required Columns
- ✅ CCTV URL

### HLS Optional Columns
- TC Code
- Ch Code (ch_no)
- Centre Name
- Camera Name
- City
- State
- Camera Category
- Classification

---

## ⚡ Performance Tips

### For Large Uploads (1000+ cameras)
1. Split into smaller files (500 cameras each)
2. Upload during off-peak hours
3. Ensure stable internet connection
4. Wait for each upload to complete before next

### For Best Results
1. Use provided Excel templates
2. Don't modify column headers
3. Fill required fields completely
4. Remove empty rows

---

## 🛠️ Technical Details

### Database Queries
```sql
-- Check for RTSP duplicates
SELECT device_id, camera_no, tc_code, COUNT(*) 
FROM camera_links_rtsp 
WHERE exam_id = ? 
GROUP BY device_id, camera_no, tc_code 
HAVING COUNT(*) > 1;

-- Check for HLS duplicates
SELECT tc_code, ch_code, COUNT(*) 
FROM camera_links_hls 
WHERE exam_id = ? 
GROUP BY tc_code, ch_code 
HAVING COUNT(*) > 1;
```

### API Endpoint
```
POST /api/exams/:examId/cameras/upload
Content-Type: multipart/form-data

Body:
- file: Excel file (.xlsx, .xls)
- shiftId: (optional, HLS only)
- examDate: (optional, HLS only)
```

---

## 📞 Quick Help

### Need Template?
Click "Download HLS Template" or "Download RTSP Template" in upload dialog

### Need to Update Existing Camera?
Edit camera individually from camera list (update feature coming soon)

### Need to Remove Duplicates?
Contact admin or manually delete from camera list

### Need More Help?
Check full documentation:
- `CAMERA_UPLOAD_DUPLICATE_PREVENTION.md`
- `CAMERA_UPLOAD_FLOW_DIAGRAM.md`
- `TEST_CAMERA_UPLOAD_DUPLICATES.md`

---

## ✅ Checklist Before Upload

- [ ] Correct exam selected
- [ ] Excel file prepared with correct columns
- [ ] Required fields filled
- [ ] Template downloaded if unsure
- [ ] Understand which cameras will be skipped
- [ ] Ready to review result message

---

## 🎓 Remember

1. **Duplicates are good** - They prevent data integrity issues
2. **Skipped ≠ Error** - It means the camera already exists
3. **Per-exam check** - Same camera can exist in different exams
4. **Null values OK** - System handles them gracefully
5. **Re-upload safe** - You can safely re-upload the same file

---

**Quick Tip:** If you're unsure whether a camera exists, just upload it. The system will skip it if it's a duplicate. No harm done! ✨
