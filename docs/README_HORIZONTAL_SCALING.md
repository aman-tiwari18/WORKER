# Horizontal Scaling - Complete Setup

## Overview

This system scales horizontally across multiple GPU nodes to handle 20K+ cameras.

**Architecture:**
- **Main Server**: PM2 (Backend + Frontend + MySQL + NFS + Script-Server)
- **Worker Nodes**: Docker (Script-Server only)
- **Shared Storage**: NFS
- **Central Database**: MySQL on main server

---

## Quick Start

### 1. Push Code to VM

```bash
git push origin main
# or
scp -r /path/to/cctv user@vm-ip:/path/to/
```

### 2. Setup Main Server (VM)

```bash
ssh user@vm-ip
cd /path/to/cctv

# Make executable
chmod +x setup_main_server.sh

# Run setup
sudo ./setup_main_server.sh

# Grant MySQL access
mysql -u root -p
GRANT ALL PRIVILEGES ON cctv_monitoring.* TO 'root'@'%' IDENTIFIED BY 'rootpassword123';
FLUSH PRIVILEGES;
EXIT;

# Start PM2
pm2 start ecosystem.config.js
pm2 save
```

### 3. Setup Workers (Each GPU System)

```bash
# Copy script-server folder
scp -r /path/to/cctv/script-server user@worker-ip:/home/user/

# SSH and run setup
ssh user@worker-ip
cd /home/user/script-server
sudo ./setup_worker.sh 2 <MAIN_SERVER_IP> 5001 10000
```

---

## Files Guide

### Setup Scripts
- **`setup_main_server.sh`** - Automated main server setup (NFS + MySQL + env)
- **`script-server/setup_worker.sh`** - Automated worker setup (NFS mount + Docker)

### Documentation
- **`MAIN_SERVER_SETUP_INSTRUCTIONS.md`** - Detailed main server setup
- **`DEPLOYMENT_CHECKLIST.md`** - Step-by-step deployment checklist
- **`QUICK_COMMANDS.txt`** - Quick command reference
- **`PM2_DEPLOYMENT_GUIDE.md`** - PM2-specific guide
- **`ARCHITECTURE.txt`** - Visual architecture diagram

### Worker Files (in script-server/)
- **`docker-compose.worker.yml`** - Worker Docker compose
- **`.env.worker.example`** - Worker environment template
- **`WORKER_DEPLOYMENT.md`** - Worker deployment guide

### Testing
- **`test_scaling.py`** - Test camera distribution logic

---

## Camera Distribution

**Example: 20,000 cameras across 4 GPU systems**

| System | Cameras | Setup Command |
|--------|---------|---------------|
| Main Server | 1-5,000 | `pm2 start ecosystem.config.js` |
| Worker 2 | 5,001-10,000 | `sudo ./setup_worker.sh 2 <ip> 5001 10000` |
| Worker 3 | 10,001-15,000 | `sudo ./setup_worker.sh 3 <ip> 10001 15000` |
| Worker 4 | 15,001-20,000 | `sudo ./setup_worker.sh 4 <ip> 15001 20000` |

---

## How It Works

1. **Main server** runs with PM2 (Backend + Frontend + MySQL + NFS)
2. **Workers** run in Docker (Script-Server only)
3. **All workers** connect to main server's MySQL (port 3306)
4. **All workers** mount NFS from main server
5. **Camera distributor** automatically filters cameras by range
6. **Each worker** processes only its assigned cameras
7. **Images** saved to shared NFS storage
8. **Results** written to central MySQL database
9. **Frontend** displays results from all workers

---

## Verification

### Main Server
```bash
pm2 status                              # PM2 services
sudo systemctl status nfs-kernel-server # NFS server
sudo exportfs -v                        # NFS exports
curl http://localhost:8000/health       # Script-server health
```

### Workers
```bash
docker-compose -f docker-compose.worker.yml ps  # Containers
df -h | grep nfs                                # NFS mount
curl http://localhost:8000/health               # Health
docker exec cctv_script_server_worker_2 nvidia-smi  # GPU
```

---

## Troubleshooting

### Main Server

**NFS not working:**
```bash
sudo systemctl restart nfs-kernel-server
sudo exportfs -ra
```

**MySQL not accessible:**
```bash
sudo systemctl restart mysql
mysql -h 127.0.0.1 -u root -p -e "SELECT 1"
```

**PM2 not starting:**
```bash
pm2 logs
pm2 restart all
```

### Workers

**Can't connect to MySQL:**
```bash
telnet <main-ip> 3306
```

**NFS mount fails:**
```bash
showmount -e <main-ip>
```

**GPU not detected:**
```bash
docker run --rm --gpus all nvidia/cuda:12.2.0-base-ubuntu22.04 nvidia-smi
```

---

## Support

- **Main Server Setup**: `MAIN_SERVER_SETUP_INSTRUCTIONS.md`
- **Worker Setup**: `script-server/WORKER_DEPLOYMENT.md`
- **Deployment Checklist**: `DEPLOYMENT_CHECKLIST.md`
- **Quick Commands**: `QUICK_COMMANDS.txt`

---

## Summary

**After pushing to VM:**

1. Run `sudo ./setup_main_server.sh` on main server
2. Grant MySQL access
3. Start with `pm2 start ecosystem.config.js`
4. Copy `script-server/` to each worker
5. Run `sudo ./setup_worker.sh` on each worker
6. Done!

**System automatically:**
- Distributes cameras by range
- Saves images to shared storage
- Writes results to central database
- Shows everything in frontend

---

**Status**: Ready for deployment ✓
