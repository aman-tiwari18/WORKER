# Deployment Checklist

## Phase 1: Push Code to VM

- [ ] Push code to VM: `git push` or `scp -r`
- [ ] SSH into VM: `ssh user@vm-ip`
- [ ] Navigate to project: `cd /path/to/cctv`

---

## Phase 2: Main Server Setup (VM)

### Make Scripts Executable
```bash
chmod +x setup_main_server.sh
chmod +x script-server/setup_worker.sh
```

### Run Setup Script
```bash
sudo ./setup_main_server.sh
```

### Grant MySQL Access
```bash
mysql -u root -p
```
```sql
GRANT ALL PRIVILEGES ON cctv_monitoring.* TO 'root'@'%' IDENTIFIED BY 'rootpassword123';
FLUSH PRIVILEGES;
EXIT;
```

### Install Dependencies
```bash
# Backend
cd cctv_backend
npm install

# Script-server
cd ../script-server
pip install -r requirements.txt
```

### Start with PM2
```bash
cd ..
pm2 start ecosystem.config.js
pm2 save
pm2 startup
```

### Verify
```bash
pm2 status
curl http://localhost:8000/health
sudo exportfs -v
```

**Checklist:**
- [ ] Scripts executable
- [ ] Setup script completed
- [ ] MySQL remote access granted
- [ ] Dependencies installed
- [ ] PM2 services running
- [ ] Health endpoint responding
- [ ] NFS server running

---

## Phase 3: Worker Nodes Setup

### Get Main Server IP
```bash
hostname -I | awk '{print $1}'
```
**Main Server IP**: `_________________`

### For Each Worker Node

**Worker 2:**
```bash
# Copy script-server folder
scp -r /path/to/cctv/script-server user@worker2-ip:/home/user/

# SSH to worker
ssh user@worker2-ip

# Run setup
cd /home/user/script-server
sudo ./setup_worker.sh 2 <MAIN_SERVER_IP> 5001 10000

# Verify
curl http://localhost:8000/health
docker logs cctv_script_server_worker_2
```

**Worker 3:**
```bash
scp -r /path/to/cctv/script-server user@worker3-ip:/home/user/
ssh user@worker3-ip
cd /home/user/script-server
sudo ./setup_worker.sh 3 <MAIN_SERVER_IP> 10001 15000
curl http://localhost:8000/health
```

**Worker 4:**
```bash
scp -r /path/to/cctv/script-server user@worker4-ip:/home/user/
ssh user@worker4-ip
cd /home/user/script-server
sudo ./setup_worker.sh 4 <MAIN_SERVER_IP> 15001 20000
curl http://localhost:8000/health
```

**Checklist per worker:**
- [ ] script-server folder copied
- [ ] Setup script completed
- [ ] NFS mounted
- [ ] MySQL accessible
- [ ] GPU detected
- [ ] Docker containers running
- [ ] Health endpoint responding

---

## Phase 4: Final Verification

### Main Server
```bash
# Check all services
pm2 status
sudo systemctl status nfs-kernel-server
sudo systemctl status mysql

# Check NFS exports
sudo exportfs -v

# Check health
curl http://localhost:8000/health
```

### Each Worker
```bash
# Check containers
docker-compose -f docker-compose.worker.yml ps

# Check NFS mount
df -h | grep nfs

# Check health
curl http://localhost:8000/health

# Check GPU
docker exec cctv_script_server_worker_2 nvidia-smi
```

### Test Camera Distribution
```bash
# On main server
cd script-server
python3 test_scaling.py
```

**Final Checklist:**
- [ ] Main server PM2 services running
- [ ] Main server NFS accessible
- [ ] Main server MySQL accessible
- [ ] All workers connected to NFS
- [ ] All workers connected to MySQL
- [ ] All workers GPU accessible
- [ ] All workers health endpoints responding
- [ ] Camera distribution test passed

---

## Camera Distribution Summary

**Total: 20,000 cameras**

| System | Worker ID | IP Address | Cameras | Status |
|--------|-----------|------------|---------|--------|
| Main Server | main | __________ | 1-5,000 | [ ] |
| Worker 2 | worker-2 | __________ | 5,001-10,000 | [ ] |
| Worker 3 | worker-3 | __________ | 10,001-15,000 | [ ] |
| Worker 4 | worker-4 | __________ | 15,001-20,000 | [ ] |

---

## Troubleshooting

### Main Server Issues

**NFS not working:**
```bash
sudo systemctl restart nfs-kernel-server
sudo exportfs -ra
sudo exportfs -v
```

**MySQL not accessible:**
```bash
sudo systemctl restart mysql
mysql -h 127.0.0.1 -u root -p -e "SELECT 1"
```

**PM2 services not starting:**
```bash
pm2 logs
pm2 restart all
```

### Worker Issues

**Can't connect to MySQL:**
```bash
telnet <main-server-ip> 3306
# If fails, check firewall on main server
```

**NFS mount fails:**
```bash
showmount -e <main-server-ip>
sudo mount -t nfs <main-server-ip>:/mnt/nfs/cctv_images /mnt/nfs/cctv_images
```

**GPU not detected:**
```bash
docker run --rm --gpus all nvidia/cuda:12.2.0-base-ubuntu22.04 nvidia-smi
```

---

## Success Criteria

✅ Main server running with PM2  
✅ NFS server accessible from all workers  
✅ MySQL accessible from all workers  
✅ All workers processing assigned camera ranges  
✅ Images saved to shared NFS storage  
✅ Results visible in frontend  

---

## Support Files

- `MAIN_SERVER_SETUP_INSTRUCTIONS.md` - Detailed main server setup
- `script-server/WORKER_DEPLOYMENT.md` - Detailed worker setup
- `PM2_DEPLOYMENT_GUIDE.md` - PM2-specific guide
- `ARCHITECTURE.txt` - System architecture diagram
