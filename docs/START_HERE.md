# START HERE - Horizontal Scaling Setup

## Your Situation

✓ Main server running with **PM2** (not Docker)  
✓ Want to add GPU worker nodes  
✓ Need to scale to 20K+ cameras  

---

## What You Need To Do

### MAIN SERVER (Your current system - 5 minutes)

**Read this file**: `PM2_DEPLOYMENT_GUIDE.md`

**Quick commands**:
```bash
# 1. Install NFS
sudo apt-get install -y nfs-kernel-server
sudo mkdir -p /mnt/nfs/cctv_images
sudo chmod 777 /mnt/nfs/cctv_images
echo "/mnt/nfs/cctv_images *(rw,sync,no_subtree_check)" | sudo tee -a /etc/exports
sudo exportfs -ra
sudo systemctl restart nfs-kernel-server

# 2. Allow MySQL remote access
sudo ufw allow from 192.168.1.0/24 to any port 3306

# 3. Update script-server/.env
echo "WORKER_ID=main" >> script-server/.env
echo "WORKER_NAME=Main Server" >> script-server/.env
echo "DISTRIBUTION_MODE=single" >> script-server/.env

# 4. Restart PM2
pm2 restart cctv-backend-fastapi
pm2 save
```

---

### WORKER NODES (Each GPU system - 2 minutes)

**On each worker**:

```bash
# 1. Copy script-server folder from main server
scp -r user@main-server:/path/to/cctv/script-server /home/user/

# 2. Run ONE command
cd /home/user/script-server
sudo ./setup_worker.sh <worker_id> <main_ip> <cam_start> <cam_end>

# Examples:
sudo ./setup_worker.sh 2 192.168.1.100 5001 10000   # Worker 2
sudo ./setup_worker.sh 3 192.168.1.100 10001 15000  # Worker 3
sudo ./setup_worker.sh 4 192.168.1.100 15001 20000  # Worker 4
```

---

## Camera Distribution

**20,000 cameras across 4 systems:**

| System | Cameras | Setup Command |
|--------|---------|---------------|
| Main (PM2) | 1-5,000 | Already running |
| Worker 2 | 5,001-10,000 | `sudo ./setup_worker.sh 2 <ip> 5001 10000` |
| Worker 3 | 10,001-15,000 | `sudo ./setup_worker.sh 3 <ip> 10001 15000` |
| Worker 4 | 15,001-20,000 | `sudo ./setup_worker.sh 4 <ip> 15001 20000` |

---

## How It Works

1. **Main server** keeps running with PM2 (no changes)
2. **Workers** run in Docker (easier deployment)
3. **All workers** connect to main server's MySQL (port 3306)
4. **All workers** save images to shared NFS storage
5. **Camera distributor** automatically filters cameras by range
6. **Frontend** shows results from all workers

---

## Files To Read

1. **`PM2_DEPLOYMENT_GUIDE.md`** ← Main server setup (PM2)
2. **`script-server/WORKER_DEPLOYMENT.md`** ← Worker setup
3. **`ARCHITECTURE.txt`** ← Visual diagram

---

## Verify Setup

**Main server**:
```bash
sudo exportfs -v  # Check NFS
curl http://localhost:8000/health  # Check script-server
```

**Each worker**:
```bash
curl http://localhost:8000/health  # Should show worker_id
docker logs cctv_script_server_worker_2
```

---

## Need Help?

- Main server setup: `PM2_DEPLOYMENT_GUIDE.md`
- Worker setup: `script-server/WORKER_DEPLOYMENT.md`
- Troubleshooting: Check logs with `pm2 logs` or `docker logs`

---

**That's it!** Your system will scale horizontally across multiple GPUs.
