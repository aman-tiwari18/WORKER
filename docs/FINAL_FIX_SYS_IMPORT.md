# Final Fix: sys Import Missing

## 🐛 Problem

After fixing the signal handler issue, a new error appeared:
```
NameError: name 'sys' is not defined. Did you forget to import 'sys'?
```

At line 4574 in `intruder_worker.py`:
```python
if _DETECTION_ROOT not in sys.path:
    sys.path.insert(0, _DETECTION_ROOT)
```

## 🔍 Root Cause

The PATH SETUP section was using `sys` but `sys` was imported later as `_sys`:
```python
# Line 4574: Uses sys (not imported yet!)
if _DETECTION_ROOT not in sys.path:
    sys.path.insert(0, _DETECTION_ROOT)

# Line 4579: sys imported as _sys (too late!)
import sys as _sys
```

## ✅ Solution

Added `sys` import at the top with other imports:

**Before:**
```python
import time
import os
import logging
import threading
import signal
import queue
import uuid
```

**After:**
```python
import time
import os
import sys  # ← Added this
import logging
import threading
import signal
import queue
import uuid
```

## 📁 Files Fixed

- ✅ `script-server/scripts/detection/workers/intruder_worker.py` - Added `sys` import

## ✅ Other Workers

Checked all other workers:
- ✅ `strongroom_worker.py` - No PATH SETUP section, no issue
- ✅ `invigilator_worker.py` - Already has `sys` imported
- ✅ All other workers - No issues

## 🧹 Cache Cleared

Ran `./clear_python_cache.sh` to remove old bytecode.

## 🧪 Test

```bash
cd script-server/scripts/detection/pipeline
python3 master_pipeline.py --analytics intruder
```

**Expected:** No more NameError, detection runs successfully.

## ✅ Status

**FIXED** - sys import added, cache cleared, ready to test.
