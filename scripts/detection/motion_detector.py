"""
Motion Detector
================
CPU-only motion detection for pre-filtering frames before sending to GPU.
Uses background subtraction to detect significant scene changes.

Used by close_proximity TCS to avoid sending static frames to GPU queue.
"""

import cv2
import threading


class MotionDetector:
    """
    Lightweight CPU-only motion detector using background subtraction.
    
    Each camera gets its own MotionDetector instance to track
    frame-over-frame changes independently.
    """

    def __init__(self, camera_id: str, motion_threshold: int = 800):
        """
        Args:
            camera_id: Identifier for the camera (for logging)
            motion_threshold: Minimum contour area to consider as motion.
                              Higher = less sensitive. Default 800.
        """
        self.camera_id = camera_id
        self.motion_threshold = motion_threshold
        self.bg_subtractor = cv2.createBackgroundSubtractorMOG2(
            history=500,
            varThreshold=50,
            detectShadows=False
        )
        self._frame_count = 0

    def has_motion(self, frame) -> bool:
        """
        Check if frame has significant motion compared to background.

        Args:
            frame: BGR frame from OpenCV

        Returns:
            True if motion detected above threshold
        """
        self._frame_count += 1

        # Let background model warm up for first 10 frames
        if self._frame_count <= 10:
            self.bg_subtractor.apply(frame)
            return True  # Process all frames during warmup

        # Apply background subtraction
        fg_mask = self.bg_subtractor.apply(frame)

        # Find contours in the foreground mask
        contours, _ = cv2.findContours(
            fg_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )

        # Check if any contour exceeds the motion threshold
        for contour in contours:
            if cv2.contourArea(contour) > self.motion_threshold:
                return True

        return False

    def reset(self):
        """Reset the background model."""
        self.bg_subtractor = cv2.createBackgroundSubtractorMOG2(
            history=500,
            varThreshold=50,
            detectShadows=False
        )
        self._frame_count = 0


# ── Factory / Registry ──────────────────────────────────────────────
# One MotionDetector per camera, thread-safe creation
_detectors = {}
_lock = threading.Lock()


def get_motion_detector(camera_id: str, motion_threshold: int = 800) -> MotionDetector:
    """
    Get or create a MotionDetector for the given camera.

    Thread-safe — guaranteed to return the same instance for the same camera_id.

    Args:
        camera_id: Unique camera identifier
        motion_threshold: Minimum contour area for motion detection

    Returns:
        MotionDetector instance
    """
    with _lock:
        if camera_id not in _detectors:
            _detectors[camera_id] = MotionDetector(camera_id, motion_threshold)
        return _detectors[camera_id]
