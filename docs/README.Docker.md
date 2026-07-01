# CCTV Monitoring System - Docker Setup

Complete Docker setup for the CCTV Monitoring System with automated deployment.

## 🚀 Quick Start

### Prerequisites
- Docker Engine 20.10+
- Docker Compose 2.0+
- NVIDIA Docker (for GPU support - optional but recommended)
- At least 8GB RAM
- 20GB free disk space

### 1. Clone and Setup

```bash
# Clone the repository
git clone <your-repo-url>
cd cctv-monitoring

# Copy environment file
cp .env.example .env

# Edit .env file with your configuration
nano .env
```

### 2. Start All Services

```bash
# Build and start all services
docker-compose up -d

# View logs
docker-compose logs -f

# Check status
docker-compose ps
```

### 3. Access the Application

- **Frontend**: http://localhost:3000
- **Backend API**: http://localhost:5000
- **MySQL**: localhost:3306
- **Ollama**: http://localhost:11434

### Default Credentials
- **Username**: admin
- **Password**: admin123

## 📦 Services

### 1. MySQL Database
- **Container**: `cctv_mysql`
- **Port**: 3306
- **Database**: cctv_monitoring
- **Auto-migrations**: Yes

### 2. Backend API
- **Container**: `cctv_backend`
- **Port**: 5000
- **Features**:
  - REST API
  - WebSocket support
  - Auto-migration on startup
  - Health checks

### 3. Frontend
- **Container**: `cctv_frontend`
- **Port**: 3000
- **Tech**: React + Vite + Nginx
- **Features**:
  - Production build
  - Gzip compression
  - API proxy

### 4. Script Server
- **Container**: `cctv_script_server`
- **Features**:
  - Python detection scripts
  - YOLO models
  - Database integration
  - GPU support (if available)

### 5. Ollama (VLM)
- **Container**: `cctv_ollama`
- **Port**: 11434
- **Model**: qwen3-vl:8b
- **GPU**: Required for good performance

## 🔧 Common Commands

### Start/Stop Services

```bash
# Start all services
docker-compose up -d

# Stop all services
docker-compose down

# Restart a specific service
docker-compose restart backend

# View logs
docker-compose logs -f backend
docker-compose logs -f frontend
docker-compose logs -f script-server
```

### Database Operations

```bash
# Access MySQL shell
docker exec -it cctv_mysql mysql -u root -prootpassword123 cctv_monitoring

# Run migrations manually
docker exec -it cctv_backend node src/migrations/run.js

# Backup database
docker exec cctv_mysql mysqldump -u root -prootpassword123 cctv_monitoring > backup.sql

# Restore database
docker exec -i cctv_mysql mysql -u root -prootpassword123 cctv_monitoring < backup.sql
```

### Script Server Operations

```bash
# Run vision classification
docker exec -it cctv_script_server python scripts/detection/vision_classification_rtsp.py --exam_id 19

# Run QP box detection
docker exec -it cctv_script_server python scripts/detection/qp_box_det.py

# Run crowd detection
docker exec -it cctv_script_server python scripts/detection/crowd_zero_det.py

# Fetch RTSP URLs
docker exec -it cctv_script_server python scripts/detection/fetch_rtsp_urls.py --exam_id 19

# Access script server shell
docker exec -it cctv_script_server bash
```

### Ollama Operations

```bash
# Pull VLM model
docker exec -it cctv_ollama ollama pull qwen3-vl:8b

# List models
docker exec -it cctv_ollama ollama list

# Test model
docker exec -it cctv_ollama ollama run qwen3-vl:8b "Hello"
```

## 🔍 Monitoring & Debugging

### View Logs

```bash
# All services
docker-compose logs -f

# Specific service
docker-compose logs -f backend
docker-compose logs -f script-server

# Last 100 lines
docker-compose logs --tail=100 backend
```

### Check Health

```bash
# Check all containers
docker-compose ps

# Check specific service health
docker inspect --format='{{.State.Health.Status}}' cctv_backend
docker inspect --format='{{.State.Health.Status}}' cctv_mysql
```

### Resource Usage

```bash
# View resource usage
docker stats

# View disk usage
docker system df
```

## 🛠️ Development Mode

For development with hot-reload:

```bash
# Backend development
cd cctv_backend
npm install
npm run dev

# Frontend development
cd cctv_frontend
npm install
npm run dev
```

## 📁 Volume Management

### Persistent Data Locations

- **MySQL Data**: `mysql_data` volume
- **Ollama Models**: `ollama_data` volume
- **Backend Uploads**: `./cctv_backend/uploads`
- **Backend Logs**: `./cctv_backend/logs`
- **Detection Results**: `./script-server/results`
- **Captured Frames**: `./script-server/frames`
- **YOLO Models**: `./script-server/models`

### Backup Volumes

```bash
# Backup MySQL data
docker run --rm -v cctv_mysql_data:/data -v $(pwd):/backup alpine tar czf /backup/mysql_backup.tar.gz /data

# Backup Ollama models
docker run --rm -v cctv_ollama_data:/data -v $(pwd):/backup alpine tar czf /backup/ollama_backup.tar.gz /data
```

## 🔐 Security

### Production Checklist

- [ ] Change default passwords in `.env`
- [ ] Update `JWT_SECRET` to a strong random value
- [ ] Configure proper email/SMS credentials
- [ ] Enable HTTPS (use reverse proxy like Nginx/Traefik)
- [ ] Set up firewall rules
- [ ] Regular database backups
- [ ] Update Docker images regularly

### Generate Secure JWT Secret

```bash
openssl rand -base64 64
```

## 🐛 Troubleshooting

### MySQL Connection Issues

```bash
# Check MySQL is running
docker-compose ps mysql

# Check MySQL logs
docker-compose logs mysql

# Test connection
docker exec -it cctv_mysql mysql -u root -prootpassword123 -e "SELECT 1"
```

### Backend Not Starting

```bash
# Check logs
docker-compose logs backend

# Rebuild container
docker-compose up -d --build backend

# Check environment variables
docker exec cctv_backend env
```

### Script Server Issues

```bash
# Check Python dependencies
docker exec -it cctv_script_server pip list

# Test database connection
docker exec -it cctv_script_server python -c "import pymysql; print('OK')"

# Check GPU availability
docker exec -it cctv_script_server python -c "import torch; print(torch.cuda.is_available())"
```

### Ollama Model Issues

```bash
# Pull model again
docker exec -it cctv_ollama ollama pull qwen3-vl:8b

# Check model list
docker exec -it cctv_ollama ollama list

# Check Ollama logs
docker-compose logs ollama
```

## 🔄 Updates & Maintenance

### Update Services

```bash
# Pull latest images
docker-compose pull

# Rebuild and restart
docker-compose up -d --build

# Remove old images
docker image prune -a
```

### Clean Up

```bash
# Stop and remove containers
docker-compose down

# Remove volumes (⚠️ deletes data!)
docker-compose down -v

# Clean up everything
docker system prune -a --volumes
```

## 📊 Performance Tuning

### MySQL Optimization

Edit `docker-compose.yml` to add MySQL configuration:

```yaml
mysql:
  command: --max_connections=200 --innodb_buffer_pool_size=1G
```

### Script Server GPU

Ensure NVIDIA Docker is installed:

```bash
# Install NVIDIA Docker
distribution=$(. /etc/os-release;echo $ID$VERSION_ID)
curl -s -L https://nvidia.github.io/nvidia-docker/gpgkey | sudo apt-key add -
curl -s -L https://nvidia.github.io/nvidia-docker/$distribution/nvidia-docker.list | sudo tee /etc/apt/sources.list.d/nvidia-docker.list
sudo apt-get update && sudo apt-get install -y nvidia-docker2
sudo systemctl restart docker
```

## 📝 Environment Variables

### Backend (.env)
```bash
NODE_ENV=production
PORT=5000
DB_HOST=mysql
DB_USER=root
DB_PASSWORD=rootpassword123
DB_NAME=cctv_monitoring
JWT_SECRET=your-secret-key
```

### Frontend (.env)
```bash
VITE_API_URL=http://localhost:5000/api
VITE_SOCKET_URL=http://localhost:5000
```

### Script Server (.env)
```bash
DB_HOST=mysql
DB_USER=root
DB_PASSWORD=rootpassword123
EXAM_ID=19
PYTHONUNBUFFERED=1
```

## 🎯 Next Steps

1. **Setup Ollama Model**:
   ```bash
   docker exec -it cctv_ollama ollama pull qwen3-vl:8b
   ```

2. **Place YOLO Models**:
   - Download YOLOv10 models
   - Place in `script-server/models/` directory

3. **Configure Email/SMS** (Optional):
   - Update `.env` with credentials
   - Restart backend: `docker-compose restart backend`

4. **Test the System**:
   - Access frontend: http://localhost:3000
   - Login with admin/admin123
   - Upload camera data
   - Run detection scripts

## 📞 Support

For issues or questions:
- Check logs: `docker-compose logs -f`
- Review this documentation
- Check container health: `docker-compose ps`

## 🎉 Success!

Your CCTV Monitoring System is now running in Docker! 🚀
