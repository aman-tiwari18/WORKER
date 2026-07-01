# Vision Classification RTSP - Quick Summary

## 🎯 What Runs?

**Script:** `vision_classification_rtsp.py` (NOT master_pipeline.py)

---

## 📊 For 1809 Cameras Example

### URLs Checked Per Camera:

#### ✅ Successful Camera (Minimum):
1. Auth API: `https://{server}/admin/API/accounts/authorize` (2 requests)
2. Start Video API: `https://{server}/admin/API/MTS/Video/StartVideo` (1 request)
3. RTSP Stream: `rtsp://{ip}:554/...` (1 connection)

**Total: 4 URLs per camera**

#### ❌ Failed Camera (Maximum with retries):
1. Auth API: 6 requests (3 attempts × 2 each)
2. Start Video API: 3 requests
3. RTSP Stream: 2 attempts (OpenCV + FFmpeg)

**Total: 11 URLs per camera**

---

## 🔢 Total for 1809 Cameras

### Pass 1: Accessibility Check
- **URLs:** 3-9 per camera
- **Total:** ~5,427 to 16,281 URLs
- **Workers:** 64 parallel threads
- **Purpose:** Check which cameras are reachable

### Pass 2: Classification (Only Accessible)
- **URLs:** 1-2 per camera (only accessible ones)
- **Total:** ~1,200 to 2,400 URLs (if 1200 accessible)
- **Workers:** 4 parallel threads
- **Purpose:** Capture frame and classify with AI

### Grand Total:
**~6,627 to 18,681 URLs checked** for 1809 cameras

---

## ⚠️ "No Route to Host" Occurs At:

### 1. Authentication Stage
```
https://{server_address}/admin/API/accounts/authorize
```
**Result:** Camera marked "Not Accessible" immediately

### 2. Start Video Stage
```
https://{server_address}/admin/API/MTS/Video/StartVideo
```
**Result:** Camera marked "Not Accessible"

### 3. RTSP Stream Stage
```
rtsp://172.x.x.x:554/...
```
**Result:** Camera marked "Accessible" but "Failed to Classify"

---

## 📈 Processing Flow

```
1809 Cameras Loaded from DB
         ↓
    PASS 1 (64 workers)
    Quick Check: Can we reach the camera API?
         ↓
    ├─→ Accessible: 1200 cameras
    └─→ Not Accessible: 609 cameras (marked in DB)
         ↓
    PASS 2 (4 workers)
    Capture & Classify: Only the 1200 accessible
         ↓
    ├─→ Classified: 1050 cameras (Server Room, Outdoor, etc.)
    └─→ Uncertain: 150 cameras (grey screen, obstructed)
         ↓
    All results saved to DB
```

---

## 🎯 Key Points

1. **ALL cameras** for the exam are processed (not filtered by script assignment)
2. **Two-pass mode** for efficiency (quick check → detailed classification)
3. **4-11 URLs** checked per camera depending on success/failure
4. **Token caching** reduces auth requests (90-second reuse)
5. **Parallel processing** with 64 network workers, 4 AI workers
6. **"No route to host"** can happen at 3 different stages
7. **Results saved** to `camera_links_rtsp` table (classification + status)

---

## 🔧 Timeouts

- **API calls:** 10 seconds each
- **OpenCV capture:** 5 seconds
- **FFmpeg capture:** 5 seconds
- **AI classification:** 60 seconds

---

## 📊 Example Output

```
Total Cameras: 1809
✅ Accessible (Classified): 1050
⚠️  Accessible (Uncertain/Obstructed): 150
❌ Not Accessible: 609
⏱️  Total Time: 45m 30s
📊 Success Rate: 66.3%
```

---

**TL;DR:** Vision classification checks **~7,000 to 19,000 URLs** for 1809 cameras across 2 passes, with "No route to host" errors occurring at auth, API, or stream connection stages.
