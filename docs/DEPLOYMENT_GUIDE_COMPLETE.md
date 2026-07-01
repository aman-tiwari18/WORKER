# Complete Deployment Guide - Horizontal Scaling
## 25,000 Cameras Across 4 GPU Nodes

---

## 📋 Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                    Main Server (192.168.19.30)                  │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────┐  │
│  │ cctv_backend │  │ cctv_frontend│  │  script-server       │  │
│  │   (Node.js)  │  │    (React)   │  │  (FastAPI + PM2)     │  │
│  │   Port 3000  │  │   Port 5173  │  │  Port 8000           │  │
│  └──────────────┘  └──────────────┘  └──────────────────────┘  │
│  ┌──────────────┐  ┌──────────────────────────────────────────┐│
│  │    MySQL     │  │         NFS Server                       ││
│  │  Port 3306   │  │  /mnt/nfs/cctv_images                    ││
│  └──────────────┘  └──────────────────────────────────────────┘│
│                                                                  │
│  🎥 Worker 0: Cameras 1 - 6,250                                 │
└─────────────────────────────────────────────────────────────────┘
                              │
                              │ Network
        ┌─────────────────────┼─────────────────────┐
        │                     │                     │
┌───────▼────────┐   ┌────────▼────────┐   ┌───────▼────────┐
│  Worker 1      │   │  Worker 2       │   │  Worker 3      │
│  192.168.19.43 │   │  192.168.19.42  │   │  192.168.19.41 │
│  ┌──────────┐  │   │  ┌──────────┐   │   │  ┌──────────┐  │
│  │ script-  │  │   │  │ script-  │   │   │  │ script-  │  │
│  │ server   │  │   │  │ server   │   │   │  │ server   │  │
│  │ (Docker) │  │   │  │ (Docker) │   │   │  │ (Docker) │  │
│  └──────────┘  │   │  └──────────┘   │   │  └──────────┘  │
│  ┌──────────┐  │   │  ┌──────────┐   │   │  ┌──────────┐  │
│  │  Ollama  │  │   │  │  Ollama  │   │   │  │  Ollama  │  │
│  └──────────┘  │   │  └──────────┘   │   │  └──────────┘  │
│  🎥 Cameras    │   │  🎥 Cameras     │   │  🎥 Cameras    │
│  6,251-12,500  │   │  12,501-18,750  │   │  18,751-25,000 │
└────────────────┘   └─────────────────┘   └────────────────┘
```

---

## 🎯 Camera Distribution

| Node | IP | Worker ID | Camera Range | Count |
|------|------------|-----------|--------------|-------|
| **Main Server** | 192.168.19.30 | worker-0 | 1 - 6,250 | 6,250 |
| **Worker 1** | 192.168.19.43 | worker-1 | 6,251 - 12,500 | 6,250 |
| **Worker 2** | 192.168.19.42 | worker-2 | 12,501 - 18,750 | 6,250 |
| **Worker 3** | 192.168.19.41 | worker-3 | 18,751 - 25,000 | 6,250 |

---

## 📦 Deployment Steps

### Step 1: Setup Main Server (192.168.19.30)

#### 1.1 Push Code to Main Server
```bash
# On your local machine
cd /home/aman/Desktop/cctv
git add .
git commit -m "Horizontal scaling setup"
git push origin main

# On main server
ssh user@192.168.19.30
cd /data/cctv_dev/cctv
git pull origin main
```

#### 1.2 Run Main Server Setup Script
```bash
cd /data/cctv_dev/cctv
sudo chmod +x setup_main_server.sh
sudo ./setup_main_server.sh
```

**This script will:**
- ✅ Install NFS server
- ✅ Create `/mnt/nfs/cctv_images` shared directory
- ✅ Configure NFS exports for all workers
- ✅ Configure MySQL for remote access (bind-address=0.0.0.0)
- ✅ Configure firewall (MySQL 3306, NFS 2049, 111)
- ✅ Create symlink: `script-server/scripts/detection/results` → `/mnt/nfs/cctv_images`
- ✅ Set worker configuration in `script-server/.env`

#### 1.3 Grant MySQL Remote Access
```bash
mysql -u root -p
```

```sql
-- Create user for workers (MySQL 8.0 syntax)
CREATE USER IF NOT EXISTS 'root'@'192.168.19.%' IDENTIFIED BY 'rootpassword123';

-- Grant privileges
GRANT ALL PRIVILEGES ON cctv_monitoring.* TO 'root'@'192.168.19.%';

-- Apply changes
FLUSH PRIVILEGES;

-- Verify
SELECT user, host FROM mysql.user WHERE user='root';

-- Exit
EXIT;
```

#### 1.4 Verify Main Server Setup
```bash
# Check NFS server
sudo systemctl status nfs-kernel-server
sudo exportfs -v

# Check MySQL
sudo systemctl status mysql
sudo netstat -tlnp | grep 3306

# Test NFS write
echo "test" > /mnt/nfs/cctv_images/test.txt
cat /mnt/nfs/cctv_images/test.txt
rm /mnt/nfs/cctv_images/test.txt

# Check symlink
ls -la script-server/scripts/detection/results
```

#### 1.5 Start Main Server with PM2
```bash
cd /data/cctv_dev/cctv

# Install dependencies if needed
cd cctv_backend && npm install && cd ..
cd script-server && source venv/bin/activate && pip install -r requirements.txt && cd ..

# Start with PM2
pm2 start ecosystem.config.js
pm2 save
pm2 startup  # Follow instructions to enable auto-start

# Verify
pm2 status
pm2 logs cctv-backend-fastapi --lines 50
curl http://localhost:8000/health
```

---

### Step 2: Setup Worker Nodes

#### 2.1 Copy Files to Worker Nodes
```bash
# From main server, copy script-server folder to each worker
# Worker 1 (192.168.19.43)
scp -r /data/cctv_dev/cctv/script-server user@192.168.19.43:/data/cctv_dev/

# Worker 2 (192.168.19.42)
scp -r /data/cctv_dev/cctv/script-server user@192.168.19.42:/data/cctv_dev/

# Worker 3 (192.168.19.41)
scp -r /data/cctv_dev/cctv/script-server user@192.168.19.41:/data/cctv_dev/
```

#### 2.2 Setup Worker 1 (192.168.19.43)
```bash
ssh user@192.168.19.43
cd /data/cctv_dev/script-server

# Run setup script
sudo chmod +x setup_worker.sh
sudo ./setup_worker.sh 1 192.168.19.30 6251 12500
```

#### 2.3 Setup Worker 2 (192.168.19.42)
```bash
ssh user@192.168.19.42
cd /data/cctv_dev/script-server

# Run setup script
sudo chmod +x setup_worker.sh
sudo ./setup_worker.sh 2 192.168.19.30 12501 18750
```

#### 2.4 Setup Worker 3 (192.168.19.41)
```bash
ssh user@192.168.19.41
cd /data/cctv_dev/script-server

# Run setup script
sudo chmod +x setup_worker.sh
sudo ./setup_worker.sh 3 192.168.19.30 18751 25000
```

**Each worker setup script will:**
- ✅ Install NFS client
- ✅ Mount NFS from main server
- ✅ Test database connectivity
- ✅ Create `.env.worker` with worker-specific configuration
- ✅ Deploy Docker containers (script-server + Ollama)
- ✅ Verify GPU access

---

### Step 3: Verification

#### 3.1 Verify Main Server (Worker 0)
```bash
# On main server (192.168.19.30)
cd /data/cctv_dev/cctv

# Check PM2 status
pm2 status

# Check script-server health
curl http://localhost:8000/health

# Check worker info
curl http://localhost:8000/status

# Check logs
pm2 logs cctv-backend-fastapi --lines 50

# Verify camera filtering
tail -f script-server/logs/app.log
```

#### 3.2 Verify Worker 1
```bash
# On worker 1 (192.168.19.43)
cd /data/cctv_dev/script-server

# Check containers
docker-compose -f docker-compose.worker.yml ps

# Check health
curl http://localhost:8000/health

# Check logs
docker logs cctv_script_server_worker_1 --tail 100

# Check GPU
docker exec cctv_script_server_worker_1 nvidia-smi

# Check NFS mount
df -h | grep nfs
ls -la /mnt/nfs/cctv_images/
```

#### 3.3 Verify Worker 2
```bash
# On worker 2 (192.168.19.42)
cd /data/cctv_dev/script-server

# Same checks as Worker 1
docker-compose -f docker-compose.worker.yml ps
curl http://localhost:8000/health
docker logs cctv_script_server_worker_2 --tail 100
docker exec cctv_script_server_worker_2 nvidia-smi
```

#### 3.4 Verify Worker 3
```bash
# On worker 3 (192.168.19.41)
cd /data/cctv_dev/script-server

# Same checks as Worker 1
docker-compose -f docker-compose.worker.yml ps
curl http://localhost:8000/health
docker logs cctv_script_server_worker_3 --tail 100
docker exec cctv_script_server_worker_3 nvidia-smi
```

---

## 🔍 Testing Camera Distribution

### Test Camera Assignment
```bash
# On main server
cd /data/cctv_dev/cctv/script-server
source venv/bin/activate
python3 << EOF
import os
os.environ['WORKER_ID'] = 'worker-0'
os.environ['CAMERA_START_ID'] = '1'
os.environ['CAMERA_END_ID'] = '6250'
os.environ['DISTRIBUTION_MODE'] = 'static'

from app.camera_distributor import filter_cameras

# Test cameras
test_cameras = [
    {'id': 100, 'ch_code': '12345'},      # Should be in worker-0
    {'id': 7000, 'ch_code': '67890'},     # Should be in worker-1
    {'id': 15000, 'ch_code': '11111'},    # Should be in worker-2
    {'id': 20000, 'ch_code': '22222'},    # Should be in worker-3
]

filtered = filter_cameras(test_cameras)
print(f"Worker 0 assigned: {len(filtered)} cameras")
for cam in filtered:
    print(f"  Camera ID: {cam['id']}")
EOF
```

### Test Detection Image Saving
```bash
# On main server - trigger a detection
# Images should save to /mnt/nfs/cctv_images/

# Check if images appear
ls -lh /mnt/nfs/cctv_images/
ls -lh /mnt/nfs/cctv_images/tcs_detections/

# On workers - verify they can see the same images
# Worker 1
ssh user@192.168.19.43 "ls -lh /mnt/nfs/cctv_images/"

# Worker 2
ssh user@192.168.19.42 "ls -lh /mnt/nfs/cctv_images/"

# Worker 3
ssh user@192.168.19.41 "ls -lh /mnt/nfs/cctv_images/"
```

---

## 🔧 Troubleshooting

### Issue: MySQL Connection Refused
```bash
# On main server
sudo netstat -tlnp | grep 3306
# Should show: 0.0.0.0:3306

# Check MySQL config
sudo cat /etc/mysql/mysql.conf.d/mysqld.cnf | grep bind-address
# Should show: bind-address = 0.0.0.0

# Restart MySQL
sudo systemctl restart mysql

# Test from worker
mysql -h 192.168.19.30 -u root -prootpassword123 -e "SELECT 1"
```

### Issue: NFS Mount Failed
```bash
# On main server - check NFS exports
sudo exportfs -v
sudo systemctl status nfs-kernel-server

# On worker - test NFS connectivity
showmount -e 192.168.19.30

# Manual mount
sudo mount -t nfs 192.168.19.30:/mnt/nfs/cctv_images /mnt/nfs/cctv_images

# Check mount
df -h | grep nfs
```

### Issue: Worker Not Processing Cameras
```bash
# Check worker logs
docker logs cctv_script_server_worker_1 --tail 200

# Check environment variables
docker exec cctv_script_server_worker_1 env | grep WORKER
docker exec cctv_script_server_worker_1 env | grep CAMERA

# Restart worker
docker-compose -f docker-compose.worker.yml restart
```

### Issue: GPU Not Detected
```bash
# Check NVIDIA driver on host
nvidia-smi

# Check Docker GPU access
docker run --rm --gpus all nvidia/cuda:12.2.0-base-ubuntu22.04 nvidia-smi

# Check container GPU access
docker exec cctv_script_server_worker_1 nvidia-smi
```

---

## 📊 Monitoring

### Check All Nodes Status
```bash
# From main server
echo "=== Main Server (Worker 0) ==="
curl -s http://localhost:8000/status | jq

echo "=== Worker 1 ==="
curl -s http://192.168.19.43:8000/status | jq

echo "=== Worker 2 ==="
curl -s http://192.168.19.42:8000/status | jq

echo "=== Worker 3 ==="
curl -s http://192.168.19.41:8000/status | jq
```

### Monitor NFS Storage
```bash
# On main server
df -h /mnt/nfs/cctv_images
du -sh /mnt/nfs/cctv_images/*
```

### Monitor GPU Usage
```bash
# Main server
watch -n 1 nvidia-smi

# Workers
ssh user@192.168.19.43 "watch -n 1 nvidia-smi"
```

---

## 🎯 Next Steps

1. ✅ **Main server configured** as Worker 0 (cameras 1-6,250)
2. ✅ **NFS shared storage** for detection images
3. ✅ **MySQL remote access** for all workers
4. ✅ **Camera distribution** logic integrated
5. ✅ **Worker deployment** scripts ready

**You're ready to deploy!** Follow the steps above to set up your 4-node cluster.

---

## 📝 Configuration Summary

### Main Server (.env)
```env
WORKER_ID=worker-0
WORKER_NAME=Main Server (Worker 0)
DISTRIBUTION_MODE=static
CAMERA_START_ID=1
CAMERA_END_ID=6250
```

### Worker 1 (.env.worker)
```env
WORKER_ID=worker-1
WORKER_NAME=GPU Worker 1
CAMERA_START_ID=6251
CAMERA_END_ID=12500
MAIN_SERVER_IP=192.168.19.30
```

### Worker 2 (.env.worker)
```env
WORKER_ID=worker-2
WORKER_NAME=GPU Worker 2
CAMERA_START_ID=12501
CAMERA_END_ID=18750
MAIN_SERVER_IP=192.168.19.30
```

### Worker 3 (.env.worker)
```env
WORKER_ID=worker-3
WORKER_NAME=GPU Worker 3
CAMERA_START_ID=18751
CAMERA_END_ID=25000
MAIN_SERVER_IP=192.168.19.30
```

---

## 🚀 Quick Commands Reference

```bash
# Main Server
pm2 status
pm2 logs cctv-backend-fastapi
pm2 restart all
curl http://localhost:8000/health

# Workers
docker-compose -f docker-compose.worker.yml ps
docker-compose -f docker-compose.worker.yml logs -f
docker-compose -f docker-compose.worker.yml restart
curl http://localhost:8000/health

# NFS
sudo exportfs -v                    # Main server
df -h | grep nfs                    # All nodes
ls -lh /mnt/nfs/cctv_images/       # All nodes

# MySQL
mysql -u root -p                    # Main server
mysql -h 192.168.19.30 -u root -p  # Workers
```
