# Worker 1 - Detailed Setup Information

## System Information

**Hostname:**`cctvprojectvm03.cdis.local`

**IP Address:**`192.168.19.43`

**Worker ID:**`worker-1`

**Worker Name:**`GPU Worker 1`

## Camera Assignment

**Camera Range:** 6,251 - 12,500

**Total Cameras:** 6,250 cameras

## Main Server Connection

**Main Server IP:**`192.168.19.30`

**Main Server Backend API:**`http://192.168.19.30:3000/cctv_analytics/api`

**MySQL Database Host:**`192.168.19.30`

**MySQL Database Port:**`3306`

## NFS Mount

**NFS Server:**`192.168.19.30:/mnt/nfs/cctv_images`

**Local Mount Point:**`/mnt/nfs/cctv_images`

**Mount Status:** ✅ Mounted (48GB total, 41GB used, 5.1GB available, 89% usage)

## Symlinks

**Results Directory:**`script-server/results` → `/mnt/nfs/cctv_images`

**Frames Directory:**`script-server/frames` → `/mnt/nfs/cctv_images`

## Docker Containers

### Container 1: Script Server

-**Container Name:**`cctv_script_server_worker_1`

-**Image:**`script-server-script-server`

-**Status:** ✅ Running (healthy)

-**Ports:**`0.0.0.0:8000->8000/tcp`

-**HTTP Port:**`8000`

### Container 2: Ollama

-**Container Name:**`cctv_ollama_worker_1`

-**Image:**`ollama/ollama:latest`

-**Status:** ✅ Running

-**Ports:**`0.0.0.0:11434->11434/tcp`

-**Ollama Port:**`11434`

-**GPU:** NVIDIA H200 NVL (140.4 GiB VRAM)

## Database Configuration

**Database Host:**`192.168.19.30`

**Database Port:**`3306`

**Database User:**`root`

**Database Password:**`rootpassword123`

**Database Name:**`cctv_monitoring`

## Deployment Location

**Working Directory:**`/cctv_dev/cctv/script-server`

**Configuration File:**`/cctv_dev/cctv/script-server/.env`

## Current Problem

### Issue: MySQL Connection Denied

**Error:** The script-server container cannot connect to the MySQL database on the main server.

**Container Logs Show:**

```

MySQL is unavailable - sleeping

```

**Root Cause:** The MySQL server on `192.168.19.30` is not allowing connections from Worker 1's IP address (`192.168.19.43`).

### Fix Required on Main Server (192.168.19.30)

Run the following commands on the **main server** (192.168.19.30):

```bash

# Connect to MySQL

mysql-uroot-p


# Grant access to Worker 1 IP

GRANTALLPRIVILEGESONcctv_monitoring.*TO'root'@'192.168.19.43'IDENTIFIEDBY'rootpassword123';


# Or grant access to entire subnet (recommended for all workers)

GRANTALLPRIVILEGESONcctv_monitoring.*TO'root'@'192.168.19.%'IDENTIFIEDBY'rootpassword123';


# Apply changes

FLUSHPRIVILEGES;


# Exit MySQL

EXIT;

```

**Also ensure MySQL is listening on all interfaces:**

```bash

# Edit MySQL configuration

sudonano/etc/mysql/mysql.conf.d/mysqld.cnf


# Change this line:

bind-address=127.0.0.1


# To:

bind-address=0.0.0.0


# Save and restart MySQL

sudosystemctlrestartmysql

```

## Verification Commands

```bash

# Check container status

dockerps


# Check script-server logs

dockerlogs-fcctv_script_server_worker_1


# Check Ollama logs

dockerlogs-fcctv_ollama_worker_1


# Check health endpoint

curlhttp://localhost:8000/health


# Check Ollama API

curlhttp://localhost:11434/api/tags


# Check NFS mount

df-h | grepnfs


# Check GPU

nvidia-smi

```

## Network Connectivity

**Worker 1 can reach:**

- ✅ Main Server MySQL Port (3306) - Port is open
- ⚠️ Main Server MySQL Access - Denied due to permissions

**Required Firewall Rules:**

- Port 8000 (HTTP) - Open
- Port 11434 (Ollama) - Open
- Port 3306 (MySQL to main server) - Open

## Environment Variables

All environment variables are configured in: `/cctv_dev/cctv/script-server/.env`

Key variables:

-`WORKER_ID=worker-1`

-`CAMERA_START_ID=6251`

-`CAMERA_END_ID=12500`

-`DB_HOST=192.168.19.30`

-`MAIN_BACKEND_URL=http://192.168.19.30:3000/cctv_analytics/api`

-`NFS_MOUNT_PATH=/mnt/nfs/cctv_images`

## Status Summary

| Component | Status | Notes |

|-----------|--------|-------|

| Docker Containers | ✅ Running | Both containers healthy |

| NFS Mount | ✅ Mounted | 89% usage, 5.1GB free |

| Ollama GPU | ✅ Working | H200 detected, 140.4 GiB VRAM |

| MySQL Connection | ❌ Blocked | Needs permission on main server |

| Script Server | ⏳ Waiting | Waiting for MySQL access |

## Next Steps

1. Fix MySQL permissions on main server (192.168.19.30)
2. Verify script-server can connect to database
3. Confirm camera processing starts
4. Monitor logs for any errors

---

# Worker 2 - Detailed Setup Information

## System Information

**Hostname:**`cctvprojectvm02.cdis.local`

**IP Address:**`192.168.19.42`

**Worker ID:**`worker-2`

**Worker Name:**`GPU Worker 2`

## Camera Assignment

**Camera Range:** 12,501 - 18,750

**Total Cameras:** 6,250 cameras

## Main Server Connection

**Main Server IP:**`192.168.19.30`

**Main Server Backend API:**`http://192.168.19.30:3000/cctv_analytics/api`

**MySQL Database Host:**`192.168.19.30`

**MySQL Database Port:**`3306`

## NFS Mount

**NFS Server:**`192.168.19.30:/mnt/nfs/cctv_images`

**Local Mount Point:**`/mnt/nfs/cctv_images`

**Mount Status:** ⏳ Not yet mounted (will mount when processing starts)

## Symlinks

**Results Directory:**`script-server/results` → `/mnt/nfs/cctv_images`

**Frames Directory:**`script-server/frames` → `/mnt/nfs/cctv_images`

## Docker Containers

### Container 1: Script Server

-**Container Name:**`cctv_script_server_worker_2`

-**Image:**`script-server-script-server`

-**Status:** ✅ Running (healthy)

-**Ports:**`0.0.0.0:8000->8000/tcp`

-**HTTP Port:**`8000`

### Container 2: Ollama

-**Container Name:**`cctv_ollama_worker_2`

-**Image:**`ollama/ollama:latest`

-**Status:** ⚠️ Running (unhealthy - starting up)

-**Ports:**`0.0.0.0:11434->11434/tcp`

-**Ollama Port:**`11434`

-**GPU:** NVIDIA H200 NVL (143,771 MiB VRAM)

## Database Configuration

**Database Host:**`192.168.19.30`

**Database Port:**`3306`

**Database User:**`root`

**Database Password:**`rootpassword123`

**Database Name:**`cctv_monitoring`

## Deployment Location

**Working Directory:**`/cctv_dev/cctv/script_server/script-server`

**Configuration File:**`/cctv_dev/cctv/script_server/script-server/.env`

## Current Problem

### Issue: MySQL Connection Denied

**Error:** The script-server container cannot connect to the MySQL database on the main server.

**Container Logs Show:**

```

MySQL is unavailable - sleeping

```

**Root Cause:** The MySQL server on `192.168.19.30` is not allowing connections from Worker 2's IP address (`192.168.19.42`).

### Fix Required on Main Server (192.168.19.30)

Run the following commands on the **main server** (192.168.19.30):

```bash

# Connect to MySQL

mysql-uroot-p


# Grant access to Worker 2 IP

GRANTALLPRIVILEGESONcctv_monitoring.*TO'root'@'192.168.19.42'IDENTIFIEDBY'rootpassword123';


# Or grant access to entire subnet (recommended for all workers)

GRANTALLPRIVILEGESONcctv_monitoring.*TO'root'@'192.168.19.%'IDENTIFIEDBY'rootpassword123';


# Apply changes

FLUSHPRIVILEGES;


# Exit MySQL

EXIT;

```

**Also ensure MySQL is listening on all interfaces:**

```bash

# Edit MySQL configuration

sudonano/etc/mysql/mysql.conf.d/mysqld.cnf


# Change this line:

bind-address=127.0.0.1


# To:

bind-address=0.0.0.0


# Save and restart MySQL

sudosystemctlrestartmysql

```

## Verification Commands

```bash

# Check container status

dockerps


# Check script-server logs

dockerlogs-fcctv_script_server_worker_2


# Check Ollama logs

dockerlogs-fcctv_ollama_worker_2


# Check health endpoint

curlhttp://localhost:8000/health


# Check Ollama API

curlhttp://localhost:11434/api/tags


# Check NFS mount

df-h | grepnfs


# Check GPU

nvidia-smi

```

## Network Connectivity

**Worker 2 can reach:**

- ✅ Main Server MySQL Port (3306) - Port is open
- ⚠️ Main Server MySQL Access - Denied due to permissions

**Required Firewall Rules:**

- Port 8000 (HTTP) - Open
- Port 11434 (Ollama) - Open
- Port 3306 (MySQL to main server) - Open

## Environment Variables

All environment variables are configured in: `/cctv_dev/cctv/script_server/script-server/.env`

Key variables:

-`WORKER_ID=worker-2`

-`CAMERA_START_ID=12501`

-`CAMERA_END_ID=18750`

-`DB_HOST=192.168.19.30`

-`MAIN_BACKEND_URL=http://192.168.19.30:3000/cctv_analytics/api`

-`NFS_MOUNT_PATH=/mnt/nfs/cctv_images`

## Status Summary

| Component | Status | Notes |

|-----------|--------|-------|

| Docker Containers | ✅ Running | Script server healthy, Ollama starting |

| NFS Mount | ⏳ Pending | Will mount when processing starts |

| Ollama GPU | ✅ Working | H200 detected, 143,771 MiB VRAM |

| MySQL Connection | ❌ Blocked | Needs permission on main server |

| Script Server | ⏳ Waiting | Waiting for MySQL access |

## Next Steps

1. Fix MySQL permissions on main server (192.168.19.30)
2. Verify script-server can connect to database
3. Confirm camera processing starts
4. Monitor logs for any errors

---

# Worker 3 - Detailed Setup Information

## System Information

**Hostname:**`cctvproject`

**IP Address:**`192.168.19.41`

**Worker ID:**`worker-3`

**Worker Name:**`GPU Worker 3`

## Camera Assignment

**Camera Range:** 18,751 - 25,000

**Total Cameras:** 6,250 cameras

## Main Server Connection

**Main Server IP:**`192.168.19.30`

**Main Server Backend API:**`http://192.168.19.30:3000/cctv_analytics/api`

**MySQL Database Host:**`192.168.19.30`

**MySQL Database Port:**`3306`

## NFS Mount

**NFS Server:**`192.168.19.30:/mnt/nfs/cctv_images`

**Local Mount Point:**`/mnt/nfs/cctv_images`

**Mount Status:** ✅ Mounted (48GB total, 41GB used, 5.1GB available, 89% usage)

## Symlinks

**Results Directory:**`script-server/results` → `/mnt/nfs/cctv_images`

**Frames Directory:**`script-server/frames` → `/mnt/nfs/cctv_images`

**Detection Results:**`script-server/scripts/detection/results` → `/mnt/nfs/cctv_images`

## Docker Containers

### Container 1: Script Server

-**Container Name:**`cctv_script_server_worker_worker-3`

-**Image:**`script-server-script-server`

-**Status:** ✅ Running (healthy)

-**Ports:**`0.0.0.0:8000->8000/tcp`

-**HTTP Port:**`8000`

### Container 2: Ollama

-**Container Name:**`cctv_ollama_worker_worker-3`

-**Image:**`ollama/ollama:latest`

-**Status:** ✅ Running

-**Ports:**`0.0.0.0:11434->11434/tcp`

-**Ollama Port:**`11434`

-**GPU:** NVIDIA GPU (CUDA 13.0, Driver 580.126.09)

## Database Configuration

**Database Host:**`192.168.19.30`

**Database Port:**`3306`

**Database User:**`root`

**Database Password:**`rootpassword123`

**Database Name:**`cctv_monitoring`

## Deployment Location

**Working Directory:**`/cctv_dev/cctv/script_server/script-server`

**Configuration File:**`/cctv_dev/cctv/script_server/script-server/.env`

## Current Problem

### Issue: MySQL Connection Denied

**Error:** The script-server container cannot connect to the MySQL database on the main server.

**Container Logs Show:**

```

MySQL is unavailable - sleeping

```

**Root Cause:** The MySQL server on `192.168.19.30` is not allowing connections from Worker 3's IP address (`192.168.19.41`).

### Fix Required on Main Server (192.168.19.30)

Run the following commands on the **main server** (192.168.19.30):

```bash

# Connect to MySQL

mysql-uroot-p


# Grant access to Worker 3 IP

GRANTALLPRIVILEGESONcctv_monitoring.*TO'root'@'192.168.19.41'IDENTIFIEDBY'rootpassword123';


# Or grant access to entire subnet (recommended for all workers)

GRANTALLPRIVILEGESONcctv_monitoring.*TO'root'@'192.168.19.%'IDENTIFIEDBY'rootpassword123';


# Apply changes

FLUSHPRIVILEGES;


# Exit MySQL

EXIT;

```

**Also ensure MySQL is listening on all interfaces:**

```bash

# Edit MySQL configuration

sudonano/etc/mysql/mysql.conf.d/mysqld.cnf


# Change this line:

bind-address=127.0.0.1


# To:

bind-address=0.0.0.0


# Save and restart MySQL

sudosystemctlrestartmysql

```

## Verification Commands

```bash

# Check container status

dockerps


# Check script-server logs

dockerlogs-fcctv_script_server_worker_worker-3


# Check Ollama logs

dockerlogs-fcctv_ollama_worker_worker-3


# Check health endpoint

curlhttp://localhost:8000/health


# Check Ollama API

curlhttp://localhost:11434/api/tags


# Check NFS mount

df-h | grepnfs


# Check GPU

nvidia-smi

```

## Network Connectivity

**Worker 3 can reach:**

- ✅ Main Server MySQL Port (3306) - Port is open
- ⚠️ Main Server MySQL Access - Denied due to permissions

**Required Firewall Rules:**

- Port 8000 (HTTP) - Open
- Port 11434 (Ollama) - Open
- Port 3306 (MySQL to main server) - Open

## Environment Variables

All environment variables are configured in: `/cctv_dev/cctv/script_server/script-server/.env`

Key variables:

-`WORKER_ID=worker-3`

-`CAMERA_START_ID=18751`

-`CAMERA_END_ID=25000`

-`DB_HOST=192.168.19.30`

-`MAIN_BACKEND_URL=http://192.168.19.30:3000/cctv_analytics/api`

-`NFS_MOUNT_PATH=/mnt/nfs/cctv_images`

## Status Summary

| Component | Status | Notes |

|-----------|--------|-------|

| Docker Containers | ✅ Running | Both containers healthy |

| NFS Mount | ✅ Mounted | 89% usage, 5.1GB free |

| Ollama GPU | ✅ Working | CUDA 13.0, Driver 580.126.09 |

| MySQL Connection | ❌ Blocked | Needs permission on main server |

| Script Server | ⏳ Waiting | Waiting for MySQL access |

## Next Steps

1. Fix MySQL permissions on main server (192.168.19.30)
2. Verify script-server can connect to database
3. Confirm camera processing starts
4. Monitor logs for any errors
