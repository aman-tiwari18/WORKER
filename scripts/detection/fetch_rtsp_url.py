"""
RTSP URL Fetcher
================
Fetches cameras from database and displays all RTSP URLs returned by the servers.
Used for debugging and verifying what URLs are available from each camera.

Usage:
    python fetch_rtsp_urls.py --exam_id 19
"""

import argparse
import hashlib
import json
import logging
import os
import sys
import time
import urllib.parse
import requests
from datetime import datetime

# ============================================================
# DB Import
# ============================================================
try:
    from utils.db_client import get_rtsp_cameras
except ImportError:
    sys.path.append(os.path.dirname(os.path.abspath(__file__)))
    from utils.db_client import get_rtsp_cameras

# ============================================================
# Logging Setup
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# ============================================================
# Configuration
# ============================================================
TIMEOUT_API = 10
AUTH_USERNAME = "system"
AUTH_PASSWORD = "Sasd@12345"

# ============================================================
# Authentication
# ============================================================
def generate_signature(userName, password, realm, randomKey):
    temp = hashlib.md5(password.encode()).hexdigest()
    temp = hashlib.md5((userName + temp).encode()).hexdigest()
    temp = hashlib.md5(temp.encode()).hexdigest()
    temp = hashlib.md5((userName + ":" + realm + ":" + temp).encode()).hexdigest()
    return hashlib.md5((temp + ":" + randomKey).encode()).hexdigest()


def make_api_call(url, payload, headers, timeout=None):
    if timeout is None:
        timeout = TIMEOUT_API
    try:
        response = requests.post(
            url, headers=headers, data=json.dumps(payload), timeout=timeout
        )
        if response.status_code in (200, 401):
            return response
        return None
    except Exception as e:
        logger.error(f"API call failed: {e}")
        return None


def authenticate_server(server_address):
    """Two-step Camview authentication."""
    auth_url = f"https://{server_address}/admin/API/accounts/authorize"
    headers = {"Content-Type": "application/json"}
    payload = {"userName": AUTH_USERNAME, "ipAddress": "", "clientType": "WINPC_V1"}
    
    # Step 1: Get realm and randomKey
    res = make_api_call(auth_url, payload, headers)
    if res is None:
        return None
    
    try:
        resp_json = json.loads(res.content.decode("utf-8"))
    except Exception:
        return None
    
    if resp_json.get("code") not in (None, 1000):
        return None
    
    realm = resp_json.get("realm")
    randomKey = resp_json.get("randomKey")
    publicKey = resp_json.get("publickey")
    cookie = res.headers.get("Set-Cookie", "")
    
    if not all([realm, randomKey, publicKey]):
        return None
    
    # Step 2: Exchange signature for token
    sig = generate_signature(AUTH_USERNAME, AUTH_PASSWORD, realm, randomKey)
    payload2 = {
        "mac": "", "signature": sig, "userName": AUTH_USERNAME,
        "randomKey": randomKey, "publicKey": publicKey, "encryptType": "MD5",
        "ipAddress": "", "clientType": "WINPC_V1", "userType": "0"
    }
    headers2 = {"Content-Type": "application/json", "Cookie": cookie, "User-Agent": "WINPC_V1"}
    res2 = make_api_call(auth_url, payload2, headers2)
    
    if res2 is None:
        return None
    
    try:
        resp_json2 = json.loads(res2.content.decode("utf-8"))
    except Exception:
        return None
    
    if resp_json2.get("code") not in (None, 1000, 0):
        return None
    
    token = resp_json2.get("token")
    return token


def add_token_to_url(rtsp_url, token):
    """Append session token as ?token= query parameter."""
    return f"{rtsp_url}?token={token}"


def get_rtsp_urls_from_server(server_address, device_id, camera_no):
    """
    Authenticate with server and fetch all RTSP URLs for a camera.
    Returns: (list_of_urls, stream_token, success, error_message)
    """
    # Authenticate
    token = authenticate_server(server_address)
    if not token:
        return None, None, False, "Authentication failed"
    
    # Call StartVideo API
    url = f"https://{server_address}/admin/API/MTS/Video/StartVideo?token={token}"
    headers = {"Content-Type": "application/json", "X-Subject-Token": token}
    payload = {
        "clientType": "WINPC_V1",
        "project": "PSDK",
        "method": "MTS.Video.StartVideo",
        "data": {
            "streamType": "1",
            "optional": f"/admin/API/MTS/Video/StartVideo?token={token}",
            "channelId": f"{device_id}$1$0${camera_no}",
            "dataType": "2",
            "enableRtsps": "0"
        }
    }
    
    res = make_api_call(url, payload, headers)
    if res is None:
        return None, None, False, "StartVideo API call failed"
    
    if res.status_code == 401:
        return None, None, False, "Token expired or invalid"
    
    if res.status_code != 200:
        return None, None, False, f"API returned status {res.status_code}"
    
    try:
        resp_json = res.json()
        data = resp_json.get("data")
        if not data:
            return None, None, False, "No data in API response"
        
        # Collect ALL URLs from both "url" and "url2" fields
        urls = []
        for key in ("url", "url2"):
            raw = data.get(key)
            if not raw:
                continue
            # Split by pipe and decode
            parts = [part.strip() for part in raw.split('|') if part.strip()]
            urls.extend([urllib.parse.unquote(part) for part in parts])
        
        stream_token = data.get("token") or token
        
        if urls:
            return urls, stream_token, True, None
        return None, None, False, "No URLs found in response"
        
    except Exception as e:
        return None, None, False, f"Error parsing response: {e}"


def main():
    parser = argparse.ArgumentParser(description="Fetch RTSP URLs from camera servers")
    parser.add_argument("--exam_id", type=int, default=19, help="Exam ID (default: 19)")
    parser.add_argument("--limit", type=int, help="Limit total number of cameras to process")
    parser.add_argument("--per_server", type=int, default=2, help="Number of cameras to fetch per unique server (default: 2)")
    parser.add_argument("--server", type=str, help="Filter by specific server address (e.g., d11.camview.io:443)")
    parser.add_argument("--verbose", action="store_true", help="Show detailed output")
    args = parser.parse_args()
    
    exam_id = args.exam_id
    
    logger.info("=" * 80)
    logger.info(f"🔍 RTSP URL Fetcher - Exam ID: {exam_id}")
    logger.info("=" * 80)
    
    # Fetch cameras from database
    cameras = get_rtsp_cameras(exam_id)
    if not cameras:
        logger.warning(f"No RTSP cameras found for exam_id={exam_id}")
        return
    
    logger.info(f"Found {len(cameras)} cameras in database")
    
    # Filter by specific server if requested
    if args.server:
        cameras = [cam for cam in cameras if cam.get("cctv_url") == args.server]
        logger.info(f"Filtered to {len(cameras)} cameras for server: {args.server}")
    
    # Group cameras by server
    servers_map = {}
    for cam in cameras:
        server = cam.get("cctv_url")
        if server not in servers_map:
            servers_map[server] = []
        servers_map[server].append(cam)
    
    logger.info(f"Found {len(servers_map)} unique server(s)")
    for server, cams in servers_map.items():
        logger.info(f"  {server}: {len(cams)} cameras")
    logger.info("")
    
    # Sample cameras per server (default: 2 per server)
    selected_cameras = []
    logger.info(f"Sampling {args.per_server} camera(s) from each server:")
    for server, cams in servers_map.items():
        sample_size = min(args.per_server, len(cams))
        selected_cameras.extend(cams[:sample_size])
        logger.info(f"  ✓ {server}: {sample_size} camera(s)")
    cameras = selected_cameras
    logger.info(f"\nTotal cameras selected: {len(cameras)}")
    
    # Apply overall limit if specified
    if args.limit and len(cameras) > args.limit:
        cameras = cameras[:args.limit]
        logger.info(f"Applied overall limit: {args.limit} cameras")
    
    logger.info("=" * 80)
    logger.info("")
    
    # Process each camera
    results = []
    for idx, cam in enumerate(cameras, 1):
        server_address = cam.get("cctv_url")
        device_id = cam.get("device_id")
        camera_no = cam.get("camera_no")
        camera_name = cam.get("camera_name", "Unknown")
        centre_name = cam.get("centre_name", "Unknown")
        classification = cam.get("classification", "Unknown")
        
        logger.info(f"[{idx}/{len(cameras)}] Processing Camera:")
        logger.info(f"  Server      : {server_address}")
        logger.info(f"  Device ID   : {device_id}")
        logger.info(f"  Camera No   : {camera_no}")
        logger.info(f"  Name        : {camera_name}")
        logger.info(f"  Centre      : {centre_name}")
        logger.info(f"  Classification: {classification}")
        
        # Fetch URLs
        urls, stream_token, success, error = get_rtsp_urls_from_server(
            server_address, device_id, camera_no
        )
        
        if success and urls:
            logger.info(f"  ✅ SUCCESS - Found {len(urls)} URL(s):")
            for url_idx, url in enumerate(urls, 1):
                logger.info(f"     URL {url_idx}: {url}")
                if args.verbose and stream_token:
                    logger.info(f"     Token: {stream_token[:20]}...")
            
            results.append({
                'server': server_address,
                'device_id': device_id,
                'camera_no': camera_no,
                'camera_name': camera_name,
                'centre_name': centre_name,
                'classification': classification,
                'status': 'SUCCESS',
                'url_count': len(urls),
                'urls': urls
            })
        else:
            logger.error(f"  ❌ FAILED - {error}")
            results.append({
                'server': server_address,
                'device_id': device_id,
                'camera_no': camera_no,
                'camera_name': camera_name,
                'centre_name': centre_name,
                'classification': classification,
                'status': 'FAILED',
                'error': error
            })
        
        logger.info("")
        time.sleep(0.5)  # Small delay between cameras
    
    # Summary
    logger.info("=" * 80)
    logger.info("📊 SUMMARY")
    logger.info("=" * 80)
    
    success_count = sum(1 for r in results if r['status'] == 'SUCCESS')
    failed_count = len(results) - success_count
    
    logger.info(f"Total Cameras Processed: {len(results)}")
    logger.info(f"✅ Successful          : {success_count}")
    logger.info(f"❌ Failed              : {failed_count}")
    logger.info("")
    
    # URL count distribution
    url_counts = {}
    for r in results:
        if r['status'] == 'SUCCESS':
            count = r['url_count']
            url_counts[count] = url_counts.get(count, 0) + 1
    
    if url_counts:
        logger.info("URL Count Distribution:")
        for count in sorted(url_counts.keys()):
            logger.info(f"  {count} URL(s): {url_counts[count]} cameras")
        logger.info("")
    
    # Server-wise statistics
    server_stats = {}
    for r in results:
        srv = r['server']
        if srv not in server_stats:
            server_stats[srv] = {'total': 0, 'success': 0}
        server_stats[srv]['total'] += 1
        if r['status'] == 'SUCCESS':
            server_stats[srv]['success'] += 1
    
    logger.info("Server-wise Statistics:")
    for srv, stats in server_stats.items():
        rate = (stats['success'] / stats['total'] * 100) if stats['total'] else 0
        logger.info(f"  {srv}")
        logger.info(f"    Total: {stats['total']} | Success: {stats['success']} | Rate: {rate:.1f}%")
    
    logger.info("=" * 80)
    
    # Save results to JSON file
    output_file = f"rtsp_urls_exam_{exam_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    try:
        with open(output_file, 'w') as f:
            json.dump(results, f, indent=2)
        logger.info(f"✅ Results saved to: {output_file}")
    except Exception as e:
        logger.error(f"Failed to save results: {e}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.warning("\nInterrupted by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
