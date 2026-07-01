# PM2 Deployment Guide (Main Server + Docker Workers)

## Your Setup

**Main Server**: PM2 (Keep as is)
**Worker Nodes**: Docker (New)

---

## Main Server Setup (5 Minutes)

### Step 1: Install NFS Server

```bash
sudo apt-get install -y nfs-kernel-server
sudo mkdir -p /mnt/nfs/cctv_images
sudo chmod 777 /mnt/nfs/cctv_images
```

### Step 2: Configure NFS Exports

```bash
sudo nano /etc/exports
```

Add (replace with your worker IPs):
```
/mnt/nfs/cctv_images 192.168.1.101(rw,sync,no_subtree_check)
/mnt/nfs/cctv_images 192.168.1.102(rw,sync,no_subtree_check)
/mnt/nfs/cctv_images 192.168.1.103(rw,sync,no_subtree_check)
```

```bash
sudo exportfs -ra
sudo systemctl restart nfs-kernel-server
```

### Step 3: Configure MySQL Remote Access

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

Grant access:
```bash
mysql -u root -p
```

```sql
GRANT ALL PRIVILEGES ON cctv_monitoring.* TO 'root'@'%' IDENTIFIED BY 'rootpassword123';
FLUSH PRIVILEGES;
EXIT;
```

Allow firewall:
```bash
sudo ufw allow from 192.168.1.0/24 to any port 3306
```

### Step 4: Update Script-Server Environment

```bash
cd script-server
nano .env
```

Add these lines:
```bash
WORKER_ID=main
WORKER_NAME=Main Server
DISTRIBUTION_MODE=single
```

### Step 5: Restart PM2

```bash
pm2 restart cctv-backend-fastapi
pm2 save
```

### Step 6: Verify

```bash
# Check NFS
sudo exportfs -v

# Check MySQL
mysql -h 127.0.0.1 -u root -prootpassword123 -e "SELECT 1"

# Check script-server
curl http://localhost:8000/health
```

---

## Worker Nodes Setup (Same Process)

On each worker:

```bash
# Copy script-server folder
scp -r /path/to/cctv/script-server user@worker-ip:/home/user/

# Run setup
cd /home/user/script-server
sudo ./setup_worker.sh 2 192.168.1.100 5001 10000
```

---

## Key Points

1. **Main server MySQL port**: 3306 (not 3307)
2. **Workers use Docker**: Easier deployment
3. **Main server uses PM2**: No changes needed
4. **NFS**: Shared storage for all workers
5. **Camera distribution**: Automatic

---

## Architecture

```
Main Server (PM2):
├── MySQL (native, port 3306) ← Workers connect here
├── NFS Server (/mnt/nfs/cctv_images) ← Shared storage
├── Backend (PM2)
├── Frontend
└── Script-Server (PM2, cameras 1-5,000)

Worker 2 (Docker):
└── Script-Server (cameras 5,001-10,000)

Worker 3 (Docker):
└── Script-Server (cameras 10,001-15,000)

Worker 4 (Docker):
└── Script-Server (cameras 15,001-20,000)
```

---

## Troubleshooting

### Workers can't connect to MySQL

```bash
# On main server
sudo ufw status
sudo ufw allow from <worker-ip> to any port 3306

# Test from worker
telnet 192.168.1.100 3306
```

### NFS mount fails

```bash
# On main server
sudo systemctl status nfs-kernel-server
sudo exportfs -v

# On worker
showmount -e 192.168.1.100
```

### PM2 script-server not starting

```bash
pm2 logs cctv-backend-fastapi
pm2 restart cctv-backend-fastapi
```

---

## Summary

**Main Server (5 minutes):**
1. Install NFS ✓
2. Configure MySQL remote access ✓
3. Add worker env vars ✓
4. Restart PM2 ✓

**Each Worker (2 minutes):**
1. Copy script-server folder ✓
2. Run setup_worker.sh ✓

**Done!** System scales horizontally.
