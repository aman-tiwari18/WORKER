#!/usr/bin/env python3
"""
Debug script to see what Tesseract is extracting from the test images
"""
import cv2
import numpy as np
import pytesseract
import re
from pathlib import Path

# Config
ROI_Y1_FRAC  = 0.82
ROI_Y2_FRAC  = 0.97
ROI_LEFT_X1  = 0.00
ROI_LEFT_X2  = 0.45

TESS_CONFIG = (
    r"--oem 3 --psm 7 "
    r"-c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-/\\ "
)

CAM_ID_RE = re.compile(r"[\dA-Z]{3,}[_\-/\\][\dA-Z_\-/\\]{3,}", re.IGNORECASE)

def get_left_roi(img):
    h, w = img.shape[:2]
    y1 = int(h * ROI_Y1_FRAC)
    y2 = int(h * ROI_Y2_FRAC)
    x1 = int(w * ROI_LEFT_X1)
    x2 = int(w * ROI_LEFT_X2)
    return img[y1:y2, x1:x2]

def preprocess_for_ocr(crop_bgr):
    gray = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY)
    _, thresh = cv2.threshold(gray, 160, 255, cv2.THRESH_BINARY)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
    thresh = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, kernel)
    scaled = cv2.resize(thresh, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    return scaled

def is_valid_cam_id(text):
    return bool(CAM_ID_RE.search(text))

# Test images
test_dir = Path("scripts/detection/test_images")
images = list(test_dir.glob("*.jpg"))

print("=" * 70)
print("TESSERACT DEBUG - What is Tesseract actually seeing?")
print("=" * 70)

for img_path in images:
    print(f"\n📷 Image: {img_path.name}")
    
    # Load image
    img = cv2.imread(str(img_path))
    
    # Get ROI
    crop = get_left_roi(img)
    print(f"   ROI size: {crop.shape}")
    
    # Preprocess
    processed = preprocess_for_ocr(crop)
    
    # Save preprocessed image for inspection
    debug_path = f"scripts/detection/debug_tess_{img_path.stem}.jpg"
    cv2.imwrite(debug_path, processed)
    print(f"   Saved preprocessed: {debug_path}")
    
    # Run Tesseract
    try:
        data = pytesseract.image_to_data(
            processed, config=TESS_CONFIG,
            output_type=pytesseract.Output.DICT
        )
        
        texts, confs = [], []
        for i, conf in enumerate(data["conf"]):
            try:
                c = float(conf)
            except (ValueError, TypeError):
                continue
            t = str(data["text"][i]).strip()
            if c > 0 and t:
                texts.append(t)
                confs.append(c)
                print(f"      Word: '{t}' (conf={c:.1f})")
        
        if texts:
            combined = "".join(texts).upper().replace(" ", "")
            avg_conf = sum(confs) / len(confs)
            print(f"   ✓ Combined: '{combined}' (avg_conf={avg_conf:.1f})")
            print(f"   Valid ID? {is_valid_cam_id(combined)}")
        else:
            print(f"   ✗ No text detected by Tesseract")
            
    except Exception as e:
        print(f"   ✗ Error: {e}")

print("\n" + "=" * 70)
