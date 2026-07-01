# Signal Handler Threading Fix

## 🐛 Problem

When worker modules are imported from threads (not the main thread), Python raises:
```
ValueError: signal only works in main thread of the main interpreter
```

This happens because `signal.signal()` can only be called from the main thread, but `master_pipeline.py` imports workers from worker threads (`start_producer` runs in threads).

## ✅ Solution Applied

### Files Fixed:

1. **`intruder_worker.py`** - Wrapped signal registration in try-except
2. **`strongroom_worker.py`** - Wrapped signal registration in try-except  
3. **`invigilator_worker.py`** - Wrapped signal registration in try-except

### Other Workers (Already Correct):
These workers already use the correct pattern:
- `blind_spot_worker.py`
- `camera_inversion_worker.py`
- `camera_tampering_worker.py`
- `close_proximity_worker.py`
- `crowd_five_det_worker.py`
- `crowd_worker.py`
- `crowd_zero_det_worker.py`
- `entry_exit_worker.py`
- `phone_worker.py`
- `server_room_worker.py`

They use:
```python
if threading.current_thread() is threading.main_thread():
    signal.signal(signal.SIGINT,  _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)
```

## 🔧 Fix Applied

**Before:**
```python
signal.signal(signal.SIGINT,  _handle_stop)
signal.signal(signal.SIGTERM, _handle_stop)
```

**After:**
```python
# Signal handlers can only be set in the main thread
# When imported from worker threads, this will fail silently
try:
    signal.signal(signal.SIGINT,  _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)
except ValueError:
    # Not in main thread, signal handlers will be set by master_pipeline
    pass
```

## 🧹 Clear Python Cache

After applying the fix, clear Python's bytecode cache:

```bash
# Clear all __pycache__ directories
find script-server/scripts/detection/workers -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null

# Or clear specific worker cache
rm -rf script-server/scripts/detection/workers/__pycache__/intruder_worker*.pyc
rm -rf script-server/scripts/detection/workers/__pycache__/strongroom_worker*.pyc
rm -rf script-server/scripts/detection/workers/__pycache__/invigilator_worker*.pyc
```

## 🎯 Why This Works

1. **Try-Except Approach:** Catches the ValueError when not in main thread
2. **Threading Check Approach:** Prevents the call entirely if not in main thread
3. **Master Pipeline:** Still handles signals in the main thread via its own handlers

Both approaches are valid. The try-except is more explicit about the error handling.

## ✅ Status

- **Fixed:** intruder_worker, strongroom_worker, invigilator_worker
- **Already Correct:** All other workers
- **Action Required:** Clear Python cache and restart

## 🧪 Test

```bash
# Clear cache
find script-server/scripts/detection -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null

# Test
cd script-server/scripts/detection/pipeline
python3 master_pipeline.py --analytics intruder
```

Should no longer see ValueError about signal handlers.
