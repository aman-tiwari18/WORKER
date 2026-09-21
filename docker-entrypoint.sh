#!/bin/bash
set -e

echo "🚀 Starting Script Server..."

# In backend mode the worker uses the backend as its DB gateway (plan §4.0/§4.5B)
# and needs NO MySQL of its own — that is what lets it run on any host. Skip the
# DB gates entirely in that mode.
if [ "${DB_ACCESS_MODE:-direct}" = "backend" ]; then
  echo "🌐 DB_ACCESS_MODE=backend — skipping MySQL checks (DB via backend gateway)"
else
  echo "⏳ Waiting for MySQL to be ready..."
  until mysql -h"$DB_HOST" -u"$DB_USER" -p"$DB_PASSWORD" -e "SELECT 1" > /dev/null 2>&1; do
    echo "MySQL is unavailable - sleeping"
    sleep 2
  done
  echo "✅ MySQL is ready!"
fi

# Models: seed the (possibly bind-mounted, possibly empty) models dir from the copy
# baked into the image; fall back to the pinned download. sha256 verified.
echo "📦 Ensuring pinned YOLO models..."
python3 /app/fetch_models.py /app/models --from /opt/models || {
    echo "❌ Required model weights unavailable (image seed and download both failed)"; exit 1; }

# Test database connection (skipped in backend mode — no direct DB there)
if [ "${DB_ACCESS_MODE:-direct}" != "backend" ]; then
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
fi

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
