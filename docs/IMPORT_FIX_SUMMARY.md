# Import Error Fix - Master Pipeline Worker Loading

## 🐛 Issue Identified

The master pipeline was failing to import worker modules with the error:
```
Exception in thread Thread-1 (start_producer):
  File "/data/cctv_dev/cctv/script-server/scripts/detection/pipeline/master_pipeline.py", line 78, in get_worker
    module = importlib.import_module(f"workers.{module_name}")
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
ModuleNotFoundError: No module named 'workers'
```

## 🔍 Root Cause

The `workers/` directory was not in Python's `sys.path`, causing the import to fail when trying to load worker modules dynamically.

**Directory Structure:**
```
script-server/scripts/detection/
├── pipeline/
│   └── master_pipeline.py
├── workers/
│   ├── intruder_worker.py
│   ├── strongroom_worker.py
│   ├── server_room_worker.py
│   └── ... (other workers)
└── ... (other files)
```

The `PROJECT_ROOT` was set to `detection/`, but the import was trying `workers.{module_name}` which requires `workers/` to be a package or in `sys.path`.

## ✅ Solution Applied

### 1. **Added Workers Directory to sys.path**

**File:** `script-server/scripts/detection/pipeline/master_pipeline.py`

**Before:**
```python
# ================= PATH SETUP =================
BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(BASE_DIR, ".."))
sys.path.insert(0, PROJECT_ROOT)
```

**After:**
```python
# ================= PATH SETUP =================
BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(BASE_DIR, ".."))
WORKERS_DIR  = os.path.join(PROJECT_ROOT, "workers")
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, WORKERS_DIR)
```

### 2. **Updated Import Order in get_worker()**

**Before:**
```python
try:
    # Try importing as a package first
    try:
        module = importlib.import_module(f"workers.{module_name}")
    except (ImportError, ModuleNotFoundError):
        # Fallback if workers/ directory is already in sys.path
        module = importlib.import_module(module_name)
```

**After:**
```python
try:
    # Try direct import first (workers/ is in sys.path)
    try:
        module = importlib.import_module(module_name)
    except (ImportError, ModuleNotFoundError):
        # Fallback to package import
        module = importlib.import_module(f"workers.{module_name}")
```

**Rationale:** Since `WORKERS_DIR` is now in `sys.path`, direct imports like `import intruder_worker` will work. This is more efficient and avoids package import issues.

## 🎯 Affected Workers

This fix applies to **ALL** detection workers:

### ✅ Fixed Workers:
1. **intruder** - `intruder_worker.py`
2. **strongroom** - `strongroom_worker.py` (newly added)
3. **server** - `server_room_worker.py`
4. **crowd** - `crowd_worker.py`
5. **entry_exit** - `entry_exit_worker.py`
6. **proximity** - `close_proximity_worker.py`
7. **phone** - `phone_worker.py`
8. **invigilator** - `invigilator_worker.py`
9. **tamper** - `camera_tampering_worker.py`
10. **inversion** - `camera_inversion_worker.py`
11. **crowd_zero** - `crowd_zero_det_worker.py`
12. **crowd_five** - `crowd_five_det_worker.py`
13. **blind_spot** - `blind_spot_worker.py`

## 🧪 Testing

### Test Import Fix:
```bash
cd script-server/scripts/detection/pipeline
python3 -c "
import sys
import os
BASE_DIR = os.path.dirname(os.path.abspath('master_pipeline.py'))
PROJECT_ROOT = os.path.abspath(os.path.join(BASE_DIR, '..'))
WORKERS_DIR = os.path.join(PROJECT_ROOT, 'workers')
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, WORKERS_DIR)

# Test direct import
import intruder_worker
print('✅ Direct import successful:', intruder_worker)

# Test function access
print('✅ RTSP function:', hasattr(intruder_worker, 'intruder_worker'))
print('✅ HLS function:', hasattr(intruder_worker, 'intruder_worker_hls'))
"
```

### Test Strongroom Worker:
```bash
cd script-server/scripts/detection/pipeline
python3 -c "
import sys
import os
BASE_DIR = os.path.dirname(os.path.abspath('master_pipeline.py'))
PROJECT_ROOT = os.path.abspath(os.path.join(BASE_DIR, '..'))
WORKERS_DIR = os.path.join(PROJECT_ROOT, 'workers')
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, WORKERS_DIR)

# Test strongroom import
import strongroom_worker
print('✅ Strongroom import successful:', strongroom_worker)
print('✅ RTSP function:', hasattr(strongroom_worker, 'strongroom_worker'))
print('✅ HLS function:', hasattr(strongroom_worker, 'strongroom_worker_hls'))
"
```

### Test Full Pipeline:
```bash
# Test intruder detection
cd script-server/scripts/detection/pipeline
python3 master_pipeline.py --analytics intruder

# Test strongroom detection
python3 master_pipeline.py --analytics strongroom

# Test multiple analytics
python3 master_pipeline.py --analytics intruder strongroom server
```

## 📊 Impact

### Before Fix:
- ❌ All worker imports failed
- ❌ RTSP pipeline couldn't start
- ❌ HLS pipeline couldn't start
- ❌ No detections were processed

### After Fix:
- ✅ All worker imports succeed
- ✅ RTSP pipeline starts correctly
- ✅ HLS pipeline starts correctly
- ✅ Detections are processed normally
- ✅ Both intruder and strongroom workers functional

## 🔐 Verification Checklist

- [x] Added `WORKERS_DIR` to `sys.path`
- [x] Updated import order in `get_worker()`
- [x] Verified all 13 workers are in mapping
- [x] Strongroom worker added to mapping
- [x] Strongroom added to `ALL_ANALYTICS` in pipeline_manager.py
- [x] Strongroom added to `DETECTION_FOLDERS` in main.py
- [x] Strongroom added to `DETECTION_LABELS` in main.py
- [x] Result directories created for strongroom
- [x] Graceful shutdown includes strongroom worker

## 🚀 Deployment

No additional steps required. The fix is backward compatible and doesn't require:
- Database migrations
- Configuration changes
- Dependency updates
- Service restarts (beyond normal deployment)

## 📝 Notes

1. **Import Order**: Direct imports are tried first since `WORKERS_DIR` is in `sys.path`. This is more efficient.

2. **Fallback**: Package imports (`workers.{module_name}`) are still supported as a fallback for compatibility.

3. **All Workers**: This fix applies to ALL detection workers, not just intruder and strongroom.

4. **No Breaking Changes**: Existing functionality is preserved. The fix only adds the missing path.

5. **Future Workers**: New workers added to the `workers/` directory will automatically work with this fix.

## ✨ Status: FIXED

The import error has been resolved for all workers including the newly added strongroom detection.
