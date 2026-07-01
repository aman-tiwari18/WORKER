# What To Do Next - Step by Step

## Current Status ✓
- Horizontal scaling implementation complete
- Camera distribution logic integrated
- Worker deployment files ready
- Everything tested and working

---

## Your Deployment Process

### STEP 1: Main Server (Your current system)

**Nothing to change!** Just start normally:

```bash
cd /path/to/cctv
docker-compose up -d
```

**But BEFORE deploying workers, setup NFS:**

```bash
# Install NFS server
sudo apt-get install -y nfs-kernel-server

# Create shared directory
sudo mkdir -p /mnt/nfs/cctv_images
sudo chmod 777 /mnt/nfs/cctv_images

# Configure exports (replace with your actual worker IPs)
echo "/mnt/nfs/cctv_images 192.168.1.101(rw,sync,no_subtree_check)" | sudo tee -a /etc/exports
echo "/mnt/nfs/cctv_images 192.168.1.102(rw,sync,no_subtree_check)" | sudo tee -a /etc/exports
echo "/mnt/nfs/cctv_images 192.168.1.103(rw,sync,no_subtree_check)" | sudo tee -a /etc/exports

# Start NFS
sudo exportfs -ra
sudo systemctl restart nfs-kernel-server

# Allow MySQL remote access
sudo ufw allow from 192.168.1.0/24 to any port 3307
```

---

### STEP 2: Worker Nodes (Your other GPU systems)

**On each GPU system, do this:**

#### A. Copy script-server folder

From main server, copy to worker:
```bash
# Method 1: SCP
scp -r /path/to/cctv/script-server user@worker-ip:/home/user/

# Method 2: USB drive
# Copy script-server folder to USB, then copy to worker

# Method 3: Git (if using version control)
git clone <repo> && cd cctv/script-server
```

#### B. Run ONE command

```bash
cd /home/user/script-server

# Worker 2 (handles cameras 5,001-10,000)
sudo ./setup_worker.sh 2 192.168.1.100 5001 10000

# Worker 3 (handles cameras 10,001-15,000)
sudo ./setup_worker.sh 3 192.168.1.100 10001 15000

# Worker 4 (handles cameras 15,001-20,000)
sudo ./setup_worker.sh 4 192.168.1.100 15001 20000
```

**Replace `192.168.1.100` with your main server's actual IP!**

#### C. Verify it's working

```bash
# Check health
curl http://localhost:8000/health

# Should show:
# {
#   "success": true,
#   "worker": {
#     "worker_id": "worker-2",
#     "camera_range": {"start": 5001, "end": 10000}
#   }
# }

# Check logs
docker logs cctv_script_server_worker_2
```

---

## What Happens Automatically

1. **Camera Distribution**: Each worker automatically processes only its assigned camera range
2. **Shared Storage**: All detection images saved to NFS (accessible from main server)
3. **Central Database**: All workers write results to main server's MySQL
4. **Frontend**: Shows results from ALL workers automatically

---

## File Structure

```
Main Server:
/path/to/cctv/
├── cctv_backend/
├── cctv_frontend/
├── script-server/          ← This folder
│   ├── docker-compose.worker.yml
│   ├── .env.worker.example
│   ├── setup_worker.sh
│   └── ... (all other files)
├── docker-compose.yml
└── ...

Worker Nodes:
/home/user/
└── script-server/          ← Just copy this folder
    ├── docker-compose.worker.yml
    ├── setup_worker.sh
    └── ... (all files)
```

---

## Camera Distribution Example

**If you have 20,000 cameras and 4 GPU systems:**

| System | Cameras | Command |
|--------|---------|---------|
| Main Server | 1 - 5,000 | `docker-compose up -d` (already running) |
| Worker 2 | 5,001 - 10,000 | `sudo ./setup_worker.sh 2 <main_ip> 5001 10000` |
| Worker 3 | 10,001 - 15,000 | `sudo ./setup_worker.sh 3 <main_ip> 10001 15000` |
| Worker 4 | 15,001 - 20,000 | `sudo ./setup_worker.sh 4 <main_ip> 15001 20000` |

**If you have 100,000 cameras and 10 GPU systems:**

Calculate ranges: 100,000 / 10 = 10,000 cameras per worker

```bash
# Main: 1-10,000 (already running)
# Worker 2:
sudo ./setup_worker.sh 2 <main_ip> 10001 20000
# Worker 3:
sudo ./setup_worker.sh 3 <main_ip> 20001 30000
# ... and so on
```

---

## Testing

After deploying all workers, test the distribution:

```bash
# On main server
cd script-server
python3 test_scaling.py
```

---

## Common Issues

### "Can't connect to MySQL"
```bash
# On main server, allow worker IPs
sudo ufw allow from <worker-ip> to any port 3307
```

### "NFS mount failed"
```bash
# On main server, check NFS is running
sudo systemctl status nfs-kernel-server
sudo exportfs -v

# On worker, check network
ping <main-server-ip>
showmount -e <main-server-ip>
```

### "GPU not detected"
```bash
# On worker, test NVIDIA runtime
docker run --rm --gpus all nvidia/cuda:12.2.0-base-ubuntu22.04 nvidia-smi
```

---

## Summary

**You need to:**
1. Setup NFS on main server (one time)
2. Copy `script-server` folder to each worker
3. Run `setup_worker.sh` on each worker (one command)
4. Done!

**The system will automatically:**
- Distribute cameras by range
- Save images to shared storage
- Write results to central database
- Show everything in frontend

---

## Need Help?

Check these files:
- `DEPLOYMENT_STEPS.md` - Detailed deployment guide
- `QUICK_START.txt` - Quick command reference
- `script-server/WORKER_DEPLOYMENT.md` - Worker-specific guide
