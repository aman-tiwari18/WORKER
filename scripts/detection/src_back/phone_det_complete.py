import requests
import json
import hashlib
import cv2
import time
import numpy as np
import torch
import torchvision.transforms as transforms
from torchvision import models
from PIL import Image
from ultralytics import YOLO
from datetime import datetime
import os
import threading
import pandas as pd

# === Configuration ===
# Get project root (2 levels up from scripts/detection/)
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '../..'))
SAVE_FOLDER = os.path.join(PROJECT_ROOT, "results/phone_detection_results")
os.makedirs(SAVE_FOLDER, exist_ok=True)
COOLDOWN_TIME = 480  # 8 minutes between saves - optimized for 25 alerts/min
EXCEL_FILE = os.path.join(PROJECT_ROOT, "data/sheets/new_sheet4.xlsx")

print("Loading YOLO model...")
yolo_model = YOLO(os.path.join(PROJECT_ROOT, "models/yolov10m.pt"))

print("Loading Places365 model...")
places_model = models.resnet18()
num_ftrs = places_model.fc.in_features
places_model.fc = torch.nn.Linear(num_ftrs, 365)

try:
    checkpoint = torch.hub.load_state_dict_from_url(
        'http://places2.csail.mit.edu/models_places365/resnet18_places365.pth.tar',
        map_location=torch.device('cpu'),
        progress=True
    )
    state_dict = {k.replace('module.', ''): v for k, v in checkpoint['state_dict'].items()}
    places_model.load_state_dict(state_dict)
    places_model.eval()
    print("Places365 model loaded successfully")
except Exception as e:
    print(f"Warning: Could not load Places365 model: {e}")
    places_model = None

# Load Places365 categories
try:
    with open("categories_places365.txt") as f:
        places_classes = [line.strip().split(' ')[0][3:] for line in f]
except FileNotFoundError:
    print("Warning: categories_places365.txt not found. Scene classification disabled.")
    places_classes = []

transform_places = transforms.Compose([
    transforms.Resize((256, 256)),
    transforms.CenterCrop(224),
    transforms.ToTensor(),
    transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])
])

last_save_times = {}
phone_counts = {}  # Store phone counts for each camera

def load_camera_config():
    """Load camera configuration from Excel file, filter by Server Room or None"""
    try:
        df = pd.read_excel(EXCEL_FILE)
        print(f"Loaded Excel file: {EXCEL_FILE}")
        print(f"Columns found: {list(df.columns)}")
        print(f"Total rows: {len(df)}")
        
        # Check if required columns exist
        required_columns = ['device_id', 'camera_no', 'Scene Classification']
        if not all(col in df.columns for col in required_columns):
            print(f"Error: Required columns {required_columns} not found in Excel file")
            print(f"Available columns: {list(df.columns)}")
            return []
        
        # Filter for Server Room or None (assuming None indicates Computer Lab)
        filtered_df = df[df['Scene Classification'].isin(['Server Room', 'Computer Lab'])]
        
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
        
        print(f"Successfully loaded {len(camera_configs)} camera configurations (Server Room or Computer Lab):")
        for config in camera_configs:
            print(f"  Device ID: {config['device_id']}, Camera No: {config['camera_no']}")
        
        return camera_configs, df  # Return DataFrame for updating later
        
    except FileNotFoundError:
        print(f"Error: Excel file '{EXCEL_FILE}' not found.")
        return [], None
    except Exception as e:
        print(f"Error reading Excel file: {e}")
        return [], None

def classify_scene(frame):
    """Classify the scene using Places365 model"""
    if places_model is None or not places_classes:
        return ["scene_classification_disabled"]
    
    try:
        img_pil = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)).convert("RGB")
        input_tensor = transform_places(img_pil).unsqueeze(0)
        with torch.no_grad():
            output = places_model(input_tensor)
            topk = torch.topk(output, 3)
            return [places_classes[idx] for idx in topk[1][0]]
    except Exception as e:
        print(f"Error in scene classification: {e}")
        return ["classification_error"]

def detect_phones_in_frame(frame, device_id="unknown", camera_no="unknown"):
    """Detect phones in a frame using YOLO and optionally save detections"""
    scene_labels = classify_scene(frame)
    results = yolo_model(frame, conf=0.25, verbose=True)[0]
    names = results.names

    phone_detected = False
    phone_count = 0
    
    # Draw bounding boxes for detected phones
    for box in results.boxes:
        cls_id = int(box.cls)
        if names[cls_id] == "cell phone":
            phone_detected = True
            phone_count += 1
            x1, y1, x2, y2 = map(int, box.xyxy[0])
            conf = float(box.conf)
            cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 0, 255), 2)
            label = f"Phone {conf:.2f}"
            cv2.putText(frame, label, (x1, y1 - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)

    # Add scene and detection info to frame
    y_offset = 30
    cv2.putText(frame, f"Scene: {', '.join(scene_labels[:2])}", (10, y_offset), 
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
    y_offset += 30
    cv2.putText(frame, f"Phones: {phone_count}", (10, y_offset), 
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
    y_offset += 30
    cv2.putText(frame, f"Device: {device_id}, Cam: {camera_no}", (10, y_offset), 
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)

    # Save frame if phone detected and cooldown period passed
    key = f"{device_id}_{camera_no}"
    now = time.time()
    time_elapsed = now - last_save_times.get(key, 0)

    if phone_detected:
        if time_elapsed >= COOLDOWN_TIME:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{SAVE_FOLDER}/phone_detected_{device_id}_{camera_no}_{timestamp}.jpg"
            cv2.imwrite(filename, frame)
            last_save_times[key] = now
            print(f"[Device {device_id} - Camera {camera_no}] 💾 Phone detected & saved: {filename}")
        else:
            remaining = int(COOLDOWN_TIME - time_elapsed)
            print(f"[Device {device_id} - Camera {camera_no}] ⏳ Phone detected but cooldown active ({remaining}s left)")
    
    # Update phone count for this camera
    phone_counts[key] = phone_counts.get(key, 0) + phone_count
    
    return frame, phone_detected, phone_count

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

def process_rtsp_stream_with_phone_detection(rtsp_url, device_id, camera_no, verbose=False):
    """
    Process RTSP stream with YOLO phone detection
    """
    if verbose:
        print(f"Starting phone detection on RTSP stream...")
        print(f"Device ID: {device_id}, Camera No: {camera_no}")
        print(f"RTSP URL: {rtsp_url}")
    
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
            print(f"Trying {backend_name} backend...")
        try:
            cap = cv2.VideoCapture(rtsp_url, backend)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 10000)
            cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000)
            if cap.isOpened():
                ret, test_frame = cap.read()
                if ret and test_frame is not None:
                    if verbose:
                        print(f"Successfully opened RTSP stream with {backend_name} backend")
                    successful_backend = backend_name
                    break
                else:
                    if verbose:
                        print(f"{backend_name} backend opened but cannot read frames")
                    cap.release()
                    cap = None
            else:
                if verbose:
                    print(f"Failed to open with {backend_name} backend")
                cap.release()
                cap = None
        except Exception as e:
            if verbose:
                print(f"Error with {backend_name} backend: {e}")
            if cap:
                cap.release()
                cap = None
    
    if cap is None or not cap.isOpened():
        print(f"[Device {device_id} - Camera {camera_no}] Failed to open RTSP stream: {rtsp_url}")
        return False
    
    if verbose:
        print(f"Stream opened successfully with {successful_backend} backend")
    
    fps = cap.get(cv2.CAP_PROP_FPS)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    if fps <= 0 or fps > 60:
        fps = 25.0
        if verbose:
            print(f"Invalid FPS detected, using default: {fps}")
    if verbose:
        print(f"Stream properties: {width}x{height} @ {fps} FPS")
    frame_count = 0
    start_time = time.time()
    try:
        if verbose:
            print("Starting phone detection...")
            print("Press Ctrl+C to stop")
        while True:
            ret, frame = cap.read()
            if not ret:
                print(f"[Device {device_id} - Camera {camera_no}] Failed to receive frame. Stream may have ended.")
                break
            frame_count += 1
            current_time = time.time()
            elapsed_time = current_time - start_time
            if frame_count % 5 == 0:
                processed_frame, phone_detected, phone_count = detect_phones_in_frame(
                    frame, device_id, camera_no
                )
                if frame_count % 50 == 0:
                    if phone_detected:
                        print(f"[Device {device_id} - Camera {camera_no}] 📱 Phone detected! Count: {phone_count}")
                    else:
                        print(f"[Device {device_id} - Camera {camera_no}] ✅ Monitoring active - No phone detected")
            if frame_count % (int(fps) * 60) == 0:
                print(f"[Device {device_id} - Camera {camera_no}] 🔄 Processing: {elapsed_time:.1f}s elapsed ({frame_count} frames)")
    except KeyboardInterrupt:
        print(f"[Device {device_id} - Camera {camera_no}] Detection interrupted by user.")
    except Exception as e:
        print(f"[Device {device_id} - Camera {camera_no}] Error during detection: {e}")
    finally:
        if cap:
            cap.release()
        cv2.destroyAllWindows()
        total_time = time.time() - start_time
        actual_fps = frame_count / total_time if total_time > 0 else 0
        print(f"[Device {device_id} - Camera {camera_no}] Detection finished!")
        print(f"[Device {device_id} - Camera {camera_no}] Total frames processed: {frame_count}")
        print(f"[Device {device_id} - Camera {camera_no}] Total processing time: {total_time:.2f} seconds")
        print(f"[Device {device_id} - Camera {camera_no}] Actual processing FPS: {actual_fps:.2f}")
    return True

def authenticate_and_get_token():
    """Authenticate and get token for API access"""
    print("Starting authentication...")
    
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
    
    print("Status Code 1:", response.status_code)
    
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
    print("Status Code 2:", response2.status_code)
    print("Response Content 2:", response_content2)
    
    token = response_content2["token"]
    return token

def start_camera_detection(device_id, camera_no, token, df, index):
    """Start detection for a specific camera and update Excel with results"""
    print(f"\n[Device {device_id} - Camera {camera_no}] Starting camera detection...")
    
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
        print(f"[Device {device_id} - Camera {camera_no}] Status Code 3:", response3.status_code)
        print(f"[Device {device_id} - Camera {camera_no}] Response Content 3:", response_content3)
        
        if response3.status_code != 200 or 'data' not in response_content3:
            print(f"[Device {device_id} - Camera {camera_no}] Failed to get video stream URL")
            return
        
        rtsp_urls = []
        if '|' in response_content3['data']['url']:
            urls = response_content3['data']['url'].split('|')
            if len(urls) >= 2 and urls[1].strip():
                rtsp_url = urls[1] + '?token=' + response_content3['data']['token']
                rtsp_urls.append(rtsp_url)
            else:
                print(f"[Device {device_id} - Camera {camera_no}] Second URL not available, using first URL")
                rtsp_url = urls[0] + '?token=' + response_content3['data']['token']
                rtsp_urls.append(rtsp_url)
        else:
            rtsp_url = response_content3['data']['url'] + '?token=' + response_content3['data']['token']
            rtsp_urls.append(rtsp_url)
        
        print(f"[Device {device_id} - Camera {camera_no}] Found {len(rtsp_urls)} RTSP URLs:")
        for i, url in enumerate(rtsp_urls):
            print(f"  {i+1}. {url}")
        
        if rtsp_urls:
            url_to_process = rtsp_urls[0]
            print(f"\n[Device {device_id} - Camera {camera_no}] Starting phone detection on RTSP URL: {url_to_process}")
            success = process_rtsp_stream_with_phone_detection(url_to_process, device_id, camera_no, verbose=False)
            if success:
                print(f"[Device {device_id} - Camera {camera_no}] Phone detection completed successfully")
                # Update Excel with phone count
                key = f"{device_id}_{camera_no}"
                df.at[index, 'Phones Detected'] = phone_counts.get(key, 0)
            else:
                print(f"[Device {device_id} - Camera {camera_no}] Failed to process RTSP stream for phone detection")
                df.at[index, 'Phones Detected'] = 0
        else:
            print(f"[Device {device_id} - Camera {camera_no}] No RTSP URLs found in the response")
            df.at[index, 'Phones Detected'] = 0
            
    except Exception as e:
        print(f"[Device {device_id} - Camera {camera_no}] Error during camera detection: {e}")
        df.at[index, 'Phones Detected'] = 0
    
    # Save updated DataFrame to Excel
    try:
        df.to_excel(EXCEL_FILE, index=False)
        print(f"[Device {device_id} - Camera {camera_no}] Updated Excel file with phone detection results")
    except Exception as e:
        print(f"[Device {device_id} - Camera {camera_no}] Error saving Excel file: {e}")

def main():
    """Main function to orchestrate the entire process"""
    print("=== RTSP Phone Detection with Excel Configuration ===\n")
    
    # Load camera configurations from Excel
    camera_configs, df = load_camera_config()
    
    if not camera_configs or df is None:
        print("No camera configurations found. Please check your Excel file.")
        return
    
    # Add Phones Detected column if it doesn't exist
    if 'Phones Detected' not in df.columns:
        df['Phones Detected'] = 0
    
    # Authenticate and get token
    try:
        token = authenticate_and_get_token()
        print(f"Authentication successful. Token obtained.")
    except Exception as e:
        print(f"Authentication failed: {e}")
        return
    
    # Start detection for all cameras in parallel
    print(f"\nStarting phone detection for {len(camera_configs)} cameras...")
    threads = []
    
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
        
        # Small delay between starting threads to avoid overwhelming the server
        time.sleep(1)
    
    print(f"Started {len(threads)} camera detection threads.")
    print("Press Ctrl+C to stop all detections.\n")
    
    # Wait for all threads to complete
    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        print("\nInterrupt received. Stopping all camera detections...")
    
    # Final save of Excel file
    try:
        df.to_excel(EXCEL_FILE, index=False)
        print(f"Final update: Saved Excel file with all phone detection results")
    except Exception as e:
        print(f"Error saving final Excel file: {e}")
    
    print("All camera detections completed.")

if __name__ == "__main__":
    main()