# Main Server Setup Instructions

## After Pushing Code to VM

### Step 1: Make Scripts Executable

```bash
cd /path/to/cctv
chmod +x setup_main_server.sh
chmod +x script-server/setup_worker.sh
```

### Step 2: Run Main Server Setup

```bash
sudo ./setup_main_server.sh
```

This script will automatically:
- ✓ Install NFS server
- ✓ Create shared directory `/mnt/nfs/cctv_images`
- ✓ Configure NFS exports
- ✓ Configure MySQL for remote access
- ✓ Configure firewall (MySQL + NFS ports)
- ✓ Update script-server/.env with worker settings
- ✓ Link results directory to NFS storage

### Step 3: Grant MySQL Remote Access

```bash
mysql -u root -p
```

In MySQL:
```sql
GRANT ALL PRIVILEGES ON cctv_monitoring.* TO 'root'@'%' IDENTIFIED BY 'rootpassword123';
FLUSH PRIVILEGES;
EXIT;
```

### Step 4: Start Application with PM2

```bash
# Install dependencies if needed
cd cctv_backend
npm install

cd ../script-server
pip install -r requirements.txt

# Start with PM2
cd ..
pm2 start ecosystem.config.js
pm2 save
pm2 startup  # Enable auto-start on boot
```

### Step 5: Verify Setup

```bash
# Check PM2 services
pm2 status

# Check script-server health
curl http://localhost:8000/health

# Should show:
# {
#   "success": true,
#   "worker": {
#     "worker_id": "main",
#     "distribution_mode": "single"
#   }
# }

# Check NFS
sudo exportfs -v

# Check MySQL
mysql -h 127.0.0.1 -u root -prootpassword123 -e "SELECT 1"
```

---

## Manual Setup (If Script Fails)

### 1. Install NFS

```bash
sudo apt-get update
sudo apt-get install -y nfs-kernel-server
sudo mkdir -p /mnt/nfs/cctv_images
sudo chmod 777 /mnt/nfs/cctv_images
```

### 2. Configure NFS Exports

```bash
sudo nano /etc/exports
```

Add:
```
/mnt/nfs/cctv_images *(rw,sync,no_subtree_check,no_root_squash)
```

```bash
sudo exportfs -ra
sudo systemctl restart nfs-kernel-server
sudo systemctl enable nfs-kernel-server
```

### 3. Configure MySQL

```bash
sudo nano /etc/mysql/mysql.conf.d/mysqld.cnf
```

Change:
```
bind-address = 0.0.0.0
```

```bash
sudo systemctl restart mysql
```

### 4. Configure Firewall

```bash
sudo ufw allow 3306/tcp
sudo ufw allow 2049/tcp
sudo ufw allow 111/tcp
sudo ufw allow 111/udp
```

### 5. Update script-server/.env

```bash
cd script-server
nano .env
```

Add:
```bash
WORKER_ID=main
WORKER_NAME=Main Server
DISTRIBUTION_MODE=single
```

### 6. Link Results to NFS

```bash
cd script-server
mv results results.backup
ln -s /mnt/nfs/cctv_images results
```

---

## Verification Checklist

- [ ] NFS server running: `sudo systemctl status nfs-kernel-server`
- [ ] NFS exports configured: `sudo exportfs -v`
- [ ] MySQL accessible: `mysql -h 127.0.0.1 -u root -p -e "SELECT 1"`
- [ ] Firewall configured: `sudo ufw status`
- [ ] script-server/.env updated: `cat script-server/.env | grep WORKER_ID`
- [ ] PM2 services running: `pm2 status`
- [ ] Health endpoint working: `curl http://localhost:8000/health`

---

## Get Your Server IP

```bash
# Get server IP for worker configuration
hostname -I | awk '{print $1}'
```

Use this IP when setting up worker nodes.

---

## Next: Deploy Worker Nodes

Once main server is running, deploy workers:

```bash
# On each worker node
scp -r /path/to/cctv/script-server user@worker-ip:/home/user/
ssh user@worker-ip
cd /home/user/script-server
sudo ./setup_worker.sh <worker_id> <main_server_ip> <cam_start> <cam_end>
```

---

## Troubleshooting

### NFS not starting
```bash
sudo systemctl status nfs-kernel-server
sudo journalctl -u nfs-kernel-server -n 50
```

### MySQL not accessible remotely
```bash
# Check bind-address
sudo grep bind-address /etc/mysql/mysql.conf.d/mysqld.cnf

# Check firewall
sudo ufw status

# Test connection
mysql -h 127.0.0.1 -u root -p
```

### PM2 services not starting
```bash
pm2 logs
pm2 restart all
```

---

## Summary

**On VM after pushing code:**

1. `chmod +x setup_main_server.sh`
2. `sudo ./setup_main_server.sh`
3. Grant MySQL access
4. `pm2 start ecosystem.config.js`
5. `pm2 save`
6. Verify with `curl http://localhost:8000/health`

**Done!** Main server ready for worker nodes.
