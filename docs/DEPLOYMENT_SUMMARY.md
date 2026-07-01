# Horizontal Scaling - Deployment Summary

## What Was Implemented

### 1. Camera Distribution System
- **File**: `script-server/app/camera_distributor.py`
- Automatically filters cameras based on worker ID and range
- Supports static range assignment (e.g., worker-2 handles cameras 5001-10000)
- Integrated into database queries (`get_rtsp_cameras`, `get_hls_cameras`)

### 2. Worker Docker Compose
- **File**: `docker-compose.worker.yml`
- Standalone compose file for GPU worker nodes
- Only runs script-server + Ollama (no backend, frontend, or database)
- Connects to main server's MySQL and NFS

### 3. Worker Configuration
- **File**: `.env.worker.example`
- Template for worker environment variables
- Configures: worker ID, camera range, main server IP, ports

### 4. Automated Setup Script
- **File**: `setup_worker.sh`
- One-command worker node setup
- Installs NFS, mounts share, configures environment, starts services
- Usage: `sudo ./setup_worker.sh 2 192.168.1.100 5001 10000`

### 5. Docker Updates
- Added `nfs-common` to script-server Dockerfile
- MySQL configured for remote connections (max 500 connections)
- Main server supports NFS volume mounts
- Worker identification in environment variables

### 6. Database Integration
- Camera queries automatically filter by worker assignment
- No code changes needed in detection scripts
- Transparent to existing functionality

## Quick Deployment

### Main Server
```bash
docker-compose up -d
```

### Worker Nodes
```bash
# Automated
sudo ./setup_worker.sh <worker_id> <main_ip> <cam_start> <cam_end>

# Manual
cp .env.worker.example .env.worker
# Edit .env.worker
docker-compose -f docker-compose.worker.yml up -d
```

## Camera Distribution Example

**20,000 cameras across 4 GPU systems:**
- Main Server: 1-5,000
- Worker 2: 5,001-10,000
- Worker 3: 10,001-15,000
- Worker 4: 15,001-20,000

## Testing

```bash
# Test distribution logic
python3 test_scaling.py

# Check worker health
curl http://localhost:8000/health
```

## Files Created/Modified

### New Files
- `docker-compose.worker.yml` - Worker node compose
- `.env.worker.example` - Worker config template
- `script-server/app/camera_distributor.py` - Distribution logic
- `setup_worker.sh` - Automated setup script
- `test_scaling.py` - Test script

### Modified Files
- `docker-compose.yml` - MySQL remote access, NFS support
- `script-server/Dockerfile` - Added nfs-common
- `script-server/scripts/detection/utils/db_client.py` - Integrated camera filtering
- `script-server/app/main.py` - Added worker info to health endpoint

## Architecture

```
Main Server (192.168.1.100)
├── MySQL (port 3307) ← All workers connect here
├── NFS Server (/mnt/nfs/cctv_images) ← Shared image storage
├── Backend API
├── Frontend UI
└── Script-Server (cameras 1-5000)

Worker Node 2 (192.168.1.101)
├── NFS Client (mounted from main)
└── Script-Server (cameras 5001-10000)

Worker Node 3 (192.168.1.102)
├── NFS Client (mounted from main)
└── Script-Server (cameras 10001-15000)

Worker Node 4 (192.168.1.103)
├── NFS Client (mounted from main)
└── Script-Server (cameras 15001-20000)
```

## Network Requirements
- MySQL port 3307 accessible from all workers
- NFS port 2049 accessible from all workers
- 1-10 Gbps network recommended

## Storage
- All images saved to NFS: `/mnt/nfs/cctv_images`
- Shared across all workers
- Accessible from main server for frontend display

## Next Steps
1. Configure NFS server on main server
2. Deploy main server
3. Deploy worker nodes using setup script
4. Verify connectivity and camera distribution
5. Start processing cameras

---
**Status**: Implementation Complete ✓
**Tested**: Camera distribution logic verified ✓
