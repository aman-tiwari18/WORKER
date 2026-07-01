# Worker Node Deployment Guide

## Quick Setup

### Step 1: Copy script-server folder to worker node

```bash
# From main server, copy to worker
scp -r script-server/ user@worker-node-ip:/path/to/
```

### Step 2: On worker node, run setup script

```bash
cd /path/to/script-server

# Automated setup (recommended)
sudo ./setup_worker.sh <worker_id> <main_server_ip> <camera_start> <camera_end>

# Examples:
sudo ./setup_worker.sh 2 192.168.1.100 5001 10000   # Worker 2
sudo ./setup_worker.sh 3 192.168.1.100 10001 15000  # Worker 3
sudo ./setup_worker.sh 4 192.168.1.100 15001 20000  # Worker 4
```

That's it! The script will:
- Install NFS client
- Mount shared storage from main server
- Configure environment
- Start Docker containers

### Step 3: Verify

```bash
# Check services
docker-compose -f docker-compose.worker.yml ps

# Check health
curl http://localhost:8000/health

# Check logs
docker logs cctv_script_server_worker_2
```

---

## Manual Setup (if automated script fails)

### 1. Install NFS client

```bash
sudo apt-get update
sudo apt-get install -y nfs-common
```

### 2. Mount NFS

```bash
sudo mkdir -p /mnt/nfs/cctv_images
sudo mount -t nfs 192.168.1.100:/mnt/nfs/cctv_images /mnt/nfs/cctv_images

# Make permanent
echo "192.168.1.100:/mnt/nfs/cctv_images /mnt/nfs/cctv_images nfs defaults,_netdev 0 0" | sudo tee -a /etc/fstab
```

### 3. Configure environment

```bash
cp .env.worker.example .env.worker
nano .env.worker
```

Edit these values:
```bash
MAIN_SERVER_IP=192.168.1.100
WORKER_ID=worker-2
CAMERA_START_ID=5001
CAMERA_END_ID=10000
```

### 4. Start services

```bash
export $(cat .env.worker | xargs)
docker-compose -f docker-compose.worker.yml up -d
```

---

## Camera Distribution

**Example: 20,000 cameras across 4 GPU systems**

| System | Worker ID | Camera Range | Count |
|--------|-----------|--------------|-------|
| Main Server | main | 1 - 5,000 | 5,000 |
| Worker 2 | worker-2 | 5,001 - 10,000 | 5,000 |
| Worker 3 | worker-3 | 10,001 - 15,000 | 5,000 |
| Worker 4 | worker-4 | 15,001 - 20,000 | 5,000 |

---

## Troubleshooting

### Can't connect to MySQL
```bash
# Test connection
telnet 192.168.1.100 3307

# Check firewall on main server
sudo ufw allow from worker-ip to any port 3307
```

### NFS mount fails
```bash
# Check NFS server on main server
sudo systemctl status nfs-kernel-server
sudo exportfs -v

# Remount
sudo umount /mnt/nfs/cctv_images
sudo mount -t nfs 192.168.1.100:/mnt/nfs/cctv_images /mnt/nfs/cctv_images
```

### GPU not detected
```bash
# Test NVIDIA runtime
docker run --rm --gpus all nvidia/cuda:12.2.0-base-ubuntu22.04 nvidia-smi
```

---

## Monitoring

```bash
# Worker status
curl http://localhost:8000/status

# GPU usage
nvidia-smi

# Container logs
docker logs -f cctv_script_server_worker_2

# NFS mount
df -h | grep nfs
```
