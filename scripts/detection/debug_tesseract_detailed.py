#!/usr/bin/env python3
"""
Detailed Tesseract Debug - Shows exactly what Tesseract sees
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
    r"-c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-/ "
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

print("=" * 80)
print("DETAILED TESSERACT DEBUG")
print("=" * 80)

for img_path in images:
    print(f"\n{'='*80}")
    print(f"📷 Image: {img_path.name}")
    print(f"{'='*80}")
    
    # Load image
    img = cv2.imread(str(img_path))
    print(f"   Full image size: {img.shape}")
    
    # Get ROI
    crop = get_left_roi(img)
    print(f"   ROI crop size: {crop.shape}")
    
    # Save original crop
    crop_path = f"scripts/detection/debug_crop_{img_path.stem}.jpg"
    cv2.imwrite(crop_path, crop)
    print(f"   Saved original crop: {crop_path}")
    
    # Preprocess
    processed = preprocess_for_ocr(crop)
    print(f"   Preprocessed size: {processed.shape}")
    
    # Save preprocessed image
    debug_path = f"scripts/detection/debug_processed_{img_path.stem}.jpg"
    cv2.imwrite(debug_path, processed)
    print(f"   Saved preprocessed: {debug_path}")
    
    # Try different Tesseract PSM modes
    psm_modes = [
        (6, "Assume a single uniform block of text"),
        (7, "Treat the image as a single text line"),
        (8, "Treat the image as a single word"),
        (11, "Sparse text. Find as much text as possible"),
        (13, "Raw line. Treat as single text line, bypassing hacks"),
    ]
    
    print(f"\n   Testing different Tesseract PSM modes:")
    print(f"   {'-'*70}")
    
    for psm, desc in psm_modes:
        config = f"--oem 3 --psm {psm} -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-/ "
        try:
            # Get raw text
            raw_text = pytesseract.image_to_string(processed, config=config).strip()
            
            # Get detailed data
            data = pytesseract.image_to_data(
                processed, config=config,
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
            
            combined = "".join(texts).upper().replace(" ", "")
            avg_conf = sum(confs) / len(confs) if confs else 0
            valid = is_valid_cam_id(combined) if combined else False
            
            print(f"   PSM {psm:2d} ({desc[:40]:40s})")
            print(f"      Raw: '{raw_text[:50]}'")
            print(f"      Words: {texts}")
            print(f"      Combined: '{combined}' (conf={avg_conf:.1f})")
            print(f"      Valid ID: {valid}")
            print()
            
        except Exception as e:
            print(f"   PSM {psm:2d}: Error - {e}")
            print()

print("\n" + "=" * 80)
print("SUMMARY")
print("=" * 80)
print("Check the saved images:")
print("  - debug_crop_*.jpg       = Original ROI crop")
print("  - debug_processed_*.jpg  = After preprocessing (what Tesseract sees)")
print("=" * 80)
