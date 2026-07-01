import requests
import json
import hashlib
import cv2
import time
import numpy as np
from ultralytics import YOLO
import math
from collections import defaultdict
import os
import threading
import pandas as pd
import logging

# === Logging Setup ===
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# === Configuration ===
# Get project root (2 levels up from scripts/detection/)
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '../..'))
SAVE_FOLDER = os.path.join(PROJECT_ROOT, "results/proximity_detection_results")
os.makedirs(SAVE_FOLDER, exist_ok=True)
COOLDOWN_TIME = 90  # 1.5 minutes - CRITICAL: lower cooldown for priority detection
EXCEL_FILE = os.path.join(PROJECT_ROOT, "data/sheets/new_sheet6.xlsx")

class ProximityDetector:
    def __init__(self, model_path=None, proximity_threshold=100, confidence_threshold=0.45, save_violations=True, output_folder=None):
        if model_path is None:
            model_path = os.path.join(PROJECT_ROOT, "models/yolov10x.pt")
        if output_folder is None:
            output_folder = SAVE_FOLDER
        """
        Initialize the proximity detection system
        """
        self.model = YOLO(model_path)
        self.proximity_threshold = proximity_threshold
        self.confidence_threshold = confidence_threshold
        self.violation_history = defaultdict(int)
        self.alert_cooldown = defaultdict(float)
        self.save_violations = save_violations
        self.output_folder = output_folder
        self.frame_count = 0
        
        if self.save_violations:
            os.makedirs(self.output_folder, exist_ok=True)
            logging.info(f"Violation frames will be saved to: {self.output_folder}")
        
    def calculate_distance(self, box1, box2):
        x1, y1 = (box1[0] + box1[2]) / 2, (box1[1] + box1[3]) / 2
        x2, y2 = (box2[0] + box2[2]) / 2, (box2[1] + box2[3]) / 2
        distance = math.sqrt((x2 - x1)**2 + (y2 - y1)**2)
        return distance
    
    def get_box_bottom_center(self, box):
        x_center = (box[0] + box[2]) / 2
        y_bottom = box[3]
        return (x_center, y_bottom)
    
    def calculate_bottom_distance(self, box1, box2):
        x1, y1 = self.get_box_bottom_center(box1)
        x2, y2 = self.get_box_bottom_center(box2)
        distance = math.sqrt((x2 - x1)**2 + (y2 - y1)**2)
        return distance
    
    def detect_proximity_violations(self, frame):
        results = self.model(frame, conf=self.confidence_threshold, verbose=False)
        violations = []
        person_boxes = []
        
        for result in results:
            boxes = result.boxes
            if boxes is not None:
                for box in boxes:
                    if int(box.cls[0]) == 0:
                        x1, y1, x2, y2 = box.xyxy[0].cpu().numpy()
                        confidence = box.conf[0].cpu().numpy()
                        person_boxes.append([x1, y1, x2, y2, confidence])
        
        for i in range(len(person_boxes)):
            for j in range(i + 1, len(person_boxes)):
                distance = self.calculate_bottom_distance(person_boxes[i][:4], person_boxes[j][:4])
                if distance < self.proximity_threshold:
                    violations.append({
                        'person1': i,
                        'person2': j,
                        'distance': distance,
                        'boxes': (person_boxes[i][:4], person_boxes[j][:4])
                    })
        
        annotated_frame = self.annotate_frame(frame, person_boxes, violations)
        return annotated_frame, violations, len(person_boxes)
    
    def annotate_frame(self, frame, person_boxes, violations):
        annotated_frame = frame.copy()
        for i, box in enumerate(person_boxes):
            x1, y1, x2, y2, conf = box
            in_violation = any(v['person1'] == i or v['person2'] == i for v in violations)
            if in_violation:
                color = (0, 0, 255)  # Red for violation
                cv2.rectangle(annotated_frame, (int(x1), int(y1)), (int(x2), int(y2)), color, 2)
                label = f"Person {i+1}: {conf:.2f}"
                cv2.putText(annotated_frame, label, (int(x1), int(y1-10)), 
                           cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)
        
        for violation in violations:
            box1, box2 = violation['boxes']
            center1 = self.get_box_bottom_center(box1)
            center2 = self.get_box_bottom_center(box2)
            cv2.line(annotated_frame, 
                    (int(center1[0]), int(center1[1])), 
                    (int(center2[0]), int(center2[1])), 
                    (0, 0, 255), 3)
            mid_x = int((center1[0] + center2[0]) / 2)
            mid_y = int((center1[1] + center2[1]) / 2)
            distance_text = f"{violation['distance']:.1f}px"
            cv2.putText(annotated_frame, distance_text, (mid_x, mid_y), 
                       cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 2)
        
        return annotated_frame
    
    def save_violation_frame(self, frame, violations, device_id, camera_no):
        if not self.save_violations or not violations:
            return None
        self.frame_count += 1
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        filename = f"violation_{device_id}_{camera_no}_{timestamp}_{self.frame_count:06d}.jpg"
        filepath = os.path.join(self.output_folder, filename)
        cv2.imwrite(filepath, frame)
        logging.info(f"Violation frame saved to: {filepath}")
        return filepath
    
    def add_status_overlay(self, frame, violations, person_count, device_id, camera_no):
        overlay = frame.copy()
        cv2.rectangle(overlay, (10, 10), (400, 120), (0, 0, 0), -1)
        cv2.addWeighted(frame, 0.7, overlay, 0.3, 0, frame)
        status_color = (0, 0, 255) if violations else (0, 255, 0)
        status_text = f"PROXIMITY VIOLATION!" if violations else "All Clear"
        cv2.putText(frame, status_text, (20, 35), cv2.FONT_HERSHEY_SIMPLEX, 0.8, status_color, 2)
        cv2.putText(frame, f"Candidates Detected: {person_count}", (20, 60), 
                   cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)
        cv2.putText(frame, f"Violations: {len(violations)}", (20, 80), 
                   cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)
        cv2.putText(frame, f"Device: {device_id}, Cam: {camera_no}", (20, 100), 
                   cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)
        return frame
    
    def process_frame(self, frame, device_id="unknown", camera_no="unknown"):
        if frame is None or not isinstance(frame, np.ndarray):
            logging.error(f"[Device {device_id} - Camera {camera_no}] Invalid input frame")
            return None, [], 0
        annotated_frame, violations, person_count = self.detect_proximity_violations(frame)
        final_frame = self.add_status_overlay(annotated_frame, violations, person_count, device_id, camera_no)
        return final_frame, violations, person_count

print("Initializing ProximityDetector...")
proximity_detector = ProximityDetector(save_violations=True)

last_save_times = {}
violation_counts = {}  # Store violation counts for each camera

def load_camera_config():
    """Load camera configuration from Excel file, filter by Computer Lab"""
    try:
        df = pd.read_excel(EXCEL_FILE)
        logging.info(f"Loaded Excel file: {EXCEL_FILE}")
        logging.info(f"Columns found: {list(df.columns)}")
        logging.info(f"Total rows: {len(df)}")
        
        required_columns = ['device_id', 'camera_no', 'Scene Classification']
        if not all(col in df.columns for col in required_columns):
            logging.error(f"Required columns {required_columns} not found in Excel file")
            logging.error(f"Available columns: {list(df.columns)}")
            return [], None
        
        filtered_df = df[df['Scene Classification'].isin(['Computer Lab'])]
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
                'index': index
            })
        
        logging.info(f"Successfully loaded {len(camera_configs)} camera configurations (Computer Lab):")
        for config in camera_configs:
            logging.info(f"  Device ID: {config['device_id']}, Camera No: {config['camera_no']}")
        
        return camera_configs, df
        
    except FileNotFoundError:
        logging.error(f"Excel file '{EXCEL_FILE}' not found.")
        return [], None
    except Exception as e:
        logging.error(f"Error reading Excel file: {e}")
        return [], None

def detect_proximity_in_frame(frame, device_id="unknown", camera_no="unknown"):
    """Detect proximity violations in a frame and optionally save detections"""
    final_frame, violations, person_count = proximity_detector.process_frame(frame, device_id, camera_no)
    
    key = f"{device_id}_{camera_no}"
    now = time.time()
    time_elapsed = now - last_save_times.get(key, 0)
    
    violation_detected = len(violations) > 0
    if violation_detected:
        if time_elapsed >= COOLDOWN_TIME:
            saved_path = proximity_detector.save_violation_frame(final_frame, violations, device_id, camera_no)
            if saved_path:
                last_save_times[key] = now
                logging.info(f"[Device {device_id} - Camera {camera_no}] 💾 Proximity violation detected & saved: {saved_path}")
        else:
            remaining = int(COOLDOWN_TIME - time_elapsed)
            logging.info(f"[Device {device_id} - Camera {camera_no}] ⏳ Violation detected but cooldown active ({remaining}s left)")
    
    violation_counts[key] = violation_counts.get(key, 0) + len(violations)
    
    return final_frame, violation_detected, len(violations)

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

def process_rtsp_stream_with_proximity_detection(rtsp_url, device_id, camera_no, verbose=False):
    """
    Process RTSP stream with proximity detection
    """
    if verbose:
        logging.info(f"Starting proximity detection on RTSP stream...")
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
            logging.info("Starting proximity detection...")
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
                processed_frame, violation_detected, violation_count = detect_proximity_in_frame(
                    frame, device_id, camera_no
                )
                if frame_count % 50 == 0:
                    if violation_detected:
                        logging.warning(f"[Device {device_id} - Camera {camera_no}] 📜 Proximity violation detected! Count: {violation_count}")
                    else:
                        logging.info(f"[Device {device_id} - Camera {camera_no}] ✅ Monitoring active - No violation detected")
            if frame_count % (int(fps) * 60) == 0:
                logging.info(f"[Device {device_id} - Camera {camera_no}] 🔄 Processing: {elapsed_time:.1f}s elapsed ({frame_count} frames)")
            time.sleep(1)
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
            logging.info(f"\n[Device {device_id} - Camera {camera_no}] Starting proximity detection on RTSP URL: {url_to_process}")
            success = process_rtsp_stream_with_proximity_detection(url_to_process, device_id, camera_no, verbose=False)
            if success:
                logging.info(f"[Device {device_id} - Camera {camera_no}] Proximity detection completed successfully")
                key = f"{device_id}_{camera_no}"
                df.at[index, 'Violations Detected'] = violation_counts.get(key, 0)
            else:
                logging.error(f"[Device {device_id} - Camera {camera_no}] Failed to process RTSP stream for proximity detection")
                df.at[index, 'Violations Detected'] = 0
        else:
            logging.error(f"[Device {device_id} - Camera {camera_no}] No RTSP URLs found in the response")
            df.at[index, 'Violations Detected'] = 0
            
    except Exception as e:
        logging.error(f"[Device {device_id} - Camera {camera_no}] Error during camera detection: {e}")
        df.at[index, 'Violations Detected'] = 0
    
    try:
        df.to_excel(EXCEL_FILE, index=False)
        logging.info(f"[Device {device_id} - Camera {camera_no}] Updated Excel file with proximity detection results")
    except Exception as e:
        logging.error(f"[Device {device_id} - Camera {camera_no}] Error saving Excel file: {e}")

def main():
    """Main function to orchestrate the process"""
    logging.info("=== RTSP Proximity Detection with Excel Configuration ===\n")
    
    camera_configs, df = load_camera_config()
    
    logging.info(f"Found {len(camera_configs)} camera configurations for processing")
    
    if not camera_configs or df is None:
        logging.error(f"No camera configurations found. Please check {EXCEL_FILE}")
        return
    
    if 'Violations Detected' not in df.columns:
        logging.info(f"Creating new column 'Violations Detected' in Excel file")
        df['Violations Detected'] = -1
    else:
        logging.info("Resetting 'Violations Detected' to '-1' for non-processed cameras")
        df.loc[~df['Scene Classification'].isin(['Computer Lab']), 'Violations Detected'] = -1
    
    try:
        logging.info(f"Saving initial Excel file: {EXCEL_FILE}")
        df.to_excel(EXCEL_FILE, index=False)
        logging.info(f"Successfully initialized Excel file with 'Violations Detected' set to -1 for non-processed cameras")
    except Exception as e:
        logging.error(f"Error saving initial Excel file: {e}")
        return
    
    try:
        logging.info("Attempting authentication...")
        token = authenticate_and_get_token()
        logging.info("Authentication successful. Token obtained")
    except Exception as e:
        logging.error(f"Authentication failed: {e}")
        return
    
    logging.info(f"Starting proximity detection for {len(camera_configs)} cameras...")
    threads = []
    
    for config in camera_configs:
        device_id = config['device_id']
        camera_no = config['camera_no']
        index = config['index']
        
        logging.info(f"Starting thread for Device: {device_id}, Camera: {camera_no}")
        
        t = threading.Thread(
            target=start_camera_detection, 
            args=(device_id, camera_no, token, df, index),
            name=f"Camera-{device_id}-{camera_no}"
        )
        t.start()
        threads.append(t)
        time.sleep(1)
    
    logging.info(f"Started {len(threads)} camera detection threads")
    logging.info("Press Ctrl+C to stop all detections\n")
    
    try:
        for t in threads:
            t.join()
        logging.info("All camera detection threads completed")
    except KeyboardInterrupt:
        logging.info("\nInterrupt received. Stopping all camera detections...")
    
    logging.info("Finalizing Excel updates...")
    df.loc[~df['Scene Classification'].isin(['Computer Lab']), 'Violations Detected'] = -1
    
    try:
        logging.info(f"Saving final Excel file: {EXCEL_FILE}")
        df.to_excel(EXCEL_FILE, index=False)
        logging.info("Final update: Successfully saved Excel file with all proximity detection results")
    except Exception as e:
        logging.error(f"Error saving final Excel file: {e}")
    
    logging.info("All proximity detection processes completed")

if __name__ == "__main__":
    main()
