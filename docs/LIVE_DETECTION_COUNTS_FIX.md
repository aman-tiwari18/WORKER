# Live Detection Counts Fix

## Problem
Live detection counts were not updating continuously in the UI (Layout.jsx sidebar, AppBar, Dashboard.jsx, and ClientDetectionDashboard.jsx) when new detections arrived via Socket.IO.

## Root Causes Identified

1. **Socket Context Issue**: The `detection:counts` and `client-detection:counts` events were **replacing** the entire counts object instead of **merging** with existing counts, potentially overwriting incremental updates from the batched queue.

2. **Context Value Stability**: The DetectionCountsContext was creating a new context value object on every render, which could cause unnecessary re-renders or prevent proper re-renders.

3. **Inline Calculations**: Components were calculating totals inline in JSX without memoization, which could miss updates.

## Changes Made

### 1. SocketContext.jsx
**Fixed socket event handlers to merge counts instead of replacing:**

```javascript
// BEFORE: Replaced entire counts object
newSocket.on('detection:counts', (counts) => {
  setUnfilteredCounts(counts);
  setAllCounts(counts);
});

// AFTER: Merge with existing counts
newSocket.on('detection:counts', (counts) => {
  setUnfilteredCounts(prev => {
    const merged = { ...prev };
    for (const type in counts) {
      merged[type] = counts[type];
    }
    return merged;
  });
  setAllCounts(prev => {
    const merged = { ...prev };
    for (const type in counts) {
      merged[type] = counts[type];
    }
    return merged;
  });
  console.log('📊 Admin counts updated via socket:', counts);
});
```

Same fix applied to `client-detection:counts` event handler.

### 2. DetectionCountsContext.jsx
**Added context value memoization for stability:**

```javascript
// Added useMemo to prevent unnecessary re-renders
const contextValue = useMemo(() => ({
  allCounts,
  setAllCounts,
  unfilteredCounts,
  setUnfilteredCounts,
  totalLiveCount,
  typeFilter,
  setTypeFilter,
  exportToExcel,
  setExportToExcel,
  downloadConsolidatedReport,
  setDownloadConsolidatedReport,
}), [
  allCounts,
  unfilteredCounts,
  totalLiveCount,
  typeFilter,
  exportToExcel,
  downloadConsolidatedReport,
]);

return (
  <DetectionCountsContext.Provider value={contextValue}>
    {children}
  </DetectionCountsContext.Provider>
);
```

### 3. Layout.jsx
**Added memoized total calculation and debugging:**

```javascript
// Calculate total detections with useMemo for better reactivity
const totalDetections = React.useMemo(() => {
  const total = Object.values(unfilteredCounts).reduce((a, b) => a + b, 0);
  console.log('[Layout] Total detections updated:', total, unfilteredCounts);
  return total;
}, [unfilteredCounts]);

// Updated Chip to use memoized value
<Chip
  label={`TOTAL DETECTIONS | ${totalDetections.toLocaleString()}`}
  // ... other props
/>
```

**Added debugging to DetectionSubItems:**

```javascript
function DetectionSubItems({ counts, typeFilter, onTypeSelect }) {
  // Log when counts change
  React.useEffect(() => {
    console.log('[DetectionSubItems] Counts updated:', counts);
  }, [counts]);
  // ... rest of component
}
```

### 4. Dashboard.jsx
**Added memoized total calculation:**

```javascript
// BEFORE: Inline calculation
const totalDetections = Object.values(unfilteredCounts).reduce((a, b) => a + b, 0);

// AFTER: Memoized with debugging
const totalDetections = useMemo(() => {
  const total = Object.values(unfilteredCounts).reduce((a, b) => a + b, 0);
  console.log('[Dashboard] Total detections updated:', total, unfilteredCounts);
  return total;
}, [unfilteredCounts]);
```

### 5. ClientDetectionDashboard.jsx
**Added memoized total calculation:**

```javascript
// Same fix as Dashboard.jsx
const totalDetections = useMemo(() => {
  const total = Object.values(unfilteredCounts).reduce((a, b) => a + b, 0);
  console.log('[ClientDashboard] Total detections updated:', total, unfilteredCounts);
  return total;
}, [unfilteredCounts]);
```

## How It Works Now

1. **Socket receives new detection** → Adds to queue
2. **Every 2 seconds** → Batch processes queue and increments counts
3. **Backend sends `detection:counts`** → Merges with existing counts (doesn't replace)
4. **Context updates** → Triggers re-render with stable memoized value
5. **Components re-render** → Display updated counts in:
   - Layout AppBar (TOTAL DETECTIONS chip)
   - Layout Sidebar (individual detection type counts)
   - Dashboard (Total Detections card)
   - ClientDetectionDashboard (Total Detections card)

## Testing

To verify the fix is working:

1. Open browser console
2. Navigate to any page with detection counts (Dashboard, Detections, etc.)
3. Watch for console logs when new detections arrive:
   - `📊 Processed X admin detections`
   - `📊 Admin counts updated via socket: {...}`
   - `[Layout] Total detections updated: X {...}`
   - `[Dashboard] Total detections updated: X {...}`
   - `[DetectionSubItems] Counts updated: {...}`

4. Verify counts update in real-time:
   - AppBar chip shows increasing total
   - Sidebar shows individual type counts increasing
   - Dashboard card shows increasing total

## Files Modified

1. `cctv_frontend/src/contexts/SocketContext.jsx`
2. `cctv_frontend/src/contexts/DetectionCountsContext.jsx`
3. `cctv_frontend/src/components/Layout.jsx`
4. `cctv_frontend/src/pages/Dashboard.jsx`
5. `cctv_frontend/src/pages/ClientDetectionDashboard.jsx`

## Summary

The fix ensures that:
- ✅ Socket events merge counts instead of replacing them
- ✅ Context provides stable memoized values
- ✅ Components use memoized calculations for reactivity
- ✅ Debugging logs help track count updates
- ✅ All UI components (AppBar, Sidebar, Dashboard cards) update continuously with live detections
