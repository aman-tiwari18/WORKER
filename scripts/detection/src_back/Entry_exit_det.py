import requests
import json
import hashlib
import cv2
import time
import torch
from ultralytics import YOLO
from datetime import datetime
import os
import threading
import pandas as pd
import logging

# === Logging Setup ===
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# === Configuration ===
# Get project root (2 levels up from scripts/detection/)
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '../..'))
SAVE_FOLDER = os.path.join(PROJECT_ROOT, "results/entry_exit_detection_results")
os.makedirs(SAVE_FOLDER, exist_ok=True)
COOLDOWN_TIME = 180  # 3 minutes between saves - optimized for 25 alerts/min
EXCEL_FILE = os.path.join(PROJECT_ROOT, "data/sheets/new_sheet1.xlsx")
MODEL_PATH = os.path.join(PROJECT_ROOT, "models/yolov10m.pt")
MIN_PERSONS = 2  # Minimum persons for Entry_exit detection - increased to reduce noise
CONF_THRESHOLD = 0.45  # Confidence threshold for person detection - increased for accuracy

print("Loading YOLO model...")
yolo_model = YOLO(MODEL_PATH)

last_save_times = {}
person_counts = {}  # Store person counts for each camera

def load_camera_config():
    """Load camera configuration from Excel file, filter by Entry_exit Room"""
    try:
        df = pd.read_excel(EXCEL_FILE)
        logging.info(f"Loaded Excel file: {EXCEL_FILE}")
        logging.info(f"Columns found: {list(df.columns)}")
        logging.info(f"Total rows: {len(df)}")
        
        # Check if required columns exist
        required_columns = ['device_id', 'camera_no', 'Scene Classification']
        if not all(col in df.columns for col in required_columns):
            logging.error(f"Required columns {required_columns} not found in Excel file")
            logging.error(f"Available columns: {list(df.columns)}")
            return [], None
        
        # Filter for Entry_exit Room or Computer Lab
        filtered_df = df[df['Scene Classification'].isin(['Entry','Exit'])]
        
        # Clean and process data
        filtered_df = filtered_df.dropna(subset=['device_id', 'camera_no'])
        filtered_df['device_id'] = filtered_df['device_id'].astype(str)
        filtered_df['camera_no'] = filtered_df['camera_no'].astype(int)
        
        camera_configs = []
        for index, row in filtered_df.iterrows():
            device_id = str(row['device_id']).strip()
            camera_no = int(row['camera_no'])
            camera_configs.append({
                'device_id': device_id,
                'camera_no': camera_no,
                'index': index  # Store index to update Excel later
            })
        
        logging.info(f"Successfully loaded {len(camera_configs)} camera configurations (['Entry','Exit']):")
        for config in camera_configs:
            logging.info(f"  Device ID: {config['device_id']}, Camera No: {config['camera_no']}")
        
        return camera_configs, df  # Return DataFrame for updating later
        
    except FileNotFoundError:
        logging.error(f"Excel file '{EXCEL_FILE}' not found.")
        return [], None
    except Exception as e:
        logging.error(f"Error reading Excel file: {e}")
        return [], None

def detect_persons_in_frame(frame, device_id="unknown", camera_no="unknown"):
    """Detect persons in a frame using YOLO and optionally save detections if Entry_exit threshold is met"""
    results = yolo_model(frame, conf=CONF_THRESHOLD, verbose=False)[0]
    person_detections = [box for box in results.boxes if int(box.cls[0]) == 0 and box.conf[0] >= CONF_THRESHOLD]
    person_count = len(person_detections)
    max_conf = float(max([box.conf[0] for box in person_detections], default=0))

    # Draw bounding boxes for detected persons
    for box in person_detections:
        x1, y1, x2, y2 = map(int, box.xyxy[0])
        conf = float(box.conf[0])
        cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 0, 255), 2)
        label = f"Person {conf:.2f}"
        cv2.putText(frame, label, (x1, y1 - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)

    # Add detection info to frame
    y_offset = 30
    cv2.putText(frame, f"Persons: {person_count}", (10, y_offset), 
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
    y_offset += 30
    cv2.putText(frame, f"Device: {device_id}, Cam: {camera_no}", (10, y_offset), 
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)

    # Save frame if Entry_exit detected and cooldown period passed
    key = f"{device_id}_{camera_no}"
    now = time.time()
    time_elapsed = now - last_save_times.get(key, 0)

    Entry_exit_detected = person_count >= MIN_PERSONS
    if Entry_exit_detected and time_elapsed >= COOLDOWN_TIME:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{SAVE_FOLDER}/{key}_{timestamp}_persons{person_count}_conf{max_conf:.2f}.jpg"
        cv2.imwrite(filename, frame)
        last_save_times[key] = now
        logging.info(f"[Device {device_id} - Camera {camera_no}] 💾 Entry_exit detected & saved: {filename}")
    elif Entry_exit_detected:
        remaining = int(COOLDOWN_TIME - time_elapsed)
        logging.info(f"[Device {device_id} - Camera {camera_no}] ⏳ Entry_exit detected but cooldown active ({remaining}s left)")

    # Update person count for this camera
    person_counts[key] = person_counts.get(key, 0) + person_count
    
    return frame, Entry_exit_detected, person_count

def generate_signature(userName, password, realm, randomKey):
    temp = hashlib.md5(password.encode()).hexdigest()
    temp = hashlib.md5((userName + temp).encode()).hexdigest()
    temp = hashlib.md5(temp.encode()).hexdigest()
    temp = hashlib.md5((userName + ":" + realm + ":" + temp).encode()).hexdigest()
    signature = hashlib.md5((temp + ":" + randomKey).encode()).hexdigest()
    return signature

def make_api_call(url, payload, headers):
    response = requests.post(url, headers=headers, data=json.dumps(payload))
    return response

def process_rtsp_stream_with_Entry_exit_detection(rtsp_url, device_id, camera_no, verbose=False):
    """
    Process RTSP stream with YOLO Entry_exit detection
    """
    if verbose:
        logging.info(f"Starting Entry_exit detection on RTSP stream...")
        logging.info(f"Device ID: {device_id}, Camera No: {camera_no}")
        logging.info(f"RTSP URL: {rtsp_url}")
    
    backends = [
        (cv2.CAP_FFMPEG, "FFmpeg"),
        (cv2.CAP_GSTREAMER, "GStreamer"), 
        (cv2.CAP_V4L2, "V4L2"),
        (cv2.CAP_ANY, "Any available")
    ]
    
    cap = None
    successful_backend = None
    
    for backend, backend_name in backends:
        if verbose:
            logging.info(f"Trying {backend_name} backend...")
        try:
            cap = cv2.VideoCapture(rtsp_url, backend)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 10000)
            cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000)
            if cap.isOpened():
                ret, test_frame = cap.read()
                if ret and test_frame is not None:
                    if verbose:
                        logging.info(f"Successfully opened RTSP stream with {backend_name} backend")
                    successful_backend = backend_name
                    break
                else:
                    if verbose:
                        logging.info(f"{backend_name} backend opened but cannot read frames")
                    cap.release()
                    cap = None
            else:
                if verbose:
                    logging.info(f"Failed to open with {backend_name} backend")
                cap.release()
                cap = None
        except Exception as e:
            if verbose:
                logging.error(f"Error with {backend_name} backend: {e}")
            if cap:
                cap.release()
                cap = None
    
    if cap is None or not cap.isOpened():
        logging.error(f"[Device {device_id} - Camera {camera_no}] Failed to open RTSP stream: {rtsp_url}")
        return False
    
    if verbose:
        logging.info(f"Stream opened successfully with {successful_backend} backend")
    
    fps = cap.get(cv2.CAP_PROP_FPS)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    if fps <= 0 or fps > 60:
        fps = 25.0
        if verbose:
            logging.info(f"Invalid FPS detected, using default: {fps}")
    if verbose:
        logging.info(f"Stream properties: {width}x{height} @ {fps} FPS")
    frame_count = 0
    start_time = time.time()
    fail_count = 0
    try:
        if verbose:
            logging.info("Starting Entry_exit detection...")
            logging.info("Press Ctrl+C to stop")
        while True:
            ret, frame = cap.read()
            if not ret:
                logging.warning(f"[Device {device_id} - Camera {camera_no}] Failed to receive frame")
                fail_count += 1
                if fail_count >= 3:
                    logging.error(f"[Device {device_id} - Camera {camera_no}] Max retries reached, stopping")
                    break
                time.sleep(3)
                continue
            fail_count = 0
            frame_count += 1
            current_time = time.time()
            elapsed_time = current_time - start_time
            if frame_count % 5 == 0:
                processed_frame, Entry_exit_detected, person_count = detect_persons_in_frame(
                    frame, device_id, camera_no
                )
                if frame_count % 50 == 0:
                    if Entry_exit_detected:
                        logging.info(f"[Device {device_id} - Camera {camera_no}] 👥 Entry_exit detected! Count: {person_count}")
                    else:
                        logging.info(f"[Device {device_id} - Camera {camera_no}] ✅ Monitoring active - No Entry_exit detected")
            if frame_count % (int(fps) * 60) == 0:
                logging.info(f"[Device {device_id} - Camera {camera_no}] 🔄 Processing: {elapsed_time:.1f}s elapsed ({frame_count} frames)")
            time.sleep(1)  # As in Entry_exit_det.py to control processing rate
    except KeyboardInterrupt:
        logging.info(f"[Device {device_id} - Camera {camera_no}] Detection interrupted by user")
    except Exception as e:
        logging.error(f"[Device {device_id} - Camera {camera_no}] Error during detection: {e}")
    finally:
        if cap:
            cap.release()
        cv2.destroyAllWindows()
        total_time = time.time() - start_time
        actual_fps = frame_count / total_time if total_time > 0 else 0
        logging.info(f"[Device {device_id} - Camera {camera_no}] Detection finished!")
        logging.info(f"[Device {device_id} - Camera {camera_no}] Total frames processed: {frame_count}")
        logging.info(f"[Device {device_id} - Camera {camera_no}] Total processing time: {total_time:.2f} seconds")
        logging.info(f"[Device {device_id} - Camera {camera_no}] Actual processing FPS: {actual_fps:.2f}")
    return True

def authenticate_and_get_token():
    """Authenticate and get token for API access"""
    logging.info("Starting authentication...")
    
    url = "https://d11.camview.io:443/admin/API/accounts/authorize"
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "Connection": "close",
        "HOST": "d11.camview.io:443"
    }
    payload = {
        "userName": "system",
        "ipAddress": "",
        "clientType": "WINPC_V1"
    }
    
    response = make_api_call(url, payload, headers)
    response_content = json.loads(response.content.decode('utf-8'))
    res_head = response.headers
    
    logging.info(f"Status Code 1: {response.status_code}")
    
    userName = "system"
    password = "Sasd@12345"
    realm = response_content["realm"]
    randomKey = response_content["randomKey"]
    publicKey = response_content["publickey"]
    cookie = res_head['Set-Cookie']
    signature = generate_signature(userName, password, realm, randomKey)
    
    payload2 = {
        "mac": '',
        "signature": signature,
        "userName": userName,
        "randomKey": randomKey,
        "publicKey": publicKey,
        "encryptType": "MD5",
        "ipAddress": "",
        "clientType": "WINPC_V1",
        "userType": "0"
    }
    
    headers2 = {
        "Content-Type": "application/json;charset=UTF-8",
        "Connection": "close",
        "Cookie": cookie
    }
    
    response2 = make_api_call(url, payload2, headers2)
    response_content2 = json.loads(response2.content.decode('utf-8'))
    logging.info(f"Status Code 2: {response2.status_code}")
    logging.info(f"Response Content 2: {response_content2}")
    
    token = response_content2["token"]
    return token

def start_camera_detection(device_id, camera_no, token, df, index):
    """Start detection for a specific camera and update Excel with results"""
    logging.info(f"\n[Device {device_id} - Camera {camera_no}] Starting camera detection...")
    
    url3 = f'https://d11.camview.io:443/admin/API/MTS/Video/StartVideo?token={token}'
    headers3 = {
        "Connection": "close",
        "Content-Type": "application/json;charset=UTF-8",
        "X-Subject-Token": token
    }
    payload3 = {
        "clientType": "WINPC_V1",
        "clientMac": '',
        "clientPushId": "",
        "project": "PSDK",
        "method": "MTS.Video.StartVideo",
        "data": {
            "streamType": "1",
            "optional": f"/admin/API/MTS/Video/StartVideo?token={token}",
            "trackId": "",
            "extend": "",
            "channelId": f"{device_id}$1$0${camera_no}",
            "keyCode": "",
            "planId": "",
            "dataType": "2",
            "enableRtsps": "0"
        }
    }
    
    try:
        response3 = make_api_call(url3, payload3, headers3)
        response_content3 = json.loads(response3.content.decode('utf-8'))
        logging.info(f"[Device {device_id} - Camera {camera_no}] Status Code 3: {response3.status_code}")
        logging.info(f"[Device {device_id} - Camera {camera_no}] Response Content 3: {response_content3}")
        
        if response3.status_code != 200 or 'data' not in response_content3:
            logging.error(f"[Device {device_id} - Camera {camera_no}] Failed to get video stream URL")
            return
        
        rtsp_urls = []
        if '|' in response_content3['data']['url']:
            urls = response_content3['data']['url'].split('|')
            if len(urls) >= 2 and urls[1].strip():
                rtsp_url = urls[1] + '?token=' + response_content3['data']['token']
                rtsp_urls.append(rtsp_url)
            else:
                logging.info(f"[Device {device_id} - Camera {camera_no}] Second URL not available, using first URL")
                rtsp_url = urls[0] + '?token=' + response_content3['data']['token']
                rtsp_urls.append(rtsp_url)
        else:
            rtsp_url = response_content3['data']['url'] + '?token=' + response_content3['data']['token']
            rtsp_urls.append(rtsp_url)
        
        logging.info(f"[Device {device_id} - Camera {camera_no}] Found {len(rtsp_urls)} RTSP URLs:")
        for i, url in enumerate(rtsp_urls):
            logging.info(f"  {i+1}. {url}")
        
        if rtsp_urls:
            url_to_process = rtsp_urls[0]
            logging.info(f"\n[Device {device_id} - Camera {camera_no}] Starting Entry_exit detection on RTSP URL: {url_to_process}")
            success = process_rtsp_stream_with_Entry_exit_detection(url_to_process, device_id, camera_no, verbose=False)
            if success:
                logging.info(f"Successfully processed Entry_exit detection for camera {device_id} - Camera {camera_no}")
                # Update Excel with person count
                key = f"{device_id}_{camera_no}"
                df.at[index, 'Persons Detected'] = person_counts.get(key, 0)
            else:
                logging.error(f"Failed to process RTSP stream for camera No {device_id}: Camera {camera_no}")
                df.at[index, 'Persons Detected'] = 0
        else:
            logging.error(f"[Device {device_id} - Camera {camera_no}] No RTSP URLs found in the response")
            df.at[index, 'Persons Detected'] = 0
            
    except Exception as e:
        logging.error(f"[Device {device_id} - Camera {camera_no}] Error during camera detection: {e}")
        df.at[index, 'Persons Detected'] = 0
    
    # Save updated DataFrame to Excel
    try:
        df.to_excel(EXCEL_FILE, index=False)
        logging.info(f"[Device {device_id} - Camera {camera_no}] Updated Excel file with Entry_exit detection results")
    except Exception as e:
        logging.error(f"[Device {device_id} - Camera {camera_no}] Error saving Excel file: {e}")
def main():
    """Main function to orchestrate the entire process"""
    logging.info("=== RTSP Entry_exit Detection with Excel Configuration ===\n")
    
    # Load camera configurations from Excel
    camera_configs, df = load_camera_config()
    
    if not camera_configs or df is None:
        logging.error("No camera configurations found. Please check your Excel file.")
        return
    
    # Initialize Persons Detected column
    if 'Persons Detected' not in df.columns:
        df['Persons Detected'] = -1  # Default to -1 for all
    else:
        # Set Persons Detected to -1 for cameras not in Entry_exit Room or Computer Lab
        df.loc[~df['Scene Classification'].isin(['Entry','Exit']), 'Persons Detected'] = -1
    
    # Save initial DataFrame with -1 for non-processed cameras
    try:
        df.to_excel(EXCEL_FILE, index=False)
        logging.info(f"Initialized Excel file with Persons Detected set to -1 for non-processed cameras")
    except Exception as e:
        logging.error(f"Error saving initial Excel file: {e}")
        return
    
    # Authenticate and get token
    try:
        token = authenticate_and_get_token()
        logging.info(f"Authentication successful. Token obtained.")
    except Exception as e:
        logging.error(f"Authentication failed: {e}")
        return
    
    # Start detection for all cameras in parallel
    logging.info(f"\nStarting Entry_exit detection for {len(camera_configs)} cameras...")
    threads = []  # Initialize threads list
    
    for config in camera_configs:
        device_id = config['device_id']
        camera_no = config['camera_no']
        index = config['index']
        
        t = threading.Thread(
            target=start_camera_detection, 
            args=(device_id, camera_no, token, df, index),
            name=f"Camera-{device_id}-{camera_no}"
        )
        t.start()
        threads.append(t)
        
        # Small delay between starting threads to avoid overwhelming the Entry_exit
        time.sleep(1)
    
    # Log and print after threads are created
    logging.info(f"Started {len(threads)} camera detection threads.")
    print(f"Started {len(threads)} camera detection threads.")
    logging.info("Press Ctrl+C to stop all detections.\n")
    
    # Wait for all threads to complete
    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        logging.info("\nInterrupt received. Stopping all camera detections...")
    
    # Final update: Ensure non-processed cameras still have -1
    df.loc[~df['Scene Classification'].isin(['Entry','Exit']), 'Persons Detected'] = -1
    
    # Final save of Excel file
    try:
        df.to_excel(EXCEL_FILE, index=False)
        logging.info(f"Final update: Saved Excel file with all Entry_exit detection results")
    except Exception as e:
        logging.error(f"Error saving final Excel file: {e}")
    
    logging.info("All camera detections completed.")
    print("All camera detections completed.")

if __name__ == "__main__":
    main()
