# Socket Throttling and Live Counts Fix

## Problem
1. **Too many live detections flooding the UI** - Need to throttle to 1 detection per second per detection type
2. **Counts not updating in real-time** - AppBar and sidebar counts were not increasing
3. **Need full counts on page refresh** - Users should see all actual counts when they refresh

## Solution Overview

### Backend: Throttle Individual Detection Emissions (1 per second per type)
- Only emit 1 `detection:new` event per second per detection type
- Always emit `detection:counts` with complete snapshot every 2 seconds
- This reduces socket traffic while keeping counts accurate

### Frontend: Use `detection:counts` as Source of Truth
- Individual `detection:new` events are for live feed display only
- `detection:counts` event provides absolute counts (not incremental)
- Counts update every 2 seconds with complete snapshot from backend

---

## Changes Made

### 1. Backend: Throttle Socket Emissions

**File:** `script-server/app/main.py`

Added throttling logic to `detection_watch_loop`:

```python
async def detection_watch_loop():
    global known_detection_files
    
    # Throttle: Track last emission time per detection type (1 per second per type)
    last_emission_time = {}

    while True:
        await asyncio.sleep(DETECTION_SCAN_INTERVAL_SECONDS)
        current_snapshot = list_detection_files()

        for type_key, current_files in current_snapshot.items():
            previous_files = known_detection_files.get(type_key, set())
            new_files = sorted(current_files - previous_files)

            # Throttle: Only emit 1 detection per second per type
            now = time.time()
            last_emit = last_emission_time.get(type_key, 0)
            
            if new_files and (now - last_emit) >= 1.0:
                # Emit only the FIRST new detection for this type
                tagged_path = new_files[0]
                
                # ... parse and emit detection ...
                await sio.emit("detection:new", payload)
                
                # Update last emission time for this type
                last_emission_time[type_key] = now
                
                logger.info(f"Live detection emitted: {type_key} (throttled: {len(new_files)-1} skipped)")
            elif new_files:
                logger.debug(f"Throttled {len(new_files)} detections for {type_key}")
                
        # Always emit updated counts (regardless of throttling)
        counts = {k: len(v) for k, v in current_snapshot.items()}
        await sio.emit("detection:counts", counts)

        known_detection_files = current_snapshot
```

**Key Points:**
- ✅ Only 1 `detection:new` per second per type (reduces socket traffic)
- ✅ Always emits `detection:counts` every 2 seconds (complete snapshot)
- ✅ Logs how many detections were throttled/skipped

### 2. Frontend: Use Counts Event as Source of Truth

**File:** `cctv_frontend/src/contexts/SocketContext.jsx`

Simplified to use `detection:counts` as the absolute source of truth:

```javascript
// Listen to detection counts updates - THIS IS THE SOURCE OF TRUTH FOR COUNTS
newSocket.on('detection:counts', (counts) => {
  console.log('📊 Received detection:counts from backend:', counts);
  
  // Set absolute counts (not incremental)
  setUnfilteredCounts(counts);
  setAllCounts(counts);
  
  console.log('✅ Counts updated in context');
});

// Individual detections are for live feed display only (not for counting)
newSocket.on('detection:new', (item) => {
  adminQueue.push(item);  // Just for live feed display
});
```

**Key Points:**
- ✅ `detection:counts` sets absolute counts (not incremental)
- ✅ Individual `detection:new` events are for live feed display only
- ✅ Removed complex batching/incrementing logic (simpler and more reliable)
- ✅ Counts update every 2 seconds from backend

### 3. Increased Cooldowns for High-Volume Scripts

**Files:**
- `script-server/scripts/detection/workers/invigilator_worker.py`
- `script-server/scripts/detection/workers/qp_box_det_worker.py`

```python
# Invigilator: 600s → 900s (15 minutes)
def _gpu_batch_worker(stop_event, save_folder, cooldown_time=900):

# QP Box: 300s → 600s (10 minutes)
def _gpu_batch_worker(stop_event, save_folder, cooldown_time=600):
```

---

## How It Works Now

### Live Detection Flow:
1. **Backend detects new image** → Adds to queue
2. **Every 2 seconds**, backend scans for new detections
3. **For each detection type**:
   - If new detections exist AND 1+ second since last emit → Emit FIRST detection only
   - If < 1 second since last emit → Skip (throttle)
4. **Always emit `detection:counts`** with complete snapshot

### Frontend Count Updates:
1. **Every 2 seconds**, frontend receives `detection:counts` event
2. **Sets absolute counts** (not incremental)
3. **Components re-render** with updated counts
4. **AppBar chip and sidebar** show updated totals

### On Page Refresh:
1. Frontend reconnects to socket
2. Backend sends current `detection:counts` snapshot
3. User sees all actual counts immediately

---

## Updated Cooldown Reference

| Script | Cooldown | Frequency | Change |
|--------|----------|-----------|--------|
| intruder | 60s | Every 1 minute | - |
| server | 60s | Every 1 minute | - |
| proximity | 60s | Every 1 minute | - |
| entry_exit | 60s | Every 1 minute | - |
| tampering | 60s | Every 1 minute | - |
| blind_spot | 180s | Every 3 minutes | - |
| crowd | 180s | Every 3 minutes | - |
| inversion | 240s | Every 4 minutes | - |
| **qp_box** | **600s** | **Every 10 minutes** | ↑ Increased from 5 min |
| crowd_zero | 360s | Every 6 minutes | - |
| crowd_five | 360s | Every 6 minutes | - |
| phone | 600s | Every 10 minutes | - |
| crowd_biometric | 600s | Every 10 minutes | - |
| **invigilator** | **900s** | **Every 15 minutes** | ↑ Increased from 10 min |
| strongroom | 0s | Immediate | - |

---

## Benefits

### ✅ Reduced Socket Traffic
- Only 1 detection per second per type (instead of all detections)
- Prevents UI from being flooded with too many live updates
- Better performance for both backend and frontend

### ✅ Accurate Counts
- `detection:counts` provides absolute truth every 2 seconds
- No risk of missed increments or race conditions
- Counts always match actual file count on disk

### ✅ Simpler Code
- Removed complex batching/incrementing logic from frontend
- Single source of truth for counts
- Easier to debug and maintain

### ✅ Better User Experience
- Live feed shows detections at reasonable rate (1/sec per type)
- Counts update smoothly every 2 seconds
- Page refresh shows all actual counts immediately

---

## Testing

### Test Throttling:
1. Start a high-volume detection script (e.g., invigilator)
2. Watch backend logs for throttling messages:
   ```
   Live detection emitted: invigilator (throttled: 5 skipped)
   ```
3. Verify only 1 detection per second appears in live feed

### Test Count Updates:
1. Open browser console
2. Watch for logs every 2 seconds:
   ```
   📊 Received detection:counts from backend: {proximity: 10, crowd: 5, ...}
   ✅ Counts updated in context
   [Layout] Total detections updated: 15
   ```
3. Verify AppBar chip and sidebar counts increase

### Test Page Refresh:
1. Let detections accumulate
2. Refresh the page
3. Verify counts show immediately (not starting from 0)

---

## Files Modified

1. ✅ `script-server/app/main.py` - Added throttling logic
2. ✅ `cctv_frontend/src/contexts/SocketContext.jsx` - Simplified to use counts event
3. ✅ `script-server/scripts/detection/workers/invigilator_worker.py` - Increased cooldown
4. ✅ `script-server/scripts/detection/workers/qp_box_det_worker.py` - Increased cooldown

---

## Summary

✅ **Socket throttling** - Only 1 detection per second per type emitted
✅ **Counts update** - Every 2 seconds with complete snapshot
✅ **Live feed** - Shows detections at reasonable rate
✅ **Page refresh** - Shows all actual counts immediately
✅ **Cooldowns optimized** - Reduced high-volume scripts (invigilator, qp_box)

The system now provides a smooth, performant live detection experience! 🎉
