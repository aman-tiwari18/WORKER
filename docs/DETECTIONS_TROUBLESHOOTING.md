# Detections Page Troubleshooting Guide

## Common Issues & Solutions

### Issue 1: Page Still Loads Slowly

**Symptoms:**
- Initial load takes 3+ seconds
- White screen on page load

**Possible Causes:**
1. **Backend slow response**
   - Check API response time in Network tab
   - Should be < 500ms for detections endpoint

2. **Large dataset**
   - Check if returning 1000+ items
   - Reduce `rowsPerPage` to 20-50

3. **Slow database queries**
   - Check backend logs for query time
   - Add indexes on frequently queried columns

**Solutions:**
```javascript
// Reduce initial page size
const [rowsPerPage, setRowsPerPage] = useState(20); // Instead of 50

// Add loading skeleton
{loading && <CircularProgress />}
```

---

### Issue 2: Push Button Still Freezes UI

**Symptoms:**
- Clicking "Push to Client" multiple times freezes page
- Button stays in "Pushing..." state

**Possible Causes:**
1. **Queue not working**
   - Check console for errors
   - Verify `RequestQueue` class is defined

2. **Backend timeout**
   - Push endpoint taking too long
   - Check backend logs

**Solutions:**
```javascript
// Add timeout to push request
await axios.post(url, data, {
  timeout: 10000 // 10 second timeout
});

// Add visual feedback
{copyingId && (
  <Box sx={{ position: 'fixed', top: 16, right: 16 }}>
    <CircularProgress size={24} />
  </Box>
)}
```

---

### Issue 3: Search Still Lags

**Symptoms:**
- Typing in search box feels sluggish
- API calls fire on every keystroke

**Possible Causes:**
1. **Debounce not working**
   - Check if `useDebounce` hook is defined
   - Verify `debouncedSearch` is used in `buildDetectionParams`

2. **Too many re-renders**
   - Check React DevTools Profiler
   - Look for unnecessary component updates

**Solutions:**
```javascript
// Verify debounce is working
console.log('Search:', search);
console.log('Debounced:', debouncedSearch);

// Increase debounce delay if needed
const debouncedSearch = useDebounce(search, 800); // 800ms instead of 500ms
```

---

### Issue 4: Duplicate API Requests

**Symptoms:**
- Network tab shows multiple identical requests
- Same params sent multiple times

**Possible Causes:**
1. **Deduplication not working**
   - Check `lastFetchParamsRef` logic
   - Verify params comparison

2. **Multiple useEffect triggers**
   - Check dependency arrays
   - Look for missing dependencies

**Solutions:**
```javascript
// Add logging to track requests
const fetchDetections = useCallback(async (pg = 1, rpp = rowsPerPage) => {
  const paramsString = params.toString();
  console.log('Fetching with params:', paramsString);
  
  if (lastFetchParamsRef.current === paramsString) {
    console.log('Skipping duplicate request');
    return;
  }
  
  lastFetchParamsRef.current = paramsString;
  // ... rest of fetch logic
}, []);
```

---

### Issue 5: Memory Leaks

**Symptoms:**
- Page gets slower over time
- Browser tab uses increasing memory
- Console shows "Can't perform state update on unmounted component"

**Possible Causes:**
1. **Requests not aborted**
   - Check if `abortControllerRef` is used
   - Verify cleanup in useEffect

2. **Timers not cleared**
   - Check if `clearTimeout` is called
   - Verify interval cleanup

**Solutions:**
```javascript
// Verify cleanup
useEffect(() => {
  return () => {
    console.log('Cleaning up...');
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
    }
    if (fetchTimeoutRef.current) {
      clearTimeout(fetchTimeoutRef.current);
    }
  };
}, []);

// Check for memory leaks in Chrome DevTools
// 1. Open DevTools → Memory tab
// 2. Take heap snapshot
// 3. Navigate away and back
// 4. Take another snapshot
// 5. Compare - should not grow significantly
```

---

### Issue 6: Filters Don't Work

**Symptoms:**
- Changing filters doesn't update results
- Search doesn't filter data

**Possible Causes:**
1. **Debounce delay too long**
   - User expects instant results
   - 300ms might feel slow

2. **Server-side filtering broken**
   - Check API response
   - Verify query params

**Solutions:**
```javascript
// Reduce debounce for filters (not search)
useEffect(() => {
  const timer = setTimeout(() => {
    fetchDetections(1);
  }, 100); // 100ms for filters, 500ms for search
  
  return () => clearTimeout(timer);
}, [typeFilter, dateFilter, examFilter]);

// Verify params are sent correctly
console.log('Fetching with params:', params.toString());
```

---

### Issue 7: Optimistic Updates Don't Rollback

**Symptoms:**
- Push fails but UI shows as pushed
- Client total count is wrong

**Possible Causes:**
1. **Error handling missing**
   - Check try/catch block
   - Verify rollback logic

2. **State not updating**
   - Check if `setPushedUids` is called in catch
   - Verify `setClientTotal` rollback

**Solutions:**
```javascript
try {
  await pushQueue.add(async () => {
    return await axios.post(url, data);
  });
} catch (err) {
  console.error('Push failed, rolling back:', err);
  
  // Rollback optimistic update
  setPushedUids((prev) => {
    const next = new Set(prev);
    items.forEach(itm => {
      const key = itm.uid || itm.id || itm.image_url;
      console.log('Rolling back:', key);
      next.delete(key);
    });
    return next;
  });
  
  setClientTotal((prev) => {
    const newTotal = prev - items.length;
    console.log('Rollback client total:', prev, '→', newTotal);
    return newTotal;
  });
}
```

---

## Performance Monitoring

### Chrome DevTools

1. **Network Tab**
   - Check API response times
   - Look for duplicate requests
   - Verify request cancellation

2. **Performance Tab**
   - Record page load
   - Look for long tasks (> 50ms)
   - Check for layout thrashing

3. **React DevTools Profiler**
   - Record interaction
   - Look for unnecessary re-renders
   - Check component render times

### Console Logging

Add strategic logs:
```javascript
// In fetchDetections
console.log('[Fetch] Starting with params:', params.toString());
console.log('[Fetch] Completed in', Date.now() - startTime, 'ms');

// In pushToClient
console.log('[Push] Queuing items:', items.length);
console.log('[Push] Queue size:', pushQueue.queue.length);

// In useDebounce
console.log('[Debounce] Value changed:', value);
console.log('[Debounce] Debounced value:', debouncedValue);
```

---

## Quick Fixes

### If page is slow:
1. Reduce `rowsPerPage` to 20
2. Increase debounce to 800ms
3. Check backend response time
4. Add loading skeleton

### If push button freezes:
1. Check `RequestQueue` is defined
2. Add timeout to axios request
3. Verify optimistic updates
4. Check backend logs

### If search lags:
1. Verify `useDebounce` hook
2. Check `debouncedSearch` is used
3. Increase debounce delay
4. Remove client-side filtering

### If memory leaks:
1. Verify abort controller cleanup
2. Check timeout cleanup
3. Remove event listeners
4. Clear intervals

---

## Testing Commands

```bash
# Check bundle size
npm run build
ls -lh build/static/js/*.js

# Run performance audit
npm run build
npx serve -s build
# Open Chrome DevTools → Lighthouse → Performance

# Check for memory leaks
# 1. Open page
# 2. Interact (search, filter, push)
# 3. Chrome DevTools → Memory → Take snapshot
# 4. Repeat interactions
# 5. Take another snapshot
# 6. Compare - should not grow significantly
```

---

## When to Contact Backend Team

Contact backend if:
- API response time > 1 second
- Push endpoint times out
- Database queries are slow
- Server returns 500 errors
- WebSocket disconnects frequently

Provide:
- Network tab screenshot
- Console errors
- Request/response payloads
- Timestamp of issue

---

## Emergency Rollback

If optimizations cause issues:

```bash
# Revert to previous version
git log --oneline  # Find commit before optimization
git revert <commit-hash>

# Or restore specific file
git checkout HEAD~1 -- src/pages/Detections.jsx
```

Then report issue with:
- Steps to reproduce
- Console errors
- Network tab screenshot
- Browser/OS version
