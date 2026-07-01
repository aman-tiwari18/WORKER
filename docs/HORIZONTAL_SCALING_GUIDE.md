# Horizontal Scaling Guide - CCTV Monitoring System

## Overview

This guide explains how to scale the CCTV monitoring system horizontally across multiple GPU systems to handle large camera deployments (20K+ cameras).

## Architecture

### Main Server (System 1)
- **Components**: Backend API, Frontend UI, MySQL Database, Script-Server, NFS Server
- **Role**: Central coordination, database, user interface, and shared storage
- **Hardware**: Standard server + 1 GPU (H200 recommended)

### Worker Nodes (Systems 2, 3, 4, ...)
- **Components**: Script-Server only (Python GPU worker)
- **Role**: Distributed camera processing (detection + classification)
- **Hardware**: GPU system (H200 recommended) + NFS client

## Distribution Strategy

### Camera Assignment

**Example: 20,000 cameras across 4 GPU systems**

```
Main Server (GPU 1):    Cameras 1-5,000
Worker Node 2 (GPU 2):  Cameras 5,001-10,000
Worker Node 3 (GPU 3):  Cameras 10,001-15,000
Worker Node 4 (GPU 4):  Cameras 15,001-20,000
```

### Two Distribution Modes

#### 1. Static Range Assignment (Simpler)
- Each worker processes a fixed camera ID range
- Configured via environment variables
- Best for predictable workloads

#### 2. Dynamic Assignment (Advanced)
- Workers pull cameras from shared database queue
- Better load balancing
- Handles worker failures gracefully

## Prerequisites

### All Systems
1. Docker + Docker Compose installed
2. NVIDIA Container Toolkit installed
3. Network connectivity between all nodes

### Main Server
1. NFS server configured and running
2. MySQL database accessible on port 3307
3. Sufficient storage for image data

### Worker Nodes
1. NFS client configured
2. NFS mount point: `/mnt/nfs/cctv_images`
3. Network access to main server (MySQL port 3307)

## Setup Instructions

### Step 1: Main Server Setup

1. **Configure NFS Server**

```bash
# Install NFS server
sudo apt-get update
sudo apt-get install -y nfs-kernel-server

# Create shared directory
sudo mkdir -p /mnt/nfs/cctv_images
sudo chown nobody:nogroup /mnt/nfs/cctv_images
sudo chmod 777 /mnt/nfs/cctv_images

# Configure exports
sudo nano /etc/exports
```

Add worker nodes to `/etc/exports`:
```
/mnt/nfs/cctv_images 192.168.1.101(rw,sync,no_subtree_check)
/mnt/nfs/cctv_images 192.168.1.102(rw,sync,no_subtree_check)
/mnt/nfs/cctv_images 192.168.1.103(rw,sync,no_subtree_check)
```

```bash
# Apply configuration
sudo exportfs -ra
sudo systemctl restart nfs-kernel-server

# Verify
sudo exportfs -v
```

2. **Configure MySQL for Remote Access**

```bash
# Edit MySQL configuration
sudo nano /etc/mysql/mysql.conf.d/mysqld.cnf
```

Change:
```
bind-address = 0.0.0.0
```

```bash
# Restart MySQL
sudo systemctl restart mysql

# Grant remote access
mysql -u root -p
```

```sql
GRANT ALL PRIVILEGES ON cctv_monitoring.* TO 'root'@'%' IDENTIFIED BY 'rootpassword123';
FLUSH PRIVILEGES;
```

3. **Deploy Main Server**

```bash
# Start all services
docker-compose up -d

# Verify services
docker-compose ps
```

### Step 2: Worker Node Setup

Repeat for each worker node (Worker 2, 3, 4, ...):

1. **Install NFS Client**

```bash
sudo apt-get update
sudo apt-get install -y nfs-common
```

2. **Configure NFS Mount**

```bash
# Create mount point
sudo mkdir -p /mnt/nfs/cctv_images

# Test mount
sudo mount -t nfs 192.168.1.100:/mnt/nfs/cctv_images /mnt/nfs/cctv_images

# Verify
ls -la /mnt/nfs/cctv_images

# Make permanent
sudo nano /etc/fstab
```

Add to `/etc/fstab`:
```
192.168.1.100:/mnt/nfs/cctv_images /mnt/nfs/cctv_images nfs defaults,_netdev 0 0
```

3. **Copy Project Files**

```bash
# Copy script-server folder to worker node
scp -r script-server/ user@worker-node:/path/to/cctv/

# Copy worker compose file
scp docker-compose.worker.yml user@worker-node:/path/to/cctv/
scp .env.worker.example user@worker-node:/path/to/cctv/
```

4. **Configure Worker Environment**

```bash
cd /path/to/cctv
cp .env.worker.example .env.worker
nano .env.worker
```

**Worker 2 Configuration:**
```bash
MAIN_SERVER_IP=192.168.1.100
WORKER_ID=worker-2
WORKER_NAME=GPU Worker 2
CAMERA_START_ID=5001
CAMERA_END_ID=10000
EXAM_ID=19
WORKER_HTTP_PORT=8000
OLLAMA_PORT=11434
```

**Worker 3 Configuration:**
```bash
MAIN_SERVER_IP=192.168.1.100
WORKER_ID=worker-3
WORKER_NAME=GPU Worker 3
CAMERA_START_ID=10001
CAMERA_END_ID=15000
EXAM_ID=19
WORKER_HTTP_PORT=8000
OLLAMA_PORT=11434
```

**Worker 4 Configuration:**
```bash
MAIN_SERVER_IP=192.168.1.100
WORKER_ID=worker-4
WORKER_NAME=GPU Worker 4
CAMERA_START_ID=15001
CAMERA_END_ID=20000
EXAM_ID=19
WORKER_HTTP_PORT=8000
OLLAMA_PORT=11434
```

5. **Deploy Worker**

```bash
# Load environment
export $(cat .env.worker | xargs)

# Start worker services
docker-compose -f docker-compose.worker.yml up -d

# Verify
docker-compose -f docker-compose.worker.yml ps
docker logs cctv_script_server_worker_2
```

### Step 3: Verify Deployment

1. **Check NFS Connectivity**

From each worker:
```bash
# Test write
echo "test from worker-2" > /mnt/nfs/cctv_images/test_worker2.txt

# From main server, verify
cat /mnt/nfs/cctv_images/test_worker2.txt
```

2. **Check Database Connectivity**

From each worker:
```bash
docker exec -it cctv_script_server_worker_2 bash
mysql -h 192.168.1.100 -P 3307 -u root -prootpassword123 cctv_monitoring -e "SELECT COUNT(*) FROM camera_links_rtsp;"
```

3. **Check GPU Access**

From each worker:
```bash
docker exec -it cctv_script_server_worker_2 nvidia-smi
```

4. **Check Worker Health**

```bash
# From each worker
curl http://localhost:8000/health

# From main server
curl http://192.168.1.101:8000/health
curl http://192.168.1.102:8000/health
curl http://192.168.1.103:8000/health
```

## Camera Distribution Logic

### Automatic Distribution

The system automatically filters cameras based on worker configuration:

```python
# In script-server/app/camera_distributor.py
from app.camera_distributor import filter_cameras

# Get all cameras from database
all_cameras = get_rtsp_cameras(exam_id=19)

# Filter for this worker
my_cameras = filter_cameras(all_cameras)

# Process only assigned cameras
for camera in my_cameras:
    process_camera(camera)
```

### Manual Distribution Calculation

```bash
# Calculate ranges for your deployment
cd script-server
python -m app.camera_distributor

# Example output:
# ============================================================
# CAMERA DISTRIBUTION PLAN
# ============================================================
# Total Cameras: 20,000
# GPU Workers: 4
# Cameras per Worker: ~5,000
# ============================================================
# worker-1     | Cameras      1 -  5,000 | Count: 5,000
# worker-2     | Cameras  5,001 - 10,000 | Count: 5,000
# worker-3     | Cameras 10,001 - 15,000 | Count: 5,000
# worker-4     | Cameras 15,001 - 20,000 | Count: 5,000
# ============================================================
```

## Monitoring

### Worker Status

```bash
# Check worker status
curl http://192.168.1.101:8000/status

# Check running executions
curl http://192.168.1.101:8000/executions/running
```

### GPU Monitoring

```bash
# Start GPU monitor (optional)
docker-compose -f docker-compose.worker.yml --profile monitoring up -d

# View GPU stats
docker logs -f cctv_gpu_monitor_worker_2
```

### NFS Performance

```bash
# Check NFS stats on main server
nfsstat -s

# Check NFS stats on worker
nfsstat -c

# Monitor NFS I/O
iostat -x 5
```

### Database Load

```sql
-- Check active connections
SHOW PROCESSLIST;

-- Check connection count per host
SELECT SUBSTRING_INDEX(host, ':', 1) AS host_ip, COUNT(*) AS connections
FROM information_schema.processlist
GROUP BY host_ip;
```

## Performance Optimization

### NFS Tuning

**Main Server (`/etc/exports`):**
```
/mnt/nfs/cctv_images 192.168.1.0/24(rw,sync,no_subtree_check,no_root_squash,async)
```

**Worker Nodes (`/etc/fstab`):**
```
192.168.1.100:/mnt/nfs/cctv_images /mnt/nfs/cctv_images nfs rsize=8192,wsize=8192,timeo=14,intr,_netdev 0 0
```

### Database Connection Pooling

Already configured in `script-server/scripts/detection/utils/db_client.py`:
- Connection pooling enabled
- Automatic reconnection
- Query optimization

### GPU Memory Management

Configured in `docker-compose.worker.yml`:
```yaml
environment:
  PYTORCH_CUDA_ALLOC_CONF: max_split_size_mb:512
  TORCH_CUDNN_V8_API_ENABLED: 1
  NVIDIA_TF32_OVERRIDE: 1
```

## Troubleshooting

### Worker Cannot Connect to Database

```bash
# Check network connectivity
ping 192.168.1.100

# Check MySQL port
telnet 192.168.1.100 3307

# Check firewall
sudo ufw status
sudo ufw allow from 192.168.1.0/24 to any port 3307
```

### NFS Mount Issues

```bash
# Check NFS server status
sudo systemctl status nfs-kernel-server

# Check exports
sudo exportfs -v

# Remount on worker
sudo umount /mnt/nfs/cctv_images
sudo mount -t nfs 192.168.1.100:/mnt/nfs/cctv_images /mnt/nfs/cctv_images

# Check mount
df -h | grep nfs
```

### Worker Not Processing Cameras

```bash
# Check worker logs
docker logs cctv_script_server_worker_2

# Verify camera range configuration
docker exec cctv_script_server_worker_2 env | grep CAMERA

# Test camera filter
docker exec -it cctv_script_server_worker_2 python -m app.camera_distributor
```

### GPU Not Detected

```bash
# Check NVIDIA runtime
docker run --rm --gpus all nvidia/cuda:12.2.0-base-ubuntu22.04 nvidia-smi

# Check Docker daemon configuration
cat /etc/docker/daemon.json

# Should contain:
{
  "runtimes": {
    "nvidia": {
      "path": "nvidia-container-runtime",
      "runtimeArgs": []
    }
  }
}
```

## Scaling Beyond 4 Workers

### 10 Workers (100K cameras)

```bash
# Calculate distribution
python -m app.camera_distributor
# Input: 100000 cameras, 10 workers
# Output: 10,000 cameras per worker

# Deploy workers 5-10 following same process
# Update NFS exports for new worker IPs
# Configure camera ranges accordingly
```

### Load Balancing Considerations

- **Network**: 10Gbps recommended for 10+ workers
- **NFS**: Consider parallel NFS (pNFS) for better performance
- **Database**: Add read replicas for 10+ workers
- **Storage**: Plan for ~100GB per 10K cameras per day

## Backup and Recovery

### Database Backup

```bash
# On main server
docker exec cctv_mysql mysqldump -u root -prootpassword123 cctv_monitoring > backup.sql
```

### NFS Backup

```bash
# On main server
rsync -avz /mnt/nfs/cctv_images/ /backup/cctv_images/
```

### Worker Recovery

If a worker fails:
1. Other workers continue processing their cameras
2. Failed worker's cameras are not processed until recovery
3. Restart worker: `docker-compose -f docker-compose.worker.yml restart`

## Security Considerations

1. **Network Isolation**: Use VPN or private network for worker communication
2. **Database Access**: Use strong passwords, consider SSL connections
3. **NFS Security**: Use Kerberos authentication for production
4. **Firewall Rules**: Restrict access to MySQL (3307) and NFS (2049) ports

## Cost Optimization

### Cloud Deployment

- **Main Server**: Standard instance + managed MySQL
- **Workers**: GPU instances (spot/preemptible for cost savings)
- **Storage**: Object storage (S3/GCS) instead of NFS for large scale

### On-Premise

- **Main Server**: 1x server with RAID storage
- **Workers**: GPU workstations
- **Network**: 10Gbps switch for NFS performance

## Support

For issues or questions:
1. Check logs: `docker logs <container_name>`
2. Review this guide's troubleshooting section
3. Contact system administrator

---

**Last Updated**: 2026-04-29
**Version**: 1.0.0
