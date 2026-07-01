# 🚀 Quick Start Guide - CCTV Monitoring System

Get your CCTV Monitoring System up and running in 5 minutes!

## Prerequisites

- Docker Engine 20.10+
- Docker Compose 2.0+
- 8GB RAM minimum
- 20GB free disk space

## Installation

### Option 1: Automated Setup (Recommended)

```bash
# Make the start script executable
chmod +x start.sh

# Run the setup
./start.sh
```

### Option 2: Using Makefile

```bash
# Start everything
make start

# View available commands
make help
```

### Option 3: Manual Setup

```bash
# 1. Copy environment file
cp .env.example .env

# 2. Build and start services
docker-compose up -d

# 3. Wait for services to be ready (2-3 minutes)
docker-compose logs -f
```

## Access the Application

Once all services are running:

- **Frontend**: http://localhost:3000
- **Backend API**: http://localhost:5000
- **API Docs**: http://localhost:5000/api/docs

### Default Login
- **Username**: `admin`
- **Password**: `admin123`

## Post-Installation Setup

### 1. Setup Ollama VLM Model

```bash
# Pull the vision model (required for classification)
docker exec -it cctv_ollama ollama pull qwen3-vl:8b

# This will download ~5GB - may take 10-15 minutes
```

### 2. Add YOLO Models

Download YOLO models and place them in `script-server/models/`:

```bash
# Create models directory if it doesn't exist
mkdir -p script-server/models

# Download YOLOv10 models (you'll need to get these)
# Place yolov10m.pt and yolov10x.pt in script-server/models/
```

### 3. Test the System

```bash
# Check all services are running
docker-compose ps

# View logs
docker-compose logs -f

# Test backend health
curl http://localhost:5000/api/health
```

## Running Detection Scripts

### Vision Classification (RTSP)

```bash
# Using docker exec
docker exec -it cctv_script_server python scripts/detection/vision_classification_rtsp.py --exam_id 19

# Using Makefile
make detect-vision
```

### QP Box Detection

```bash
docker exec -it cctv_script_server python scripts/detection/qp_box_det.py

# Or
make detect-qp
```

### Fetch RTSP URLs

```bash
docker exec -it cctv_script_server python scripts/detection/fetch_rtsp_urls.py --exam_id 19

# Or
make fetch-urls
```

## Common Commands

### Service Management

```bash
# Start all services
docker-compose up -d

# Stop all services
docker-compose down

# Restart a service
docker-compose restart backend

# View logs
docker-compose logs -f backend
```

### Database Operations

```bash
# Access MySQL shell
docker exec -it cctv_mysql mysql -uroot -prootpassword123 cctv_monitoring

# Backup database
make db-backup

# Run migrations
make db-migrate
```

### Monitoring

```bash
# Check service status
docker-compose ps

# View all logs
docker-compose logs -f

# View specific service logs
docker-compose logs -f backend
docker-compose logs -f script-server

# Check resource usage
docker stats
```

## Troubleshooting

### Services Not Starting

```bash
# Check logs for errors
docker-compose logs

# Rebuild containers
docker-compose up -d --build

# Check Docker resources
docker system df
```

### Database Connection Issues

```bash
# Check MySQL is running
docker-compose ps mysql

# Test connection
docker exec -it cctv_mysql mysql -uroot -prootpassword123 -e "SELECT 1"

# Restart MySQL
docker-compose restart mysql
```

### Backend Not Responding

```bash
# Check backend logs
docker-compose logs backend

# Check environment variables
docker exec cctv_backend env | grep DB

# Restart backend
docker-compose restart backend
```

### Script Server Issues

```bash
# Check Python dependencies
docker exec -it cctv_script_server pip list

# Test database connection
docker exec -it cctv_script_server python -c "import pymysql; print('OK')"

# Access shell for debugging
docker exec -it cctv_script_server bash
```

## Stopping the System

```bash
# Stop all services (keeps data)
docker-compose down

# Stop and remove volumes (⚠️ deletes all data)
docker-compose down -v

# Clean up everything
make clean-all
```

## Next Steps

1. **Login to Frontend**: http://localhost:3000
2. **Upload Camera Data**: Use the admin panel
3. **Configure Exams**: Set up exam schedules
4. **Run Detection**: Execute detection scripts
5. **View Reports**: Check detection results

## Getting Help

### View Available Commands

```bash
# Using Makefile
make help

# View Docker Compose services
docker-compose config --services
```

### Check Service Health

```bash
# All services
make health

# Individual checks
curl http://localhost:5000/api/health
curl http://localhost:3000
```

### Access Service Shells

```bash
# Backend shell
make shell-backend

# Script server shell
make shell-script

# MySQL shell
make shell-db
```

## Configuration

### Environment Variables

Edit `.env` file to customize:

```bash
# Database
DB_PASSWORD=your-secure-password

# Backend
JWT_SECRET=your-secret-key
PORT=5000

# Email (optional)
EMAIL_USER=your-email@gmail.com
EMAIL_PASSWORD=your-password

# Script Server
EXAM_ID=19
```

### Ports

Default ports (can be changed in `docker-compose.yml`):
- Frontend: 3000
- Backend: 5000
- MySQL: 3306
- Ollama: 11434

## Production Deployment

For production use:

1. **Change default passwords** in `.env`
2. **Generate secure JWT secret**: `openssl rand -base64 64`
3. **Enable HTTPS** using reverse proxy (Nginx/Traefik)
4. **Set up backups**: `make db-backup` (schedule with cron)
5. **Configure firewall** rules
6. **Monitor logs** regularly

## Support

- **Documentation**: See `README.Docker.md` for detailed info
- **Logs**: `docker-compose logs -f`
- **Status**: `docker-compose ps`
- **Health**: `make health`

## Success! 🎉

Your CCTV Monitoring System is now running!

Access the frontend at http://localhost:3000 and login with `admin` / `admin123`.
