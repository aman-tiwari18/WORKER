#!/bin/bash
set -e

echo "🚀 Starting Script Server..."

# Wait for MySQL to be ready
echo "⏳ Waiting for MySQL to be ready..."
until mysql -h"$DB_HOST" -u"$DB_USER" -p"$DB_PASSWORD" -e "SELECT 1" > /dev/null 2>&1; do
  echo "MySQL is unavailable - sleeping"
  sleep 2
done

echo "✅ MySQL is ready!"

# Download YOLO models if not present
echo "📦 Checking YOLO models..."
if [ ! -f "models/yolov10m.pt" ]; then
    echo "Downloading YOLOv10m model..."
    mkdir -p models
    # You can add wget/curl commands here to download models
    # wget -O models/yolov10m.pt https://github.com/ultralytics/assets/releases/download/v0.0.0/yolov10m.pt
    echo "⚠️  Please manually place yolov10m.pt in the models directory"
fi

if [ ! -f "models/yolov10x.pt" ]; then
    echo "Downloading YOLOv10x model..."
    # wget -O models/yolov10x.pt https://github.com/ultralytics/assets/releases/download/v0.0.0/yolov10x.pt
    echo "⚠️  Please manually place yolov10x.pt in the models directory"
fi

# Test database connection
echo "🔍 Testing database connection..."
python3 -c "
import pymysql
import os
try:
    conn = pymysql.connect(
        host=os.environ.get('DB_HOST', 'mysql'),
        user=os.environ.get('DB_USER', 'root'),
        password=os.environ.get('DB_PASSWORD', 'rootpassword123'),
        database=os.environ.get('DB_NAME', 'cctv_monitoring')
    )
    print('✅ Database connection successful!')
    conn.close()
except Exception as e:
    print(f'❌ Database connection failed: {e}')
    exit(1)
"

echo "✅ Script Server initialization complete!"
echo "📝 Available scripts:"
echo "   - scripts/detection/vision_classification_rtsp.py"
echo "   - scripts/detection/qp_box_det.py"
echo "   - scripts/detection/crowd_zero_det.py"
echo "   - scripts/detection/close_proximity.py"
echo ""
echo "💡 To run a script:"
echo "   docker exec -it cctv_script_server python scripts/detection/vision_classification_rtsp.py --exam_id 19"

# Execute the main command
exec "$@"
