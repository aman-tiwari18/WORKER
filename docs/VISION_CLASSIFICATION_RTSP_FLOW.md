# Vision Classification RTSP Flow - Complete Analysis

## 📋 Overview

When you schedule a **"vision classification"** script for an **RTSP exam**, here's exactly what happens:

---

## 🔄 Execution Flow

### 1. **Script Selection**
**File:** `script-server/app/script_runner.py`

When `script_code = "vision"` and `link_type = "RTSP"`:
```python
if script_code == "vision":
    if link_type_for_path == "RTSP":
        full_script_path = self.scripts_dir / "detection/vision_classification_rtsp.py"
    else:
        full_script_path = self.scripts_dir / "detection/vision_classification.py"
```

**Result:** Runs `vision_classification_rtsp.py` (NOT master_pipeline.py)

---

### 2. **Camera Loading**
**Function:** `get_rtsp_cameras(exam_id)`

Loads ALL RTSP cameras for the exam from the database:
```sql
SELECT * FROM camera_links_rtsp 
WHERE exam_id = ? 
AND status = 'active'
```

**Example:** If exam has 1809 cameras, ALL 1809 are loaded.

---

### 3. **Two-Pass Processing**

The script uses a **TWO-PASS MODE** for efficiency:

#### **PASS 1: Quick Accessibility Check**
- **Purpose:** Determine which cameras are accessible
- **Workers:** `MAX_WORKERS_NETWORK = 64` (parallel threads)
- **What it does:** For EACH camera, tries to get RTSP stream URL
- **No frame capture** - just checks if API responds

#### **PASS 2: Frame Capture & Classification**
- **Purpose:** Classify only accessible cameras
- **Workers:** `MAX_WORKERS_VLM = 4` (VLM is slower)
- **What it does:** Captures frame and classifies with AI

---

## 🌐 URL Checking Process (Per Camera)

### For Each Camera, the Script Checks:

#### Step 1: **Authentication** (`authenticate_server`)
**URL Checked:**
```
https://{server_address}/admin/API/accounts/authorize
```

**Attempts:** Up to 3 attempts per server
**Timeout:** `TIMEOUT_API = 10` seconds per attempt

**What happens:**
1. First POST: Get realm, randomKey, publicKey
2. Second POST: Exchange for authentication token

**Possible Errors:**
- ❌ Connection timeout (10s)
- ❌ No route to host
- ❌ SSL/TLS errors
- ❌ Invalid credentials

---

#### Step 2: **Start Video Stream** (`start_video_stream`)
**URL Checked:**
```
https://{server_address}/admin/API/MTS/Video/StartVideo?token={token}
```

**Payload:**
```json
{
  "channelId": "{device_id}$1$0${camera_no}",
  "streamType": "1",
  "dataType": "2"
}
```

**Response Contains:**
- `url`: Primary RTSP URL
- `url2`: Secondary RTSP URL (backup)
- `token`: Stream token

**Possible Errors:**
- ❌ 401 Unauthorized (token expired)
- ❌ 404 Not Found (camera doesn't exist)
- ❌ Connection timeout
- ❌ No route to host

---

#### Step 3: **Build RTSP URL** (`build_rtsp_url`)
Takes the URL from Step 2 and adds the stream token:
```
rtsp://172.x.x.x:554/...?token={stream_token}
```

**URL Formats Returned:**
- Public IP: `rtsp://public.ip:554/...`
- Private IP: `rtsp://172.x.x.x:554/...`

**Preference:** Public IPs are preferred over private 172.x subnets

---

#### Step 4: **Frame Capture** (Pass 2 Only)
Tries TWO methods to capture a frame:

##### Method 1: OpenCV (`capture_frame_with_opencv_stream`)
```python
cap = cv2.VideoCapture(stream_url)
cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000)
cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000)
```

**Timeout:** 5000ms (5 seconds)

##### Method 2: FFmpeg (fallback)
```bash
ffmpeg -rtsp_transport tcp -timeout 5000000 -i {stream_url} -frames:v 1 output.jpg
```

**Timeout:** 5 seconds

**Possible Errors:**
- ❌ Connection timeout
- ❌ No route to host
- ❌ Stream not available
- ❌ Network unreachable

---

## 📊 Total URLs Checked Per Camera

### Minimum (Successful Case):
1. **Auth URL** (Step 1): 2 requests (initial + token exchange)
2. **Start Video URL** (Step 2): 1 request
3. **RTSP Stream URL** (Step 4): 1 connection attempt

**Total: 4 URL checks per camera**

### Maximum (Failed Case with Retries):
1. **Auth URL**: 6 requests (3 attempts × 2 requests each)
2. **Start Video URL**: 3 requests (if token keeps expiring)
3. **RTSP Stream URL**: 2 attempts (OpenCV + FFmpeg)

**Total: Up to 11 URL checks per camera**

---

## 🔢 Example: 1809 Cameras

### Pass 1: Accessibility Check
- **Cameras:** 1809
- **Workers:** 64 parallel threads
- **URLs per camera:** 3-9 (auth + start video)
- **Total URLs checked:** ~5,427 to 16,281 URLs
- **Time:** Depends on network latency and failures

### Pass 2: Classification (Only Accessible)
- **Cameras:** Let's say 1200 are accessible
- **Workers:** 4 parallel threads (VLM is slow)
- **URLs per camera:** 1-2 (RTSP stream)
- **Total URLs checked:** ~1,200 to 2,400 URLs
- **Time:** Depends on VLM processing time

### Grand Total URLs Checked:
**~6,627 to 18,681 URLs** for 1809 cameras

---

## ⚠️ "No Route to Host" Errors

### When Does This Happen?

#### 1. **During Authentication (Step 1)**
```
https://{server_address}/admin/API/accounts/authorize
```

**Causes:**
- Server is down
- Network is unreachable
- Firewall blocking
- DNS resolution failed
- Server IP changed

**Impact:** Camera marked as "Not Accessible" immediately

---

#### 2. **During Start Video (Step 2)**
```
https://{server_address}/admin/API/MTS/Video/StartVideo
```

**Causes:**
- Same as above
- API endpoint changed
- Server overloaded

**Impact:** Camera marked as "Not Accessible"

---

#### 3. **During RTSP Stream (Step 4)**
```
rtsp://172.x.x.x:554/...
```

**Causes:**
- RTSP port (554) blocked
- Private IP not routable from script server
- Stream server different from API server
- Network segmentation

**Impact:** Camera marked as "Accessible" but "Failed to Classify"

---

## 📈 Progress Logging

The script logs progress every 10 cameras:

### Pass 1 Example:
```
⏳ Pass 1 Progress: 10/1809 (0.6%) processed
⏳ Pass 1 Progress: 20/1809 (1.1%) processed
...
⏳ Pass 1 Progress: 1809/1809 (100.0%) processed
```

### Pass 2 Example:
```
⏳ Pass 2 Progress: 10/1200 (0.8%) classified
⏳ Pass 2 Progress: 20/1200 (1.7%) classified
...
⏳ Pass 2 Progress: 1200/1200 (100.0%) classified
```

---

## 🎯 Final Results

### Database Updates
For each camera, updates `camera_links_rtsp` table:
```sql
UPDATE camera_links_rtsp 
SET classification = ?, 
    status = ? 
WHERE exam_id = ? 
AND device_id = ?
```

**Possible Classifications:**
- ✅ "Server Room"
- ✅ "Outdoor Scene"
- ✅ "Outside Area"
- ✅ "Parking Lot"
- ✅ "Entry"
- ✅ "Computer Lab"
- ⚠️ "Uncertain/Obstructed"
- ❌ "Failed to Classify"

**Possible Statuses:**
- ✅ "Accessible"
- ❌ "Not Accessible"

---

## 📊 Summary Statistics

### Example Output:
```
================================================================================
🎉 FINAL SUMMARY
================================================================================
Total Cameras: 1809
✅ Accessible (Classified): 1050
⚠️  Accessible (Uncertain/Obstructed): 150
❌ Not Accessible: 609
⏱️  Total Time: 45m 30s
📊 Success Rate: 66.3%

Server-wise Statistics:
Server: D16.camview.io | Total: 500 | Accessible: 350 | Rate: 70.0%
Server: D17.camview.io | Total: 450 | Accessible: 300 | Rate: 66.7%
Server: D18.camview.io | Total: 859 | Accessible: 550 | Rate: 64.0%
================================================================================
```

---

## 🔧 Configuration

### Timeouts (Configurable)
```python
TIMEOUT_API = 10           # API requests (auth, start video)
TIMEOUT_OPENCV = 5000      # OpenCV stream capture (ms)
TIMEOUT_FFMPEG = 5         # FFmpeg capture (seconds)
TIMEOUT_VLM = 60           # AI classification (seconds)
```

### Workers (Configurable)
```python
MAX_WORKERS_NETWORK = 64   # Pass 1: Parallel accessibility checks
MAX_WORKERS_VLM = 4        # Pass 2: Parallel AI classifications
```

### Token Caching
```python
TOKEN_EXPIRY_SECONDS = 90  # Reuse auth tokens for 90 seconds
```

---

## 🚀 Performance Optimization

### Why Two-Pass Mode?
1. **Pass 1 is fast** (no frame capture, just API calls)
2. **Pass 2 is slow** (frame capture + AI classification)
3. **Avoids wasting time** on inaccessible cameras

### Token Caching
- Authenticates once per server
- Reuses token for 90 seconds
- Reduces auth requests by ~99%

### Parallel Processing
- **64 threads** for network I/O (Pass 1)
- **4 threads** for CPU/GPU-intensive AI (Pass 2)

---

## 🐛 Common Issues

### 1. "No Route to Host" for All Cameras
**Cause:** Script server cannot reach camera servers
**Solution:** Check network connectivity, firewall rules, VPN

### 2. "No Route to Host" for Private IPs (172.x.x.x)
**Cause:** Script server not on same network as cameras
**Solution:** Use public IPs or VPN tunnel

### 3. High "Not Accessible" Rate
**Causes:**
- Servers down
- Network issues
- Firewall blocking
- Wrong credentials

### 4. High "Uncertain/Obstructed" Rate
**Causes:**
- Cameras showing grey screens
- Cameras blocked/covered
- Poor lighting
- Camera malfunction

---

## 📝 Key Takeaways

1. **Script Used:** `vision_classification_rtsp.py` (NOT master_pipeline)
2. **Cameras Processed:** ALL cameras for the exam (e.g., 1809)
3. **URLs Checked:** 4-11 per camera (auth + API + stream)
4. **Total URLs:** ~6,000 to 18,000 for 1809 cameras
5. **Processing Mode:** Two-pass (accessibility → classification)
6. **Parallel Workers:** 64 for network, 4 for AI
7. **"No Route to Host":** Can occur at auth, API, or stream level
8. **Database Updates:** Classification and status for each camera

---

**Last Updated:** April 27, 2026
**Script Version:** Optimized Two-Pass Mode
**Status:** Production ✅
