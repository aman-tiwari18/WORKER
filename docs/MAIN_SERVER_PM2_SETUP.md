# Main Server Setup (PM2 - No Docker)

## Your Current Setup
- Backend: Running with PM2
- Script-Server: Running with PM2
- MySQL: Running natively (not in Docker)
- Frontend: Running separately

**Keep everything as is!** Just add NFS server.

---

## What You Need To Do

### 1. Install NFS Server (One-Time Setup)

```bash
# Install NFS server
sudo apt-get update
sudo apt-get install -y nfs-kernel-server

# Create shared directory
sudo mkdir -p /mnt/nfs/cctv_images
sudo chmod 777 /mnt/nfs/cctv_images

# Configure exports (replace with your actual worker IPs)
sudo nano /etc/exports
```

Add these lines (replace with your worker IPs):
```
/mnt/nfs/cctv_images 192.168.1.101(rw,sync,no_subtree_check)
/mnt/nfs/cctv_images 192.168.1.102(rw,sync,no_subtree_check)
/mnt/nfs/cctv_images 192.168.1.103(rw,sync,no_subtree_check)
```

```bash
# Apply and start NFS
sudo exportfs -ra
sudo systemctl restart nfs-kernel-server
sudo systemctl enable nfs-kernel-server

# Verify NFS is running
sudo exportfs -v
sudo systemctl status nfs-kernel-server
```

### 2. Configure MySQL for Remote Access

```bash
# Edit MySQL config
sudo nano /etc/mysql/mysql.conf.d/mysqld.cnf
```

Change:
```
bind-address = 0.0.0.0
```

```bash
# Restart MySQL
sudo systemctl restart mysql

# Allow remote connections
mysql -u root -p
```

In MySQL:
```sql
GRANT ALL PRIVILEGES ON cctv_monitoring.* TO 'root'@'%' IDENTIFIED BY 'rootpassword123';
FLUSH PRIVILEGES;
EXIT;
```

```bash
# Allow MySQL port in firewall
sudo ufw allow from 192.168.1.0/24 to any port 3306
```

### 3. Update Script-Server Environment

Edit `script-server/.env`:
```bash
# Add these lines
WORKER_ID=main
WORKER_NAME=Main Server
DISTRIBUTION_MODE=single
```

### 4. Restart Script-Server

```bash
pm2 restart cctv-backend-fastapi
pm2 save
```

### 5. Link Results to NFS (Optional but Recommended)

```bash
# Backup current results
mv script-server/results script-server/results.backup

# Link to NFS
ln -s /mnt/nfs/cctv_images script-server/results

# Or copy existing results to NFS
cp -r script-server/results.backup/* /mnt/nfs/cctv_images/
```

---

## Verify Main Server Setup

```bash
# Check NFS
sudo exportfs -v
sudo systemctl status nfs-kernel-server

# Check MySQL remote access
mysql -h 127.0.0.1 -P 3306 -u root -prootpassword123 -e "SELECT 1"

# Check PM2 services
pm2 status

# Check script-server health
curl http://localhost:8000/health
```

---

## Worker Nodes Setup (Same as Before)

Workers will use Docker. On each worker:

```bash
# Copy script-server folder
scp -r /path/to/cctv/script-server user@worker-ip:/home/user/

# Run setup
cd /home/user/script-server
sudo ./setup_worker.sh 2 192.168.1.100 5001 10000
```

**Important**: Workers connect to your main server's:
- MySQL on port **3306** (not 3307 since you're not using Docker)
- NFS on port **2049**

---

## Architecture

```
Main Server (PM2 - No Docker):
├── MySQL (native, port 3306)
├── NFS Server (/mnt/nfs/cctv_images)
├── Backend (PM2)
├── Frontend (PM2 or separate)
└── Script-Server (PM2, cameras 1-5,000)

Worker Nodes (Docker):
├── Script-Server (Docker)
└── Connects to main server MySQL + NFS
```

---

## Key Differences from Docker Setup

| Component | Docker Setup | Your PM2 Setup |
|-----------|--------------|----------------|
| MySQL Port | 3307 | 3306 |
| Backend | Docker | PM2 |
| Script-Server (Main) | Docker | PM2 |
| Script-Server (Workers) | Docker | Docker |
| NFS | Same | Same |

---

## Update Worker Environment

Since your MySQL is on port 3306 (not 3307), update `.env.worker.example`:

```bash
# In script-server/.env.worker.example
DB_PORT=3306  # Change from 3307 to 3306
```

---

## Summary

**Main Server (PM2):**
1. Install NFS server ✓
2. Configure MySQL remote access ✓
3. Add worker env vars to script-server/.env ✓
4. Restart PM2 services ✓
5. Keep everything else as is ✓

**Workers (Docker):**
1. Copy script-server folder
2. Run setup_worker.sh
3. Done!

**No need to change your main server to Docker!**
