#!/bin/bash

# Worker Node Dependencies Installation Script
# Installs Docker, NVIDIA Container Toolkit, NFS client, and other dependencies

set -e

echo "=========================================="
echo "Worker Node Dependencies Installation"
echo "=========================================="
echo ""

# Check if running as root
if [ "$EUID" -ne 0 ]; then 
    echo "ERROR: Please run as root (use sudo)"
    exit 1
fi

echo ""
echo "============================================================"
echo "Step 1: Updating System"
echo "============================================================"
echo ""
echo "[INFO] Updating package lists..."
apt-get update
echo "[INFO] Upgrading installed packages..."
apt-get upgrade -y
echo "[SUCCESS] System updated"

echo ""
echo "============================================================"
echo "Step 2: Installing Basic Tools"
echo "============================================================"
echo ""
echo "[INFO] Installing curl, wget, git, net-tools..."
apt-get install -y \
    curl \
    wget \
    git \
    net-tools \
    netcat-openbsd \
    ca-certificates \
    gnupg \
    lsb-release
echo "[SUCCESS] Basic tools installed"

echo ""
echo "============================================================"
echo "Step 3: Installing Docker"
echo "============================================================"
echo ""
echo "[INFO] Installing Docker..."

# Remove old Docker versions if they exist
apt-get remove -y docker docker-engine docker.io containerd runc 2>/dev/null || true

# Add Docker's official GPG key
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg

# Set up Docker repository
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  $(lsb_release -cs) stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null

# Install Docker Engine
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# Start and enable Docker
systemctl start docker
systemctl enable docker

echo "[SUCCESS] Docker installed successfully"
docker --version

echo ""
echo "============================================================"
echo "Step 4: Installing Docker Compose"
echo "============================================================"
echo ""
echo "[INFO] Installing Docker Compose..."
DOCKER_COMPOSE_VERSION="v2.24.5"
curl -L "https://github.com/docker/compose/releases/download/${DOCKER_COMPOSE_VERSION}/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
chmod +x /usr/local/bin/docker-compose
echo "[SUCCESS] Docker Compose installed successfully"
docker-compose --version

echo ""
echo "============================================================"
echo "Step 5: Installing NVIDIA Container Toolkit"
echo "============================================================"
echo ""
echo "[INFO] Installing NVIDIA Container Toolkit..."

# Add NVIDIA Container Toolkit GPG key
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg

# Add the stable repository
echo "deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://nvidia.github.io/libnvidia-container/stable/deb/\$(ARCH) /" | \
    tee /etc/apt/sources.list.d/nvidia-container-toolkit.list

# Update and install
apt-get update
apt-get install -y nvidia-container-toolkit

# Configure Docker to use NVIDIA runtime
nvidia-ctk runtime configure --runtime=docker
systemctl restart docker

echo "[SUCCESS] NVIDIA Container Toolkit installed"

echo ""
echo "============================================================"
echo "Step 6: Installing NFS Client"
echo "============================================================"
echo ""
echo "[INFO] Installing NFS client..."
apt-get install -y nfs-common
echo "[SUCCESS] NFS client installed"

echo ""
echo "============================================================"
echo "Step 7: Configuring Firewall"
echo "============================================================"
echo ""
if systemctl is-active --quiet ufw; then
    echo "[INFO] Configuring UFW firewall..."
    ufw allow 8000/tcp comment "CCTV Script Server"
    ufw allow 11434/tcp comment "Ollama"
    echo "[SUCCESS] Firewall rules added"
else
    echo "[INFO] UFW not active, skipping firewall configuration"
fi

echo ""
echo "=========================================="
echo "Installation Complete!"
echo "=========================================="
echo ""
echo "Installed versions:"
echo "  Docker: $(docker --version)"
echo "  Docker Compose: $(docker-compose --version)"
echo "  NVIDIA Container Toolkit: $(nvidia-ctk --version 2>/dev/null || echo 'installed')"
echo ""
echo "System information:"
echo "  OS: $(lsb_release -d | cut -f2)"
echo "  Kernel: $(uname -r)"
echo "  Python: $(python3 --version 2>/dev/null || echo 'not found')"
echo ""

# Test NVIDIA GPU access
if command -v nvidia-smi &> /dev/null; then
    echo "GPU Information:"
    nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader
    echo ""
    echo "Testing Docker GPU access..."
    docker run --rm --gpus all nvidia/cuda:12.0.0-base-ubuntu22.04 nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null || echo "[WARNING] GPU test failed - check NVIDIA driver installation"
else
    echo "[WARNING] nvidia-smi not found. GPU support may not be available."
fi

echo ""
echo "=========================================="
echo "Next Steps"
echo "=========================================="
echo ""
echo "1. Ensure main server is accessible and NFS is configured"
echo "2. Run: sudo ./setup_worker.sh <WORKER_ID> <MAIN_IP> <CAM_START> <CAM_END>"
echo ""
echo "Examples:"
echo "  Worker 1: sudo ./setup_worker.sh 1 172.31.102.8 6251 12500"
echo "  Worker 2: sudo ./setup_worker.sh 2 172.31.102.8 12501 18750"
echo "  Worker 3: sudo ./setup_worker.sh 3 172.31.102.8 18751 25000"
echo ""
