# Horizontal Scaling - Quick Reference

## Quick Start Commands

### Main Server Setup

```bash
# 1. Configure NFS
sudo mkdir -p /mnt/nfs/cctv_images
sudo chmod 777 /mnt/nfs/cctv_images
echo "/mnt/nfs/cctv_images *(rw,sync,no_subtree_check)" | sudo tee -a /etc/exports
sudo exportfs -ra

# 2. Start main server
docker-compose up -d

# 3. Verify
docker-compose ps
curl http://localhost:3000/api/health
```

### Worker Node Setup (Automated)

```bash
# Single command setup
sudo ./setup_worker.sh <worker_id> <main_server_ip> <camera_start> <camera_end>

# Examples:
sudo ./setup_worker.sh 2 192.168.1.100 5001 10000
sudo ./setup_worker.sh 3 192.168.1.100 10001 15000
sudo ./setup_worker.sh 4 192.168.1.100 15001 20000
```

### Worker Node Setup (Manual)

```bash
# 1. Install NFS client
sudo apt-get install -y nfs-common

# 2. Mount NFS
sudo mkdir -p /mnt/nfs/cctv_images
sudo mount -t nfs 192.168.1.100:/mnt/nfs/cctv_images /mnt/nfs/cctv_images

# 3. Configure environment
cp .env.worker.example .env.worker
nano .env.worker  # Edit configuration

# 4. Start worker
export $(cat .env.worker | xargs)
docker-compose -f docker-compose.worker.yml up -d

# 5. Verify
docker-compose -f docker-compose.worker.yml ps
curl http://localhost:8000/health
```

## Camera Distribution Calculator

```bash
# Calculate camera ranges
cd script-server
python -m app.camera_distributor

# Example for 20K cameras, 4 workers:
# worker-1: 1-5,000
# worker-2: 5,001-10,000
# worker-3: 10,001-15,000
# worker-4: 15,001-20,000
```

## Common Commands

### Check Worker Status

```bash
# Health check
curl http://localhost:8000/health

# Worker status
curl http://localhost:8000/status

# Running executions
curl http://localhost:8000/executions/running
```

### Monitor GPU

```bash
# From host
nvidia-smi

# From container
docker exec cctv_script_server_worker_2 nvidia-smi

# Continuous monitoring
watch -n 1 nvidia-smi
```

### Check NFS

```bash
# Verify mount
df -h | grep nfs

# Test write
echo "test" > /mnt/nfs/cctv_images/test.txt

# Check NFS stats
nfsstat -c  # Client stats
nfsstat -s  # Server stats (on main server)
```

### View Logs

```bash
# Worker logs
docker logs cctv_script_server_worker_2

# Follow logs
docker logs -f cctv_script_server_worker_2

# Last 100 lines
docker logs --tail 100 cctv_script_server_worker_2
```

### Database Connectivity

```bash
# Test from worker
docker exec -it cctv_script_server_worker_2 bash
mysql -h 192.168.1.100 -P 3307 -u root -prootpassword123 -e "SHOW DATABASES;"
```

## Troubleshooting

### Worker Can't Connect to Database

```bash
# Check network
ping 192.168.1.100

# Check MySQL port
telnet 192.168.1.100 3307

# Check firewall (on main server)
sudo ufw allow from 192.168.1.0/24 to any port 3307
```

### NFS Mount Issues

```bash
# Check NFS server (on main server)
sudo systemctl status nfs-kernel-server
sudo exportfs -v

# Remount (on worker)
sudo umount /mnt/nfs/cctv_images
sudo mount -t nfs 192.168.1.100:/mnt/nfs/cctv_images /mnt/nfs/cctv_images
```

### Worker Not Processing Cameras

```bash
# Check environment
docker exec cctv_script_server_worker_2 env | grep CAMERA

# Check logs
docker logs cctv_script_server_worker_2 | grep "Camera"

# Restart worker
docker-compose -f docker-compose.worker.yml restart
```

### GPU Not Detected

```bash
# Test NVIDIA runtime
docker run --rm --gpus all nvidia/cuda:12.2.0-base-ubuntu22.04 nvidia-smi

# Check daemon config
cat /etc/docker/daemon.json

# Restart Docker
sudo systemctl restart docker
```

## Deployment Checklist

### Main Server
- [ ] NFS server installed and configured
- [ ] MySQL accessible on port 3307
- [ ] Firewall rules configured
- [ ] All services running (`docker-compose ps`)
- [ ] Health endpoint responding

### Each Worker Node
- [ ] NFS client installed
- [ ] NFS mount working
- [ ] Database connectivity verified
- [ ] GPU accessible
- [ ] Environment configured
- [ ] Services running
- [ ] Health endpoint responding

## Performance Monitoring

```bash
# System resources
htop

# Disk I/O
iostat -x 5

# Network traffic
iftop

# Docker stats
docker stats

# GPU utilization
nvidia-smi dmon -s pucvmet
```

## Scaling Examples

### 4 Workers (20K cameras)
```
Main:    1-5,000
Worker2: 5,001-10,000
Worker3: 10,001-15,000
Worker4: 15,001-20,000
```

### 10 Workers (100K cameras)
```
Main:     1-10,000
Worker2:  10,001-20,000
Worker3:  20,001-30,000
...
Worker10: 90,001-100,000
```

## Network Requirements

| Workers | Cameras | Bandwidth | Storage/Day |
|---------|---------|-----------|-------------|
| 4       | 20K     | 1 Gbps    | ~200 GB     |
| 10      | 100K    | 10 Gbps   | ~1 TB       |
| 20      | 200K    | 10 Gbps   | ~2 TB       |

## Support Contacts

- System Admin: [contact info]
- Network Team: [contact info]
- Database Team: [contact info]

---

**Quick Reference Version**: 1.0.0  
**Last Updated**: 2026-04-29
