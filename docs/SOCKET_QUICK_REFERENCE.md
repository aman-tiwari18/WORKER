# Socket Implementation - Quick Reference

## 🎯 **How It Works Now**

### **Single Socket Connection**
All socket communication is handled by `SocketContext.jsx` - one connection for the entire app.

---

## 📦 **Provider Hierarchy**

```javascript
<AuthProvider>
  <DetectionCountsProvider>
    <SocketProvider>  ← Handles all socket events
      <App />
    </SocketProvider>
  </DetectionCountsProvider>
</AuthProvider>
```

---

## 🔌 **Using Socket in Components**

### **Read Connection Status**
```javascript
import { useSocket } from '../contexts/SocketContext';

const MyComponent = () => {
  const { connected } = useSocket();
  
  return <div>Status: {connected ? '🟢 Connected' : '🔴 Disconnected'}</div>;
};
```

### **Set Client Exams (for filtering)**
```javascript
import { useSocket } from '../contexts/SocketContext';

const ClientDashboard = () => {
  const { setClientExams } = useSocket();
  
  useEffect(() => {
    if (dashboardData?.exams) {
      setClientExams(dashboardData.exams.map(e => e.id));
    }
  }, [dashboardData, setClientExams]);
};
```

### **Read Detection Counts**
```javascript
import { useDetectionCounts } from '../contexts/DetectionCountsContext';

const MyComponent = () => {
  const { unfilteredCounts, allCounts } = useDetectionCounts();
  
  // Total across all types
  const total = Object.values(unfilteredCounts).reduce((a, b) => a + b, 0);
  
  // Specific type
  const proximityCount = unfilteredCounts.proximity || 0;
  
  return <div>Total: {total}, Proximity: {proximityCount}</div>;
};
```

---

## 🎬 **Socket Events**

### **Backend Emissions**
| Event | When | Payload |
|-------|------|---------|
| `detection:new` | New admin detection | `{ type, image_url, ... }` |
| `client-detection:new` | Admin pushes to client | `{ type, image_url, exam_id, ... }` |
| `client-detection:remove` | Client removes detection | `{ uid, success }` |

### **Frontend Handling**
All events are automatically handled by `SocketContext`:
- Batches events every 2 seconds
- Updates `unfilteredCounts` in context
- Updates `allCounts` in context
- Applies security filtering for client detections

---

## 📊 **Count Updates**

### **Automatic Updates**
Counts update automatically when:
1. Backend emits `detection:new` or `client-detection:new`
2. SocketContext batches the events (every 2 seconds)
3. Context updates `unfilteredCounts` and `allCounts`
4. All components re-render with new counts

### **Manual Refresh**
To manually fetch counts:
```javascript
const fetchCounts = async () => {
  const res = await axios.get(`${API_BASE_1}/detections?page=1&limit=1`);
  setUnfilteredCounts(res.data.counts || {});
};
```

---

## 🔒 **Security Filtering**

### **Client Detections**
SocketContext automatically filters client detections:
```javascript
// Only processes if detection belongs to client's exams
if (clientExamsRef.current.length === 0 || 
    clientExamsRef.current.includes(String(item.exam_id))) {
  clientQueue.push(item);
}
```

### **Setting Client Exams**
```javascript
const { setClientExams } = useSocket();
setClientExams([1, 2, 3]); // Array of exam IDs
```

---

## 🐛 **Debugging**

### **Console Logs**
SocketContext logs important events:
- `✅ Socket connected` - Connection established
- `❌ Socket disconnected` - Connection lost
- `📊 Processed X admin detections` - Batch processed
- `📊 Processed X client detections` - Batch processed
- `🔐 Client exams set: [...]` - Exam filter updated
- `🔌 Socket cleanup completed` - Cleanup on unmount

### **Check Connection**
```javascript
const { socket, connected } = useSocket();
console.log('Socket ID:', socket?.id);
console.log('Connected:', connected);
```

### **Browser DevTools**
1. Open Network tab
2. Filter by "WS" (WebSocket) or "socket.io"
3. Should see only **ONE** connection

---

## ⚠️ **Common Mistakes**

### **❌ DON'T: Create duplicate socket connections**
```javascript
// ❌ BAD - Creates duplicate connection
const socket = io(SOCKET_URL, { ... });
```

### **✅ DO: Use the context**
```javascript
// ✅ GOOD - Uses centralized connection
const { connected } = useSocket();
```

### **❌ DON'T: Use local state for counts**
```javascript
// ❌ BAD - Local state gets out of sync
const [localCount, setLocalCount] = useState(0);
```

### **✅ DO: Use context counts**
```javascript
// ✅ GOOD - Always in sync
const { unfilteredCounts } = useDetectionCounts();
const total = Object.values(unfilteredCounts).reduce((a, b) => a + b, 0);
```

---

## 🔧 **Troubleshooting**

### **Counts not updating?**
1. Check console for socket connection logs
2. Verify SocketProvider is in App.jsx
3. Check Network tab for socket connection
4. Verify backend is emitting events

### **Counts inconsistent?**
1. All components should use `unfilteredCounts` from context
2. Check for any remaining local state
3. Verify no duplicate socket connections

### **Client not receiving detections?**
1. Check if `setClientExams` was called
2. Verify exam IDs match backend payload
3. Check console for security filtering logs

### **Memory leaks?**
1. Verify SocketContext cleanup is working
2. Check for any remaining socket connections in components
3. Monitor browser memory usage over time

---

## 📝 **File Locations**

| File | Purpose |
|------|---------|
| `src/contexts/SocketContext.jsx` | Socket connection & event handling |
| `src/contexts/DetectionCountsContext.jsx` | Count state management |
| `src/App.jsx` | Provider setup |
| `src/pages/Dashboard.jsx` | Admin dashboard (uses context) |
| `src/pages/ClientDetectionDashboard.jsx` | Client dashboard (uses context) |
| `src/pages/Detections.jsx` | Admin detections page |
| `src/pages/ClientDetections.jsx` | Client detections page |

---

## 🚀 **Performance**

### **Before Centralization**
- 4 socket connections
- 4 separate event handlers
- Inconsistent batching (1s and 2s)
- Higher memory usage

### **After Centralization**
- 1 socket connection ✅
- 1 event handler per event type ✅
- Consistent 2s batching ✅
- Lower memory usage ✅

---

## 📚 **Additional Resources**

- Socket.IO Client Docs: https://socket.io/docs/v4/client-api/
- React Context API: https://react.dev/reference/react/useContext
- WebSocket Protocol: https://developer.mozilla.org/en-US/docs/Web/API/WebSocket

---

## ✅ **Checklist for New Features**

When adding new socket events:
1. [ ] Add event listener in `SocketContext.jsx`
2. [ ] Update context state if needed
3. [ ] Add console logging for debugging
4. [ ] Test with multiple users
5. [ ] Verify no memory leaks
6. [ ] Update this documentation

---

**Last Updated:** $(date)
**Version:** 1.0.0
