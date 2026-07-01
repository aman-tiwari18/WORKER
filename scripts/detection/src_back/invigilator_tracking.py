import os
import time
import cv2
import numpy as np
import pandas as pd
import argparse
import random
import traceback
from collections import deque, OrderedDict # Added OrderedDict
from ultralytics import YOLO
from datetime import datetime
import threading
from queue import Queue # Added Queue
import requests # Added
import json     # Added
import hashlib  # Added

# Constants from rtsp_auth.py (or make them configurable)
API_BASE_URL = "https://d11.camview.io:443"
AUTH_USER_NAME = "system"
AUTH_PASSWORD = "Sasd@12345"

# Helper functions from rtsp_auth.py
def generate_signature(userName, password, realm, randomKey):
    # Step 1: temp = md5(password)
    temp = hashlib.md5(password.encode()).hexdigest()
    # Step 2: temp = md5(userName + temp)
    temp = hashlib.md5((userName + temp).encode()).hexdigest()
    # Step 3: temp = md5(temp)
    temp = hashlib.md5(temp.encode()).hexdigest()
    # Step 4: temp = md5(userName + ":" + realm + ":" + temp)
    temp = hashlib.md5((userName + ":" + realm + ":" + temp).encode()).hexdigest()
    # Step 5: signature = md5(temp + ":" + randomKey)
    signature = hashlib.md5((temp + ":" + randomKey).encode()).hexdigest()
    return signature

# def make_api_call(url, payload, headers):
#     try:
#         # Removed timeout=10 to align with rtsp_auth.py and use default timeout behavior
#         response = requests.post(url, headers=headers, data=json.dumps(payload))
#         print(headers)
#         return response
#     except requests.exceptions.Timeout:
#         # This message might still appear if a default timeout is hit, or if requests.post is called with a timeout elsewhere.
#         print(f"API call to {url} timed out.")
#         return None
#     except requests.exceptions.ConnectionError:
#         print(f"API call to {url} failed due to connection error.")
#         return None
#     except requests.exceptions.RequestException as e: # Catch other request-related errors
#         print(f"API call to {url} failed at request level: {e}")
#         return None

def make_api_call(url, payload, headers):
    response = requests.post(url, headers=headers, data=json.dumps(payload))
    # print(response.headers)
    # response=json.loads(response.content.decode('utf-8')) # Reverted: Let caller parse
    return response


class StreamProcessor:
    def __init__(self, stream_id, rtsp_url, cam_name, reconnect_interval=5):
        self.stream_id = stream_id
        self.rtsp_url = rtsp_url
        self.cam_name = cam_name
        self.running = False
        self.cap = None
        self.frame_queue = Queue(maxsize=30)
        self.reconnect_interval = reconnect_interval
        self.last_reconnect_attempt = 0
        
    def start(self):
        self.running = True
        self.connect()
        threading.Thread(target=self._read_frames, daemon=True).start()
        
    def connect(self):
        try:
            if self.cap is not None:
                self.cap.release()
            self.cap = cv2.VideoCapture(self.rtsp_url)
            if not self.cap.isOpened():
                raise Exception(f"Failed to open stream {self.cam_name}")
            print(f"Successfully connected to camera {self.cam_name}")
        except Exception as e:
            print(f"Error connecting to camera {self.cam_name}: {str(e)}")
            
    def stop(self):
        self.running = False
        if self.cap:
            self.cap.release()
            
    def _read_frames(self):
        consecutive_failures = 0
        while self.running:
            try:
                if not self.cap.isOpened():
                    current_time = time.time()
                    if current_time - self.last_reconnect_attempt > self.reconnect_interval:
                        print(f"Attempting to reconnect to camera {self.cam_name}")
                        self.connect()
                        self.last_reconnect_attempt = current_time
                    time.sleep(1)
                    continue

                ret, frame = self.cap.read()
                if ret:
                    consecutive_failures = 0
                    if self.frame_queue.full():
                        self.frame_queue.get()
                    self.frame_queue.put((ret, frame))
                else:
                    consecutive_failures += 1
                    if consecutive_failures > 5:
                        print(f"Camera {self.cam_name} connection lost. Attempting to reconnect...")
                        self.connect()
                        consecutive_failures = 0
                    time.sleep(0.1)
            except Exception as e:
                print(f"Error in camera {self.cam_name}: {str(e)}")
                time.sleep(1)

class Track:
    def __init__(self, track_id, bbox):
        self.track_id = track_id
        self.bbox = bbox
        
    def update_bbox(self, bbox):
        self.bbox = bbox

class Tracker:
    def __init__(self):
        self.tracks = []
        self.track_id_count = 0

    def update(self, frame, detections, min_height):
        filtered_detections = [det for det in detections if det[5] >= min_height]
        
        if not self.tracks:
            for det in filtered_detections:
                self._add_new_track(det[:4])
        else:
            self._update_tracks(filtered_detections)

    def _add_new_track(self, bbox):
        self.tracks.append(Track(self.track_id_count, bbox))
        self.track_id_count += 1

    def _update_tracks(self, detections):
        if not detections:
            self.tracks = []
            return

        iou_matrix = np.zeros((len(self.tracks), len(detections)))
        for i, track in enumerate(self.tracks):
            for j, det in enumerate(detections):
                iou_matrix[i, j] = self._calculate_iou(track.bbox, det[:4])

        matched_tracks = set()
        matched_detections = set()
        
        while True:
            if np.max(iou_matrix) < 0.3:
                break
                
            i, j = np.unravel_index(np.argmax(iou_matrix), iou_matrix.shape)
            if iou_matrix[i, j] < 0.3:
                break
                
            matched_tracks.add(i)
            matched_detections.add(j)
            self.tracks[i].update_bbox(detections[j][:4])
            iou_matrix[i, :] = 0
            iou_matrix[:, j] = 0

        for j in range(len(detections)):
            if j not in matched_detections:
                self._add_new_track(detections[j][:4])

        self.tracks = [track for i, track in enumerate(self.tracks) if i in matched_tracks]

    def _calculate_iou(self, bbox1, bbox2):
        x1 = max(bbox1[0], bbox2[0])
        y1 = max(bbox1[1], bbox2[1])
        x2 = min(bbox1[2], bbox2[2])
        y2 = min(bbox1[3], bbox2[3])

        if x2 < x1 or y2 < y1:
            return 0.0

        intersection = (x2 - x1) * (y2 - y1)
        area1 = (bbox1[2] - bbox1[0]) * (bbox1[3] - bbox1[1])
        area2 = (bbox2[2] - bbox2[0]) * (bbox2[3] - bbox2[1])
        union = area1 + area2 - intersection

        return intersection / union if union > 0 else 0

class SimplifiedTracker(Tracker):
    def __init__(self, stream_id, cam_name, base_dir, static_threshold=35, history_length=10):
        super().__init__()
        self.stream_id = stream_id
        self.cam_name = cam_name
        self.position_history = {}    
        self.static_status = {}       
        self.visibility_status = {}   
        self.static_threshold = static_threshold  
        self.history_length = history_length
        self.invisible_notifications = OrderedDict()
        self.notification_duration = 3.0
        self.static_start_times = {}
        self.tracked_targets = set()
        
        self.base_dir = os.path.join(base_dir, f"{self.cam_name}")
        self.static_dir = os.path.join(self.base_dir, "static_person")
        self.invisible_dir = os.path.join(self.base_dir, "no_targets")
        self.annotated_dir = os.path.join(self.base_dir, "annotated")
        
        for directory in [self.static_dir, self.invisible_dir, self.annotated_dir]:
            os.makedirs(directory, exist_ok=True)
        
        self.static_frames_saved = {}
        self.last_invisible_save_time = 0
        self.last_static_save_times = {}
        self.no_target_start_time = None
        self.cooldown_period = 600  # 10 minutes cooldown between no-target alerts
        self.static_threshold_time = 600
        self.invisible_threshold_time = 20
        
        self.static_events_saved = set()

    def save_frame(self, frame, directory, prefix, annotated=False):
        try:
            if frame is None:
                raise ValueError("Frame is None")
                
            if not os.path.exists(directory):
                print(f"Debug: Creating directory {directory}")
                os.makedirs(directory, exist_ok=True)
                
            current_time = datetime.now().strftime("%Y%m%d_%H%M%S")
            annotation_str = "_annotated" if annotated else ""
            filename = f"{self.cam_name}_{prefix}{annotation_str}_{current_time}.jpg"
            filepath = os.path.join(directory, filename)
            
            print(f"Debug: Attempting to save frame to {filepath}")
            success = cv2.imwrite(filepath, frame)
            
            if not success:
                raise Exception("cv2.imwrite failed to save the frame")
                
            print(f"Camera {self.cam_name} - Successfully saved frame: {filepath}")
            return filepath
        except Exception as e:
            print(f"Error in save_frame for camera {self.cam_name}: {str(e)}")
            raise

    def check_and_save_static_frames(self, display_frame, original_frame, track_id):
        current_time = time.time()
        
        if track_id not in self.last_static_save_times:
            self.last_static_save_times[track_id] = 0
            
        if self.is_static(track_id):
            static_duration = current_time - self.static_start_times[track_id]
            static_event_id = f"{track_id}_{int(self.static_start_times[track_id])}"
            
            print(f"Debug: Camera {self.cam_name} - Track {track_id} static for {static_duration:.1f} seconds")
            
            if static_duration >= self.static_threshold_time:
                print(f"Debug: Camera {self.cam_name} - Static threshold reached for track {track_id}")
                
                if static_event_id not in self.static_events_saved:
                    print(f"Debug: Camera {self.cam_name} - Attempting to save static event {static_event_id}")
                    try:
                        original_path = self.save_frame(original_frame, self.static_dir, f"static_person_{track_id}")
                        annotated_path = self.save_frame(display_frame, self.annotated_dir, f"static_person_{track_id}", annotated=True)
                        
                        self.static_events_saved.add(static_event_id)
                        self.last_static_save_times[track_id] = current_time
                        
                        print(f"Debug: Camera {self.cam_name} - Successfully saved static event frames")
                        self.log_event("static_person", track_id, original_path, annotated_path)
                    except Exception as e:
                        print(f"Error saving static frames for camera {self.cam_name}: {str(e)}")

    def check_and_save_invisible_frames(self, frame):
        current_time = time.time()
        
        if not self.visibility_status:
            if self.no_target_start_time is None:
                self.no_target_start_time = current_time
                print(f"Debug: Camera {self.cam_name} - Starting no targets timer")
        else:
            if self.no_target_start_time is not None:
                print(f"Debug: Camera {self.cam_name} - Resetting no targets timer")
            self.no_target_start_time = None
            
        if self.no_target_start_time is not None:
            duration = current_time - self.no_target_start_time
            print(f"Debug: Camera {self.cam_name} - No targets for {duration:.1f} seconds")
            
            if (duration >= self.invisible_threshold_time and 
                current_time - self.last_invisible_save_time >= self.cooldown_period):
                print(f"Debug: Camera {self.cam_name} - Attempting to save no targets frame")
                try:
                    original_path = self.save_frame(frame, self.invisible_dir, "no_targets")
                    self.last_invisible_save_time = current_time
                    self.log_event("no_targets", None, original_path, None)
                    print(f"Debug: Camera {self.cam_name} - Successfully saved no targets frame")
                except Exception as e:
                    print(f"Error saving invisible frame for camera {self.cam_name}: {str(e)}")

    def log_event(self, event_type, track_id, original_path, annotated_path):
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "camera_name": self.cam_name,
            "event_type": event_type,
            "track_id": track_id,
            "original_frame": original_path,
            "annotated_frame": annotated_path
        }
        
        log_file = os.path.join(self.base_dir, "events.json")
        try:
            if os.path.exists(log_file):
                with open(log_file, 'r') as f:
                    logs = json.load(f)
            else:
                logs = []
            
            logs.append(log_entry)
            
            with open(log_file, 'w') as f:
                json.dump(logs, f, indent=4)
        except Exception as e:
            print(f"Error logging event: {str(e)}")

    def mark_visible(self, track_id):
        self.visibility_status[track_id] = True

    def mark_invisible(self, track_id):
        if track_id in self.visibility_status:
            self.visibility_status[track_id] = False

    def is_static(self, track_id):
        return self.static_status.get(track_id, False)

    def update_track_status(self, track_id, bbox):
        center = ((bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2)
        
        if track_id not in self.position_history:
            self.position_history[track_id] = deque(maxlen=self.history_length)
        
        self.position_history[track_id].append(center)
        
        if len(self.position_history[track_id]) >= self.history_length:
            positions = list(self.position_history[track_id])
            max_movement = max(
                np.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2)
                for (x1, y1), (x2, y2) in zip(positions[:-1], positions[1:])
            )
            
            is_currently_static = max_movement < self.static_threshold
            was_static = self.static_status.get(track_id, False)
            
            if is_currently_static and not was_static:
                self.static_start_times[track_id] = time.time()
            elif not is_currently_static and was_static:
                self.static_start_times.pop(track_id, None)
            
            self.static_status[track_id] = is_currently_static

    def draw_notifications(self, frame):
        current_time = time.time()
        y_position = 30
        font = cv2.FONT_HERSHEY_SIMPLEX
        
        expired_notifications = []
        for track_id, timestamp in self.invisible_notifications.items():
            if current_time - timestamp > self.notification_duration:
                expired_notifications.append(track_id)
            else:
                cv2.putText(frame, f"Track {track_id} lost", (10, y_position), 
                           font, 0.7, (0, 0, 255), 2)
                y_position += 30
        
        for track_id in expired_notifications:
            self.invisible_notifications.pop(track_id)

class MultiStreamTracker:
    def __init__(self, excel_path, base_dir, max_streams=10):
        self.streams = {}
        self.trackers = {}
        print("Loading YOLO model...")
        # Get the project root directory (3 levels up from this script)
        script_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(os.path.dirname(script_dir))
        model_path = os.path.join(project_root, "models", "yolov10m.pt")
        self.model = YOLO(model_path)
        self.colors = [(random.randint(0, 255), random.randint(0, 255), random.randint(0, 255)) 
                      for _ in range(10)]
        self.detection_threshold = 0.5
        self.min_height = 250
        self.stream_stats = {}
        self.max_streams = max_streams
        self.base_dir = base_dir
        
        self.token = self._authenticate()
        if not self.token:
            print("Failed to authenticate. Exiting.")
            exit(1)
            
        self.initialize_streams_from_excel(excel_path)

    def _authenticate(self):
        print("Starting authentication...")
        auth_url =  "https://d11.camview.io:443/admin/API/accounts/authorize" # Changed to direct string
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "Connection": "close",
            "HOST": "d11.camview.io:443"
        }
        payload1 = {
            "userName": "system",
            "ipAddress": "",
            "clientType": "WINPC_V1"
        }

        response1 = make_api_call(auth_url, payload1, headers)
        
        
        # if not response1: # If make_api_call returned None (due to timeout, connection error, etc.)
        #     print(f"Authentication step 1 failed: No response or request-level error for {auth_url}.")
        #     return None

        # # Check status code before attempting to parse or access headers
        # if response1.status_code != 200:
        #     print(f"Authentication step 1 failed. Status: {response1.status_code}. URL: {auth_url}")
        #     try:
        #         print(f"Response body: {response1.text[:500]}") # Print some of the error body
        #     except Exception as e_text:
        #         print(f"Could not get response text: {e_text}")
        #     return None
        
        try:
            # print(response1)
            # res_head1 = response1.headers # Get headers from the response object
            response_content1 = json.loads(response1.content.decode('utf-8')) # Reverted: Let caller parse # Parse JSON from response content
        except json.JSONDecodeError as e_json:
            print(f"Authentication step 1 succeeded (Status 200), but failed to decode JSON response from {auth_url}. Error: {e_json}")
            try:
                print(f"Response body: {response1.text[:500]}")
            except Exception as e_text:
                print(f"Could not get response text: {e_text}")
            return None
        
        print("Authentication step 1 successful.")

        realm = response_content1.get("realm")
        randomKey = response_content1.get("randomKey")
        publicKey = response_content1.get("publickey")
        res_head1 = response1.headers # Get headers from the response object

        if not all([realm, randomKey, publicKey]):
            print(f"Authentication step 1 failed: Missing realm, randomKey, or publicKey in response. Response: {response_content1}")
            return None

        cookie = res_head1.get('Set-Cookie')
        if not cookie:
            print(f"Authentication step 1 failed: Missing Set-Cookie in response headers.")
            return None
            
        # Hardcoded credentials for signature generation
        signature = generate_signature("system", "Sasd@12345", realm, randomKey)

        payload2 = {
            "mac": '',
            "signature": signature,
            "userName": "system",  # Hardcoded userName
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

        response2 = make_api_call(auth_url, payload2, headers2)

        if not response2: # If make_api_call returned None
            print(f"Authentication step 2 failed: No response or request-level error for {auth_url}.")
            return None

        if response2.status_code != 200:
            print(f"Authentication step 2 failed. Status: {response2.status_code}. URL: {auth_url}")
            try:
                print(f"Response body: {response2.text[:500]}")
            except Exception as e_text:
                print(f"Could not get response text: {e_text}")
            return None
            
        try:
            response_content2 = json.loads(response2.content.decode('utf-8')) # Parse JSON from response content
        except json.JSONDecodeError as e_json:
            print(f"Authentication step 2 succeeded (Status 200), but failed to decode JSON response from {auth_url}. Error: {e_json}")
            try:
                print(f"Response body: {response2.text[:500]}")
            except Exception as e_text:
                print(f"Could not get response text: {e_text}")
            return None
            
        print("Authentication step 2 successful.")
        print(response_content2)
        token = response_content2.get("token")
        if not token:
            print(f"Authentication step 2 failed: Token not found in response. Response: {response_content2}")
            return None
        return token

    def _get_rtsp_url_for_camera(self, device_id, camera_no):
        if not self.token:
            print("No token available for fetching RTSP URL.")
            return None

        print(f"Fetching RTSP URL for Device ID: {device_id}, Camera No: {camera_no}...")
        start_video_url = f'https://d11.camview.io:443/admin/API/MTS/Video/StartVideo?token={self.token}'
        headers = {
            "Connection": "close",
            "Content-Type": "application/json;charset=UTF-8",
            "X-Subject-Token": self.token
        }
        payload = {
            "clientType": "WINPC_V1",
            "clientMac": '',
            "clientPushId": "",
            "project": "PSDK",
            "method": "MTS.Video.StartVideo",
            "data": {
                "streamType": "1", # Main stream
                "optional": f"/admin/API/MTS/Video/StartVideo?token={self.token}",
                "trackId": "",
                "extend": "",
                "channelId": f"{device_id}$1$0${camera_no}",
                "keyCode": "",
                "planId": "",
                "dataType": "2", # RTSP
                "enableRtsps": "0" # Prefer RTSP
            }
        }
        
        response = make_api_call(start_video_url, payload, headers)
        # if not response or response.status_code != 200:
        #     print(f"Failed to start video for Cam {camera_no}. Status: {response.status_code if response else 'N/A'}")
        #     if response: print(f"Response: {response.text}")
        #     return None

        response_content = json.loads(response.content.decode('utf-8')) # Parse JSON from response content
        print(f"StartVideo API Response (Cam {camera_no}): {response_content}")

        if response_content.get('data', {}).get('url'):
            data_url = response_content['data']['url']
            data_token = response_content['data']['token']
            rtsp_url = ""
            if '|' in data_url:
                urls = data_url.split('|')
                # Prefer the second URL if available (often rtsps, but logic from rtsp_auth used rtsp)
                # The original rtsp_auth.py used urls[1] if available, which might be an RTSPS URL.
                # For simplicity and consistency with original extraction, let's try to get a valid RTSP URL.
                # The original code preferred urls[1] then urls[0].
                if len(urls) >= 2 and urls[1].strip(): 
                    rtsp_url = urls[1] 
                else:
                    rtsp_url = urls[0]
            else:
                rtsp_url = data_url
            
            if not rtsp_url.startswith("rtsp://"): # Ensure it's an RTSP URL
                 # Fallback or specific selection if primary is not RTSP
                if '|' in data_url:
                    urls = data_url.split('|')
                    if urls[0].startswith("rtsp://"): rtsp_url = urls[0]
                    elif len(urls) > 1 and urls[1].startswith("rtsp://"): rtsp_url = urls[1]
                    else: # If neither is clearly RTSP, take the first and hope for the best or log error
                        print(f"Warning: Could not find a clear RTSP url in {data_url} for Cam {camera_no}")
                        # rtsp_url = urls[0] # Default to first if unsure
                elif not data_url.startswith("rtsp://"):
                     print(f"Warning: URL {data_url} for Cam {camera_no} does not look like an RTSP URL.")


            final_rtsp_url = f"{rtsp_url}?token={data_token}"
            print(f"Successfully fetched RTSP URL for Cam {camera_no}: {final_rtsp_url}")
            return final_rtsp_url
        else:
            print(f"Error in StartVideo API response for Cam {camera_no}: {response_content.get('msg', 'Unknown error')}")
            return None

    def initialize_streams_from_excel(self, excel_path):
        try:
            print("Reading stream configurations from Excel...")
            df = pd.read_excel(excel_path)
            # Expecting 'cam_name', 'device_id', 'camera_no', 'label'
            required_columns = ['Camera Name', 'device_id', 'camera_no', 'Scene Classification']
            if not all(col in df.columns for col in required_columns):
                raise ValueError(f"Excel file must contain {', '.join(required_columns)} columns")

            # Filter for 'Computer Lab'
            df_filtered = df[df['Scene Classification'] == 'Computer Lab']

            print(f"Found {len(df_filtered)} streams labeled as 'Computer Lab'.")
            print(f"\\nInitializing {len(df_filtered)} streams...")
            
            for index, row in df_filtered.iterrows():
                stream_id = str(index)
                cam_name = str(row['Camera Name'])
                device_id = str(row['device_id'])
                camera_no = str(row['camera_no']) # Ensure camera_no is string for API
                
                print(f"Attempting to get RTSP URL for: Cam Name='{cam_name}', Device ID='{device_id}', Camera No='{camera_no}'")
                rtsp_url = self._get_rtsp_url_for_camera(device_id, camera_no)
                print(rtsp_url)
                if rtsp_url:
                    self.streams[stream_id] = StreamProcessor(stream_id, rtsp_url, cam_name)
                    self.trackers[stream_id] = SimplifiedTracker(stream_id, cam_name, self.base_dir) # Pass base_dir here
                    self.stream_stats[stream_id] = {
                        'frames_processed': 0,
                        'detections': 0,
                        'static_events': 0, # Assuming this is managed by SimplifiedTracker or elsewhere
                        'start_time': time.time()
                    }
                    print(f"Stream '{cam_name}' initialized with RTSP URL: {rtsp_url}")
                else:
                    print(f"Could not initialize stream for Cam Name='{cam_name}' (Device ID='{device_id}', Camera No='{camera_no}') due to missing RTSP URL.")
            
            if not self.streams:
                # This check might be too strict if some streams fail but others succeed.
                # Consider if partial success is acceptable.
                print("Warning: No streams were successfully initialized after attempting to fetch RTSP URLs.")
                # raise ValueError("No streams could be initialized from the provided Excel file after fetching RTSP URLs.")
                
        except Exception as e:
            print(f"Error initializing streams from Excel: {str(e)}")
            traceback.print_exc()
            # Decide if to exit or continue if some streams might be running
            # exit(1) # Or handle more gracefully

    def process_frame(self, stream_id, frame):
        try:
            if frame is None:
                print(f"Warning: Received None frame from stream {stream_id}")
                return None

            print(f"\nProcessing frame for camera {self.streams[stream_id].cam_name}")
            display_frame = frame.copy()
            current_visible_ids = set()
            
            # Run YOLO detection
            results = self.model(frame, verbose=False)
            tracker = self.trackers[stream_id]
            
            detections_count = 0
            for result in results:
                detections = []
                boxes = result.boxes.data.tolist()
                print(f"Found {len(boxes)} potential detections")
                
                for r in boxes:
                    x1, y1, x2, y2, score, class_id = r
                    x1, x2, y1, y2 = map(int, (x1, x2, y1, y2))
                    class_id = int(class_id)
                    if score > self.detection_threshold and class_id == 0:  # class 0 is person
                        height = y2 - y1
                        if height >= self.min_height:
                            detections.append([x1, y1, x2, y2, score, height])
                            detections_count += 1
                            # print(f"Valid person detection: height={height}, score={score:.2f}")

                print(f"Processing {len(detections)} valid detections")
                tracker.update(display_frame, detections, self.min_height)

                for track in tracker.tracks:
                    track_id = track.track_id
                    current_visible_ids.add(track_id)
                    tracker.mark_visible(track_id)
                    tracker.update_track_status(track_id, track.bbox)
                    
                    # Debug static person checks
                    if tracker.is_static(track_id):
                        static_time = time.time() - tracker.static_start_times.get(track_id, time.time())
                        print(f"Track {track_id} is static for {static_time:.1f} seconds")
                        
                    print(f"Checking static frames for track {track_id}")
                    tracker.check_and_save_static_frames(display_frame, frame, track_id)
                    
                    # Draw bounding boxes and labels
                    bbox = track.bbox
                    x1, y1, x2, y2 = map(int, bbox)
                    height = y2 - y1
                    
                    color = (0, 0, 255) if tracker.is_static(track_id) else self.colors[track_id % len(self.colors)]
                    cv2.rectangle(display_frame, (x1, y1), (x2, y2), color, 2)
                    
                    status = "Moving"
                    if tracker.is_static(track_id):
                        static_duration = time.time() - tracker.static_start_times[track_id]
                        status = f"Static ({static_duration:.1f}s)"
                    
                    label = f"ID: {track_id}, H: {height}, {status}"
                    cv2.putText(display_frame, label, (x1, y1 - 10), 
                              cv2.FONT_HERSHEY_SIMPLEX, 0.9, color, 2)

            # Update visibility status
            print(f"Current visible IDs: {current_visible_ids}")
            for track_id in tracker.visibility_status.keys():
                if track_id not in current_visible_ids:
                    print(f"Track {track_id} became invisible")
                    tracker.mark_invisible(track_id)

            print("Checking for invisible frames")
            tracker.check_and_save_invisible_frames(display_frame)
            tracker.draw_notifications(display_frame)
            
            self.update_stats(stream_id, detections_count)
            return display_frame
            
        except Exception as e:
            print(f"Error processing frame for camera {self.streams[stream_id].cam_name}: {str(e)}")
            import traceback
            traceback.print_exc()
            return frame

    def start(self):
        try:
            print("\nStarting Multi-Stream Tracker...")
            print(f"Base directory: {os.path.abspath(self.base_dir)}")
            
            # Check directories exist
            if not os.path.exists(self.base_dir):
                print(f"Creating base directory: {self.base_dir}")
                os.makedirs(self.base_dir)
            
            # Start all stream processors
            for stream_id, stream in self.streams.items():
                print(f"\nInitializing camera {stream.cam_name}:")
                print(f"- Stream ID: {stream_id}")
                print(f"- RTSP URL: {stream.rtsp_url}")
                
                # Create camera-specific directories
                cam_dir = os.path.join(self.base_dir, stream.cam_name)
                print(f"- Creating camera directory: {cam_dir}")
                os.makedirs(cam_dir, exist_ok=True)
                
                # Start the stream
                stream.start()
                print(f"- Started camera {stream.cam_name}")

            print("\nAll streams started. Processing frames...")
            
            # Process frames from each stream
            while True:
                for stream_id, stream in self.streams.items():
                    if not stream.frame_queue.empty():
                        ret, frame = stream.frame_queue.get()
                        if ret:
                            processed_frame = self.process_frame(stream_id, frame)
                time.sleep(0.1)  # Small delay to prevent CPU overload
                
        except KeyboardInterrupt:
            print("\nReceived interrupt signal...")
        except Exception as e:
            print(f"\nError in main loop: {str(e)}")
            import traceback
            traceback.print_exc()
        finally:
            print("\nShutting down streams...")
            for stream in self.streams.values():
                stream.stop()
            print("\nTracking complete")

    def update_stats(self, stream_id, detections_count):
        stats = self.stream_stats[stream_id]
        stats['frames_processed'] += 1
        stats['detections'] += detections_count
        
        if stats['frames_processed'] % 100 == 0:
            elapsed_time = time.time() - stats['start_time']
            fps = stats['frames_processed'] / elapsed_time
            print(f"\nCamera {self.streams[stream_id].cam_name} Statistics:")
            print(f"Frames Processed: {stats['frames_processed']}")
            print(f"Average FPS: {fps:.2f}")
            print(f"Total Detections: {stats['detections']}")
            print(f"Static Events: {stats['static_events']}\n")

def main():
    parser = argparse.ArgumentParser(description="Multi-Stream Tracker")
    parser.add_argument("excel_path", type=str, help="Path to the Excel file containing stream information")
    parser.add_argument("base_dir", type=str, help="Base directory to store results")
    args = parser.parse_args()
    
    # Create base directories if they don't exist
    os.makedirs(args.base_dir, exist_ok=True)
    
    # Initialize and start the tracker with the Excel file path and base directory
    tracker = MultiStreamTracker(args.excel_path, args.base_dir)
    tracker.start()

if __name__ == "__main__":
    main()
