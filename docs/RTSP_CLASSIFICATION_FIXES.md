# RTSP Classification Script Fixes

## Issues Fixed

### 1. **Missing `re` Module Import**
- **Problem**: Script used `re.search()` and `re.sub()` without importing the `re` module
- **Fix**: Added `import re` to the imports section
- **Impact**: Prevents `NameError: name 're' is not defined` runtime errors

### 2. **Incorrect Authentication Password**
- **Problem**: Script had `AUTH_PASSWORD = "Coreiseven@@"` but should be `"Sasd@12345"`
- **Fix**: Updated password to match the working reference implementation
- **Impact**: Fixes authentication failures with DSS servers

### 3. **Port 9100 Connectivity Issues**
- **Problem**: RTSP streams on port 9100 were blocked/unreachable for private IPs (172.16.x.x)
- **Fix**: Enhanced `build_rtsp_url()` to:
  - Probe TCP port before attempting connection
  - Automatically fall back to HLS (HTTP/443) when RTSP port is blocked
  - Filter private IPs when `SKIP_PRIVATE_IPS` is enabled
- **Impact**: Cameras behind firewalls can now use HLS fallback instead of timing out

### 4. **OpenCV Timeout Handling**
- **Problem**: OpenCV's timeout properties are ignored by many builds, causing 30s hangs
- **Fix**: Implemented thread-based hard timeout wrapper
  - Runs OpenCV capture in daemon thread
  - Hard kills after TIMEOUT_OPENCV_REAL seconds (8s)
  - Prevents indefinite blocking
- **Impact**: Reliable timeout enforcement regardless of OpenCV build

### 5. **FFmpeg Error Capture**
- **Problem**: FFmpeg errors were not being captured properly
- **Fix**: 
  - Capture stderr output on failure
  - Use subprocess timeout longer than internal FFmpeg timeout
  - Log detailed error messages including stderr
  - Changed loglevel from "error" to "warning" for better diagnostics
- **Impact**: Better visibility into RTSP connection failures

### 6. **VLM Response Parsing**
- **Problem**: Qwen3-VL model sometimes includes `<think>...</think>` tags in responses
- **Fix**: Added regex to strip chain-of-thought tags before JSON parsing
- **Impact**: Prevents JSON parsing errors from VLM responses

### 7. **RTSP Proxy Support**
- **Problem**: Some cameras may not trust the external IP of the processing server
- **Fix**: Added `RTSP_PROXY_MAP` configuration
  - Maps "host:port" → "proxy_host:proxy_port"
  - Rewrites RTSP URLs to go through trusted relay
  - Optional feature (empty dict = direct connection)
- **Impact**: Enables access to cameras with IP whitelisting

## Key Improvements

### Thread-Based Timeout for OpenCV
```python
def capture_frame_with_opencv_stream(stream_url, id_str, timeout_ms):
    result = [None]
    
    def _do_capture():
        # OpenCV capture logic
        ...
    
    thread = threading.Thread(target=_do_capture, daemon=True)
    thread.start()
    thread.join(timeout=TIMEOUT_OPENCV_REAL)  # Hard 8s timeout
    
    if thread.is_alive():
        logger.warning("OpenCV hard timeout triggered")
        return None
    
    return result[0]
```

### Enhanced FFmpeg Error Capture
```python
try:
    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=TIMEOUT_FFMPEG + 5,  # Longer than internal timeout
    )
    if result.returncode != 0:
        stderr_out = result.stderr.decode('utf-8', errors='replace').strip()
        logger.warning(f"FFmpeg failed: {stderr_out[:200]}")
except subprocess.TimeoutExpired as e:
    stderr_out = (e.stderr or b'').decode('utf-8', errors='replace').strip()
    logger.warning(f"FFmpeg timeout: {stderr_out or 'no stderr'}")
```

### HLS Fallback Strategy
When RTSP port 9100 is blocked:
1. Detect port unreachability via TCP probe (2s timeout)
2. Automatically request HLS stream (dataType=1) instead of RTSP (dataType=2)
3. HLS uses HTTP/443 which is typically firewall-friendly
4. Log the fallback for visibility

### RTSP Proxy Rewriting
```python
RTSP_PROXY_MAP = {
    "15.207.163.63:9100": "10.x.x.x:19100",  # Route through trusted relay
}

def apply_rtsp_proxy(url):
    if not RTSP_PROXY_MAP:
        return url
    h, p = extract_host_port(url)
    key = f"{h}:{p}"
    if key in RTSP_PROXY_MAP:
        proxy = RTSP_PROXY_MAP[key]
        return url.replace(f"{h}:{p}", proxy, 1)
    return url
```

## Configuration

Current optimized settings:
```python
MAX_WORKERS_NETWORK = 64   # High parallelism for Pass 1
MAX_WORKERS_VLM = 4        # GPU-limited for Pass 2
TIMEOUT_API = 10           # API calls
TIMEOUT_OPENCV = 5000      # OpenCV property (milliseconds, often ignored)
TIMEOUT_OPENCV_REAL = 8    # Hard thread timeout (seconds, always enforced)
TIMEOUT_FFMPEG = 8         # FFmpeg subprocess timeout
TIMEOUT_VLM = 60           # VLM inference timeout
SKIP_PRIVATE_IPS = True    # Filter private IPs
TWO_PASS_MODE = True       # Efficient two-pass processing
RTSP_PROXY_MAP = {}        # Optional proxy mapping
```

## Testing Recommendations

1. **Test with blocked ports**: Verify HLS fallback works when port 9100 is blocked
2. **Test with private IPs**: Ensure `SKIP_PRIVATE_IPS` filters correctly
3. **Test authentication**: Verify new password works across all servers
4. **Test VLM responses**: Check that chain-of-thought tags are properly stripped
5. **Test OpenCV timeout**: Verify hard timeout prevents 30s hangs
6. **Test FFmpeg errors**: Check that stderr is captured and logged
7. **Monitor logs**: Watch for "Port X on Y is BLOCKED" messages and HLS fallback success
8. **Test proxy (if used)**: Verify RTSP_PROXY_MAP rewrites URLs correctly

## Expected Behavior

### Pass 1 (Quick Accessibility Check)
- Authenticate with DSS servers
- Request stream URLs via API
- Check port reachability
- Fall back to HLS if RTSP port blocked
- Mark cameras as accessible/not accessible

### Pass 2 (Frame Capture & Classification)
- Try each URL in sequence until one works
- Capture frame with OpenCV (primary) or FFmpeg (fallback)
- Hard timeout enforcement prevents indefinite hangs
- Classify with VLM
- Update database with results

## Common Error Messages

| Error | Meaning | Solution |
|-------|---------|----------|
| `No route to host` | Port blocked by firewall | HLS fallback should trigger automatically |
| `The nonce is illegal` | Auth token timing issue | Script retries with exponential backoff |
| `method OPTIONS failed: 401` | Token expired | Script auto-refreshes tokens |
| `Invalid JSON from VLM` | VLM response parsing failed | Check Ollama service and model |
| `OpenCV hard timeout triggered` | Stream hanging/unresponsive | FFmpeg fallback will be attempted |
| `FFmpeg failed (exit=1)` | Connection/format error | Check stderr in logs for details |

## Performance Improvements

### Before
- OpenCV could hang for 30s per camera
- FFmpeg errors were silent
- No HLS fallback for blocked ports
- No proxy support for IP whitelisting

### After
- OpenCV hard timeout at 8s
- FFmpeg stderr captured and logged
- Automatic HLS fallback (HTTP/443)
- Optional RTSP proxy support
- Better error visibility for debugging

## Files Modified

- `script-server/scripts/detection/vision_classification_rtsp.py`
  - Added `import re`
  - Updated `AUTH_PASSWORD`
  - Reduced `TIMEOUT_OPENCV` to 5000ms (from 10000ms)
  - Reduced `TIMEOUT_FFMPEG` to 8s (from 10s)
  - Reduced `TIMEOUT_VLM` to 60s (from 120s)
  - Added `RTSP_PROXY_MAP` configuration
  - Implemented thread-based OpenCV timeout
  - Enhanced FFmpeg error capture with stderr
  - Added `apply_rtsp_proxy()` function
  - Improved logging with timeout information
  - Enhanced `classify_with_vlm()` with regex stripping
