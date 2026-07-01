# Deployment Steps - Horizontal Scaling

## Main Server Setup (Do this FIRST)

### 1. Install NFS Server
```bash
sudo apt-get update
sudo apt-get install -y nfs-kernel-server

# Create shared directory
sudo mkdir -p /mnt/nfs/cctv_images
sudo chmod 777 /mnt/nfs/cctv_images

# Configure exports (add your worker IPs)
sudo nano /etc/exports
```

Add these lines (replace with your worker IPs):
```
/mnt/nfs/cctv_images 192.168.1.101(rw,sync,no_subtree_check)
/mnt/nfs/cctv_images 192.168.1.102(rw,sync,no_subtree_check)
/mnt/nfs/cctv_images 192.168.1.103(rw,sync,no_subtree_check)
```

```bash
# Apply and start
sudo exportfs -ra
sudo systemctl restart nfs-kernel-server
sudo systemctl enable nfs-kernel-server

# Verify
sudo exportfs -v
```

### 2. Configure MySQL for Remote Access
```bash
# Allow remote connections
sudo ufw allow from 192.168.1.0/24 to any port 3307

# Or disable firewall temporarily for testing
sudo ufw disable
```

### 3. Start Main Server
```bash
cd /path/to/cctv
docker-compose up -d

# Verify all services running
docker-compose ps

# Check MySQL accessible
docker exec -it cctv_mysql mysql -u root -prootpassword123 -e "SHOW DATABASES;"
```

---

## Worker Node Setup (Do this for EACH worker)

### Prerequisites on Each Worker
- Docker installed
- Docker Compose installed
- NVIDIA Container Toolkit installed
- Network access to main server

### Deployment Steps

**Worker 2 (Cameras 5,001-10,000):**
```bash
# 1. Copy script-server folder from main server
scp -r user@main-server:/path/to/cctv/script-server /home/user/

# 2. Go to script-server folder
cd /home/user/script-server

# 3. Run automated setup
sudo ./setup_worker.sh 2 192.168.1.100 5001 10000

# 4. Verify
curl http://localhost:8000/health
docker logs cctv_script_server_worker_2
```

**Worker 3 (Cameras 10,001-15,000):**
```bash
cd /home/user/script-server
sudo ./setup_worker.sh 3 192.168.1.100 10001 15000
curl http://localhost:8000/health
```

**Worker 4 (Cameras 15,001-20,000):**
```bash
cd /home/user/script-server
sudo ./setup_worker.sh 4 192.168.1.100 15001 20000
curl http://localhost:8000/health
```

---

## Verification Checklist

### Main Server
- [ ] NFS server running: `sudo systemctl status nfs-kernel-server`
- [ ] NFS exports configured: `sudo exportfs -v`
- [ ] MySQL accessible: `docker exec cctv_mysql mysql -u root -prootpassword123 -e "SELECT 1"`
- [ ] All services up: `docker-compose ps`
- [ ] Backend API responding: `curl http://localhost:3000/api/health`
- [ ] Frontend accessible: `curl http://localhost:3001`

### Each Worker Node
- [ ] NFS mounted: `df -h | grep nfs`
- [ ] NFS writable: `echo test > /mnt/nfs/cctv_images/test.txt`
- [ ] MySQL accessible: `docker exec cctv_script_server_worker_2 mysql -h 192.168.1.100 -P 3307 -u root -prootpassword123 -e "SELECT 1"`
- [ ] GPU detected: `docker exec cctv_script_server_worker_2 nvidia-smi`
- [ ] Services running: `docker-compose -f docker-compose.worker.yml ps`
- [ ] Health endpoint: `curl http://localhost:8000/health`
- [ ] Worker info correct: `curl http://localhost:8000/health | grep worker_id`

---

## Quick Commands Reference

### Main Server
```bash
# View logs
docker-compose logs -f

# Restart services
docker-compose restart

# Check NFS connections
sudo nfsstat -s
```

### Worker Nodes
```bash
# View logs
docker logs -f cctv_script_server_worker_2

# Restart worker
docker-compose -f docker-compose.worker.yml restart

# Check GPU
nvidia-smi

# Check NFS
df -h | grep nfs
```

---

## Camera Distribution Summary

**Total: 20,000 cameras across 4 systems**

```
Main Server:  Cameras     1 -  5,000  (5,000 cameras)
Worker 2:     Cameras 5,001 - 10,000  (5,000 cameras)
Worker 3:     Cameras 10,001 - 15,000 (5,000 cameras)
Worker 4:     Cameras 15,001 - 20,000 (5,000 cameras)
```

Each worker automatically processes only its assigned camera range.

---

## What Happens When You Start Processing

1. User starts exam from frontend
2. Backend creates assignments in database
3. **All workers** poll the database for assignments
4. Each worker fetches cameras from database
5. **Camera distributor automatically filters** cameras by worker range
6. Worker processes only its assigned cameras
7. Detection images saved to **shared NFS storage**
8. Results written to **central MySQL database**
9. Frontend displays all results from all workers

---

## Network Architecture

```
                    Main Server (192.168.1.100)
                    ┌─────────────────────────┐
                    │ MySQL (port 3307)       │◄─────┐
                    │ NFS (/mnt/nfs/...)      │◄───┐ │
                    │ Backend API             │    │ │
                    │ Frontend                │    │ │
                    │ Script-Server (1-5K)    │    │ │
                    └─────────────────────────┘    │ │
                                                   │ │
        ┌──────────────────────────────────────────┘ │
        │                                            │
        │                                            │
Worker 2 (192.168.1.101)          Worker 3 (192.168.1.102)
┌─────────────────────┐           ┌─────────────────────┐
│ Script-Server       │           │ Script-Server       │
│ (5K-10K cameras)    │           │ (10K-15K cameras)   │
│ NFS Client          │           │ NFS Client          │
└─────────────────────┘           └─────────────────────┘
```

---

## Support

If something doesn't work:
1. Check logs: `docker logs <container_name>`
2. Verify network connectivity: `ping`, `telnet`
3. Check NFS mount: `df -h | grep nfs`
4. Verify GPU: `nvidia-smi`
5. Review this checklist again
