#!/usr/bin/env python3
"""
Test Camera ID Verification from Direct Images
===============================================
Helper script to test OCR camera ID extraction on saved frame images
without needing live camera streams.

USAGE:
    python test_camera_id_from_images.py \
        --images-dir ./test_frames \
        --expected-id "11394_REG-D_C01" \
        --output results.json

    # Or test multiple images with a CSV mapping
    python test_camera_id_from_images.py \
        --csv image_list.csv \
        --image-col "Image Path" \
        --id-col "Expected ID" \
        --output results.json

CSV Format:
    Image Path,Expected ID
    /path/to/frame1.jpg,11394_REG-D_C01
    /path/to/frame2.jpg,11395_LAB-A_C02
"""

import argparse
import base64
import json
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Tuple

import cv2
import numpy as np
import pandas as pd
import pytesseract

# EasyOCR — lazy import
try:
    import easyocr
    _EASYOCR_AVAILABLE = True
except ImportError:
    _EASYOCR_AVAILABLE = False

# Anthropic SDK — lazy import
try:
    import anthropic as _anthropic_sdk
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    _ANTHROPIC_AVAILABLE = False

# ══════════════════════════════════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════════════
ROI_Y1_FRAC  = 0.82
ROI_Y2_FRAC  = 0.97
ROI_LEFT_X1  = 0.00
ROI_LEFT_X2  = 0.45
ROI_RIGHT_X1 = 0.55
ROI_RIGHT_X2 = 1.00

CAM_ID_RE = re.compile(r"[\dA-Z]{3,}[_\-/\\][\dA-Z_\-/\\]{3,}", re.IGNORECASE)

TESS_CONFIG = r"--oem 3 --psm 6 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-/ "

CLAUDE_VISION_MODEL = "claude-sonnet-4-20250514"
EASYOCR_MIN_CONF = 0.35

_easyocr_reader = None
_anthropic_client = None

# ══════════════════════════════════════════════════════════════════════════
# INIT
# ══════════════════════════════════════════════════════════════════════════
def init_easyocr(gpu: bool = False):
    global _easyocr_reader
    if not _EASYOCR_AVAILABLE:
        log.warning("EasyOCR not installed — Stage 1 will be skipped.")
        return
    log.info(f"Loading EasyOCR model (gpu={gpu}) …")
    _easyocr_reader = easyocr.Reader(["en"], gpu=gpu, verbose=False)
    log.info("EasyOCR ready.")


def init_anthropic(api_key: str):
    global _anthropic_client
    if not _ANTHROPIC_AVAILABLE:
        log.warning("anthropic SDK not installed — Stage 3 will be skipped.")
        return
    _anthropic_client = _anthropic_sdk.Anthropic(api_key=api_key)
    log.info("Anthropic client ready (Stage 3 enabled).")


# ══════════════════════════════════════════════════════════════════════════
# ROI & PREPROCESSING
# ══════════════════════════════════════════════════════════════════════════
def _get_roi_crops(img: np.ndarray) -> List[Tuple[np.ndarray, str]]:
    h, w = img.shape[:2]
    y1 = int(h * ROI_Y1_FRAC)
    y2 = int(h * ROI_Y2_FRAC)

    crops = []
    x1l = int(w * ROI_LEFT_X1)
    x2l = int(w * ROI_LEFT_X2)
    crops.append((img[y1:y2, x1l:x2l], "left"))

    x1r = int(w * ROI_RIGHT_X1)
    x2r = int(w * ROI_RIGHT_X2)
    crops.append((img[y1:y2, x1r:x2r], "right"))

    return crops


def _preprocess_for_ocr(crop_bgr: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY)
    _, thresh = cv2.threshold(gray, 160, 255, cv2.THRESH_BINARY)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
    thresh = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, kernel)
    scaled = cv2.resize(thresh, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    return scaled


def _is_valid_cam_id(text: str) -> bool:
    return bool(CAM_ID_RE.search(text))


# ══════════════════════════════════════════════════════════════════════════
# STAGE 1 — EasyOCR
# ══════════════════════════════════════════════════════════════════════════
def _stage1_easyocr(img: np.ndarray) -> Tuple[str, float, str]:
    if _easyocr_reader is None:
        return "", 0.0, ""

    for crop, side in _get_roi_crops(img):
        try:
            results = _easyocr_reader.readtext(crop, detail=1)
            candidates = []
            for (_, text, conf) in results:
                clean = text.strip().upper().replace(" ", "")
                if conf >= EASYOCR_MIN_CONF and _is_valid_cam_id(clean):
                    candidates.append((clean, conf))

            if candidates:
                best = max(candidates, key=lambda x: x[1])
                return best[0], round(best[1] * 100, 1), side

        except Exception as e:
            log.debug(f"EasyOCR error ({side}): {e}")

    return "", 0.0, ""


# ══════════════════════════════════════════════════════════════════════════
# STAGE 2 — Tesseract
# ══════════════════════════════════════════════════════════════════════════
def _stage2_tesseract(img: np.ndarray) -> Tuple[str, float, str]:
    for crop, side in _get_roi_crops(img):
        try:
            processed = _preprocess_for_ocr(crop)
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

            if not texts:
                continue

            combined = "".join(texts).upper().replace(" ", "")
            avg_conf  = sum(confs) / len(confs)
            
            log.debug(f"Tesseract raw ({side}): '{combined}' (conf={avg_conf:.1f})")

            if _is_valid_cam_id(combined):
                return combined, round(avg_conf, 1), side

        except Exception as e:
            log.debug(f"Tesseract error ({side}): {e}")

    return "", 0.0, ""


# ══════════════════════════════════════════════════════════════════════════
# STAGE 3 — Claude Vision
# ══════════════════════════════════════════════════════════════════════════
def _stage3_claude_vision(jpeg_bytes: bytes) -> Tuple[str, float, str]:
    if _anthropic_client is None:
        return "", 0.0, ""

    try:
        b64 = base64.standard_b64encode(jpeg_bytes).decode("utf-8")

        prompt = (
            "This is a CCTV camera frame. There is a camera ID burned into the "
            "video overlay — it appears as white text on a dark background, "
            "typically in the bottom-left or bottom-right corner. "
            "The format looks like: 11394_REG-D_C01 or similar "
            "(digits, letters, underscores, hyphens, slashes). "
            "Extract ONLY the camera ID text. "
            "Return ONLY the raw camera ID string — no explanation, no quotes, "
            "no other text. If you cannot find it, return: NOT_FOUND"
        )

        response = _anthropic_client.messages.create(
            model=CLAUDE_VISION_MODEL,
            max_tokens=64,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": b64,
                            },
                        },
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
        )

        raw = response.content[0].text.strip().upper().replace(" ", "")
        if raw and raw != "NOT_FOUND" and _is_valid_cam_id(raw):
            return raw, 95.0, "claude_detected"

        return "", 0.0, ""

    except Exception as e:
        log.debug(f"Claude Vision error: {e}")
        return "", 0.0, ""


# ══════════════════════════════════════════════════════════════════════════
# COMBINED OCR
# ══════════════════════════════════════════════════════════════════════════
def ocr_camera_id_from_image(image_path: str, try_all_stages: bool = False) -> Tuple[str, float, str, str]:
    """
    Returns (extracted_text, confidence, stage_used, roi_side).
    
    Args:
        image_path: Path to the image file
        try_all_stages: If True, tries all 3 stages and picks the best result.
                       If False, stops at first successful stage (default behavior).
    """
    try:
        img = cv2.imread(image_path)
        if img is None:
            return "", 0.0, "image_load_error", ""

        if try_all_stages:
            # Try all stages and collect results
            results = []
            
            # Stage 1: EasyOCR
            text, conf, side = _stage1_easyocr(img)
            if text:
                results.append((text, conf, "easyocr", side))
                log.info(f"  Stage1 EasyOCR: {text} ({conf}%) [{side}]")
            
            # Stage 2: Tesseract
            text, conf, side = _stage2_tesseract(img)
            if text:
                results.append((text, conf, "tesseract", side))
                log.info(f"  Stage2 Tesseract: {text} ({conf}%) [{side}]")
            else:
                log.info(f"  Stage2 Tesseract: No valid result found")
            
            # Stage 3: Claude Vision
            _, jpeg_bytes = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 92])
            text, conf, side = _stage3_claude_vision(jpeg_bytes.tobytes())
            if text:
                results.append((text, conf, "claude_vision", side))
                log.info(f"  Stage3 Claude Vision: {text} ({conf}%)")
            else:
                log.info(f"  Stage3 Claude Vision: Disabled or no valid result")
            
            if not results:
                return "", 0.0, "all_stages_failed", ""
            
            # Pick the result with highest confidence
            best = max(results, key=lambda x: x[1])
            log.info(f"✓ Best result: {best[0]} ({best[1]}%) from {best[2]}")
            return best
        
        else:
            # Original behavior: stop at first success
            # Stage 1: EasyOCR
            text, conf, side = _stage1_easyocr(img)
            if text:
                log.info(f"✓ Stage1 EasyOCR: {text} ({conf}%) [{side}]")
                return text, conf, "easyocr", side

            # Stage 2: Tesseract
            text, conf, side = _stage2_tesseract(img)
            if text:
                log.info(f"✓ Stage2 Tesseract: {text} ({conf}%) [{side}]")
                return text, conf, "tesseract", side

            # Stage 3: Claude Vision
            _, jpeg_bytes = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 92])
            text, conf, side = _stage3_claude_vision(jpeg_bytes.tobytes())
            if text:
                log.info(f"✓ Stage3 Claude Vision: {text} ({conf}%)")
                return text, conf, "claude_vision", side

            return "", 0.0, "all_stages_failed", ""

    except Exception as e:
        log.error(f"OCR pipeline error: {e}")
        return "", 0.0, f"pipeline_error:{e}", ""


# ══════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(
        description="Test camera ID OCR on saved frame images"
    )
    parser.add_argument("--images-dir", help="Directory containing test images")
    parser.add_argument("--expected-id", help="Expected camera ID (for single image test)")
    parser.add_argument("--csv", help="CSV file with image paths and expected IDs")
    parser.add_argument("--image-col", default="Image Path", help="CSV column for image paths")
    parser.add_argument("--id-col", default="Expected ID", help="CSV column for expected IDs")
    parser.add_argument("--output", default="ocr_test_results.json", help="Output JSON file")
    parser.add_argument("--gpu", action="store_true", help="Use GPU for EasyOCR")
    parser.add_argument("--anthropic-key", default="", help="Anthropic API key for Stage 3")
    parser.add_argument("--no-claude", action="store_true", help="Disable Stage 3 Claude Vision")
    parser.add_argument("--try-all-stages", action="store_true", help="Try all OCR stages and pick best result")

    args = parser.parse_args()

    log.info("=" * 60)
    log.info("Camera ID OCR Test — Direct Image Testing")
    if args.try_all_stages:
        log.info("Mode: Try all stages and pick best result")
    else:
        log.info("Mode: Stop at first successful stage")
    log.info("=" * 60)

    init_easyocr(gpu=args.gpu)

    if args.anthropic_key and not args.no_claude:
        init_anthropic(args.anthropic_key)

    results = []

    # Single directory test
    if args.images_dir:
        image_dir = Path(args.images_dir)
        if not image_dir.exists():
            log.error(f"Directory not found: {args.images_dir}")
            return 1

        image_files = list(image_dir.glob("*.jpg")) + list(image_dir.glob("*.png"))
        log.info(f"Found {len(image_files)} images in {args.images_dir}")

        for img_path in image_files:
            log.info(f"\nProcessing: {img_path.name}")
            extracted, conf, stage, side = ocr_camera_id_from_image(str(img_path), args.try_all_stages)

            match = "UNKNOWN"
            if args.expected_id:
                match = "YES" if extracted == args.expected_id.upper().replace(" ", "") else "NO"

            results.append({
                "image": str(img_path),
                "expected": args.expected_id or "N/A",
                "extracted": extracted,
                "match": match,
                "confidence": conf,
                "ocr_stage": stage,
                "roi_side": side,
            })

    # CSV test
    elif args.csv:
        if not os.path.exists(args.csv):
            log.error(f"CSV file not found: {args.csv}")
            return 1

        df = pd.read_csv(args.csv)
        log.info(f"Loaded {len(df)} images from CSV")

        for idx, row in df.iterrows():
            img_path = str(row[args.image_col]).strip()
            expected = str(row[args.id_col]).strip().upper().replace(" ", "")

            if not os.path.exists(img_path):
                log.warning(f"Image not found: {img_path}")
                continue

            log.info(f"\n[{idx+1}/{len(df)}] Processing: {img_path}")
            extracted, conf, stage, side = ocr_camera_id_from_image(img_path, args.try_all_stages)

            match = "YES" if extracted == expected else "NO"

            results.append({
                "image": img_path,
                "expected": expected,
                "extracted": extracted,
                "match": match,
                "confidence": conf,
                "ocr_stage": stage,
                "roi_side": side,
            })

    else:
        log.error("Please provide either --images-dir or --csv")
        return 1

    # Save results
    with open(args.output, "w") as f:
        json.dump(results, f, indent=2)

    # Summary
    matched = sum(1 for r in results if r["match"] == "YES")
    mismatched = sum(1 for r in results if r["match"] == "NO")
    unknown = sum(1 for r in results if r["match"] == "UNKNOWN")

    log.info("\n" + "=" * 60)
    log.info("SUMMARY")
    log.info("=" * 60)
    log.info(f"Total images:  {len(results)}")
    log.info(f"✓ Matched:     {matched}")
    log.info(f"✗ Mismatched:  {mismatched}")
    log.info(f"? Unknown:     {unknown}")
    log.info(f"\nResults saved to: {args.output}")
    log.info("=" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(main())
