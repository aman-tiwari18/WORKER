"""
Camera Distribution Logic for Horizontal Scaling
=================================================
Handles camera assignment across multiple GPU worker nodes.

Supports two distribution strategies:
1. Static Range Assignment: Each worker handles a fixed camera ID range
2. Dynamic Assignment: Workers pull cameras from a shared queue

Author: CCTV Monitoring System
Date: 2026-04-29
"""

import os
import logging
import zlib
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)


class CameraDistributor:
    """
    Manages camera distribution across multiple worker nodes.
    """
    
    def __init__(self):
        self.worker_id = os.environ.get("WORKER_ID", "main")
        self.camera_start = self._parse_int_env("CAMERA_START_ID")
        self.camera_end = self._parse_int_env("CAMERA_END_ID")
        self.distribution_mode = os.environ.get("DISTRIBUTION_MODE", "auto")
        
        # For auto mode: total workers in the cluster
        self.total_workers = self._parse_int_env("TOTAL_WORKERS") or 1
        self.worker_index = self._extract_worker_index()
        
        logger.info(
            f"Camera Distributor initialized: "
            f"worker_id={self.worker_id}, "
            f"mode={self.distribution_mode}, "
            f"worker_index={self.worker_index}/{self.total_workers}, "
            f"range={self.camera_start}-{self.camera_end}"
        )
    
    @staticmethod
    def _parse_int_env(key: str) -> Optional[int]:
        """Parse integer from environment variable."""
        val = os.environ.get(key)
        if val:
            try:
                return int(val)
            except ValueError:
                logger.warning(f"Invalid integer for {key}: {val}")
        return None
    
    def _extract_worker_index(self) -> int:
        """
        Extract numeric index from worker_id.
        Examples: worker-0 -> 0, worker-1 -> 1, main -> 0

        Honors WORKER_INDEX env var when set. This is required for
        multi-GPU workers (e.g. worker-4-gpu0..gpu7) whose IDs do not
        end in a unique trailing integer.
        """
        explicit = os.environ.get("WORKER_INDEX")
        if explicit is not None and explicit != "":
            try:
                return int(explicit)
            except ValueError:
                logger.warning(f"Invalid WORKER_INDEX env var: {explicit!r}; falling back to regex")

        if self.worker_id in ["main", "worker-0"]:
            return 0

        import re
        match = re.search(r'(\d+)$', self.worker_id)
        if match:
            return int(match.group(1))

        return 0
    
    def should_process_camera(self, camera: Dict) -> bool:
        """
        Determine if this worker should process the given camera.
        
        Args:
            camera: Camera dict with 'id', 'ch_code', or 'device_id'
        
        Returns:
            True if this worker should process the camera
        """
        if self.distribution_mode in ("static", "range"):
            return self._static_assignment(camera)
        elif self.distribution_mode == "dynamic":
            return self._dynamic_assignment(camera)
        elif self.distribution_mode == "auto":
            return self._auto_assignment(camera)
        elif self.distribution_mode == "single":
            return True  # Process all cameras on this worker regardless of range settings
        else:
            # Default: process all cameras (single-node mode)
            return True
    
    def _static_assignment(self, camera: Dict) -> bool:
        """
        Static range-based assignment.
        
        Cameras are assigned based on their ID falling within
        the worker's configured range.
        """
        # If no range configured, process all cameras
        if self.camera_start is None or self.camera_end is None:
            return True
        
        # Try to get camera ID from various fields
        # Prioritize camera_id over the generic 'id' field which might be an execution ID
        # Note: If 'camera_id' is present, it's a specific assignment. 
        # If only 'id' is present, we check if it looks like a camera ID or an execution ID.
        camera_id = camera.get("camera_id")
        
        # If no camera_id, check other fields
        if camera_id is None:
            camera_id = (
                self._extract_numeric_id(camera.get("ch_code")) or
                self._extract_numeric_id(camera.get("device_id"))
            )
            
        # Fallback to 'id' only if it's not a poller assignment (where id is execution_id)
        # We assume if 'script_code' or 'task_name' is present, it's an assignment object.
        if camera_id is None and not any(k in camera for k in ["script_code", "task_name", "analytics_task_id"]):
            camera_id = camera.get("id")
        
        if camera_id is None:
            # If no camera ID is found, it might be a global assignment.
            # Allow it to run on all workers so they can filter cameras internally.
            logger.debug(f"No camera ID found in assignment; allowing on worker {self.worker_id} for internal filtering")
            return True
        
        # Check if camera falls within this worker's range
        in_range = self.camera_start <= camera_id <= self.camera_end
        
        if in_range:
            logger.debug(
                f"Camera {camera_id} assigned to worker {self.worker_id} "
                f"(range: {self.camera_start}-{self.camera_end})"
            )
        
        return in_range
    
    def _dynamic_assignment(self, camera: Dict) -> bool:
        """
        Dynamic assignment using database worker_id field.
        
        Cameras have a 'worker_id' field in the database that
        indicates which worker should process them.
        """
        assigned_worker = camera.get("worker_id")
        
        # If no worker assigned, this worker can claim it
        if not assigned_worker:
            return True
        
        # Check if assigned to this worker
        return assigned_worker == self.worker_id
    
    def _auto_assignment(self, camera: Dict) -> bool:
        """
        Automatic load balancing using hash-based distribution.
        
        Distributes cameras evenly across workers using modulo operation.
        This automatically adapts to any number of cameras without
        manual configuration.
        """
        # Try to get camera ID from various fields
        camera_id = camera.get("camera_id")
        
        # If no camera_id, check other fields
        if camera_id is None:
            camera_id = (
                self._extract_numeric_id(camera.get("ch_code")) or
                self._extract_numeric_id(camera.get("device_id"))
            )
            
        # Fallback to 'id' only if it's not a poller assignment (where id is execution_id)
        if camera_id is None and not any(k in camera for k in ["script_code", "task_name", "analytics_task_id"]):
            camera_id = camera.get("id")
        
        if camera_id is None:
            # If no camera ID is found, it might be a global assignment.
            # Allow it to run on all workers so they can filter cameras internally.
            logger.debug(f"No camera ID found in auto assignment; allowing on worker {self.worker_id}")
            return True
        
        # Hash-based distribution: camera_id % total_workers == worker_index % total_workers
        # This handles both 0-based (0,1,2,3) and 1-based (1,2,3,4) worker naming
        assigned_worker = camera_id % self.total_workers
        return assigned_worker == (self.worker_index % self.total_workers)
    
    @staticmethod
    def _extract_numeric_id(value: any) -> Optional[int]:
        """Extract numeric ID from string or int value."""
        if value is None:
            return None
        
        if isinstance(value, int):
            return value
        
        if isinstance(value, str):
            # Try direct conversion
            try:
                return int(value)
            except ValueError:
                pass
            
            # Try extracting first number
            import re
            match = re.search(r'\d+', value)
            if match:
                return int(match.group())
        
        return None

    def _stable_camera_row_id(self, camera: Dict) -> Optional[int]:
        """
        Resolve a stable per-camera ID for list filtering.

        DB camera rows expose the table primary key as ``id``. Camera configs
        produced later in the pipeline keep that same value in ``camera_id_pk``.
        Those fields are preferred so worker assignment is independent of DB
        row order and stays stable even when camera rows are added or removed.
        """
        for key in ("id", "camera_id_pk", "camera_id", "ch_code", "device_id"):
            camera_id = self._extract_numeric_id(camera.get(key))
            if camera_id is not None:
                return camera_id

        return None

    def _camera_partition_key(self, camera: Dict) -> Optional[int]:
        """
        Build a stable, evenly-distributed partition key for auto mode.

        Using DB primary keys (`id`) can become badly skewed after downstream
        filters like exam/status/classification. For camera sharding we prefer
        the logical camera identity:
        - HLS: `ch_code`
        - RTSP: `device_id:camera_no`
        and hash it deterministically.
        """
        ch_code = str(camera.get("ch_code") or "").strip()
        if ch_code:
            return zlib.crc32(ch_code.encode("utf-8")) & 0xFFFFFFFF

        device_id = str(camera.get("device_id") or "").strip()
        camera_no = str(camera.get("camera_no") or "").strip()
        if device_id and camera_no:
            return zlib.crc32(f"{device_id}:{camera_no}".encode("utf-8")) & 0xFFFFFFFF

        camera_id = str(camera.get("camera_id") or "").strip()
        if camera_id:
            return zlib.crc32(camera_id.encode("utf-8")) & 0xFFFFFFFF

        camera_id_pk = self._stable_camera_row_id(camera)
        if camera_id_pk is not None:
            return camera_id_pk

        return None
    
    def filter_cameras(self, cameras: List[Dict]) -> List[Dict]:
        """
        Filter camera list to only include cameras assigned to this worker.
        Uses stable camera IDs instead of list indexes, so assignment does not
        change when the database returns the same rows in a different order.
        """
        if self.distribution_mode == "single":
            assigned = list(cameras)
        elif self.distribution_mode == "auto":
            if self.total_workers <= 1:
                assigned = list(cameras)
            else:
                worker_slot = self.worker_index % self.total_workers
                assigned = []

                for cam in cameras:
                    camera_id = self._camera_partition_key(cam)

                    if camera_id is None:
                        # Fall back to the mode-aware path for non-standard camera
                        # payloads. This preserves existing behavior for global
                        # assignments that intentionally run on every worker.
                        if self.should_process_camera(cam):
                            assigned.append(cam)
                        continue

                    if camera_id % self.total_workers == worker_slot:
                        assigned.append(cam)
        elif self.distribution_mode in ("static", "range"):
            if self.camera_start is None or self.camera_end is None:
                assigned = list(cameras)
            else:
                assigned = []
                for cam in cameras:
                    camera_id = self._stable_camera_row_id(cam)
                    if camera_id is None:
                        if self.should_process_camera(cam):
                            assigned.append(cam)
                        continue

                    if self.camera_start <= camera_id <= self.camera_end:
                        assigned.append(cam)
        else:
            assigned = [cam for cam in cameras if self.should_process_camera(cam)]

        logger.info(
            f"Worker {self.worker_id}: "
            f"Assigned {len(assigned)}/{len(cameras)} cameras"
        )

        return assigned
    
    def get_worker_stats(self) -> Dict:
        """Get worker statistics for monitoring."""
        return {
            "worker_id": self.worker_id,
            "worker_index": self.worker_index,
            "total_workers": self.total_workers,
            "distribution_mode": self.distribution_mode,
            "camera_range": {
                "start": self.camera_start,
                "end": self.camera_end,
            } if self.camera_start and self.camera_end else None,
        }


# Global distributor instance
distributor = CameraDistributor()


def should_process_camera(camera: Dict) -> bool:
    """
    Convenience function to check if camera should be processed.
    
    Args:
        camera: Camera dict
    
    Returns:
        True if this worker should process the camera
    """
    return distributor.should_process_camera(camera)


def filter_cameras(cameras: List[Dict]) -> List[Dict]:
    """
    Convenience function to filter camera list.
    
    Args:
        cameras: List of camera dicts
    
    Returns:
        Filtered list for this worker
    """
    return distributor.filter_cameras(cameras)


# ============================================================
# Camera Distribution Utilities
# ============================================================

def calculate_camera_ranges(total_cameras: int, num_workers: int) -> List[Dict]:
    """
    Calculate camera ID ranges for static distribution.
    
    Args:
        total_cameras: Total number of cameras
        num_workers: Number of GPU worker nodes
    
    Returns:
        List of dicts with worker_id, start, end
    
    Example:
        >>> calculate_camera_ranges(20000, 4)
        [
            {'worker_id': 'worker-1', 'start': 1, 'end': 5000},
            {'worker_id': 'worker-2', 'start': 5001, 'end': 10000},
            {'worker_id': 'worker-3', 'start': 10001, 'end': 15000},
            {'worker_id': 'worker-4', 'start': 15001, 'end': 20000},
        ]
    """
    cameras_per_worker = total_cameras // num_workers
    remainder = total_cameras % num_workers
    
    ranges = []
    current_start = 1
    
    for i in range(num_workers):
        # Distribute remainder across first workers
        extra = 1 if i < remainder else 0
        cameras_for_this_worker = cameras_per_worker + extra
        
        current_end = current_start + cameras_for_this_worker - 1
        
        ranges.append({
            "worker_id": f"worker-{i+1}",
            "start": current_start,
            "end": current_end,
            "count": cameras_for_this_worker,
        })
        
        current_start = current_end + 1
    
    return ranges


def print_distribution_plan(total_cameras: int, num_workers: int):
    """
    Print camera distribution plan for deployment.
    
    Args:
        total_cameras: Total number of cameras
        num_workers: Number of GPU worker nodes
    """
    ranges = calculate_camera_ranges(total_cameras, num_workers)
    
    print("\n" + "="*60)
    print("CAMERA DISTRIBUTION PLAN")
    print("="*60)
    print(f"Total Cameras: {total_cameras:,}")
    print(f"GPU Workers: {num_workers}")
    print(f"Cameras per Worker: ~{total_cameras // num_workers:,}")
    print("="*60)
    
    for r in ranges:
        print(
            f"{r['worker_id']:12} | "
            f"Cameras {r['start']:6,} - {r['end']:6,} | "
            f"Count: {r['count']:5,}"
        )
    
    print("="*60 + "\n")


if __name__ == "__main__":
    # Example usage
    logging.basicConfig(level=logging.INFO)
    
    # Test distribution calculation
    print_distribution_plan(20000, 4)
    print_distribution_plan(100000, 10)
    
    print("\n" + "="*60)
    print("TESTING AUTO MODE (Hash-based Distribution)")
    print("="*60)
    
    # Simulate 4 workers with auto mode
    test_cameras = [
        {"id": i, "ch_code": f"cam_{i}"} 
        for i in range(1, 41)  # Test with 40 cameras
    ]
    
    # Test each worker
    for worker_idx in range(4):
        os.environ["WORKER_ID"] = f"worker-{worker_idx}"
        os.environ["TOTAL_WORKERS"] = "4"
        os.environ["DISTRIBUTION_MODE"] = "auto"
        
        dist = CameraDistributor()
        filtered = dist.filter_cameras(test_cameras)
        
        print(f"\nWorker {worker_idx}: Assigned {len(filtered)} cameras")
        print(f"  Camera IDs: {[c['id'] for c in filtered[:10]]}{'...' if len(filtered) > 10 else ''}")
    
    print("\n" + "="*60)
    print("TESTING STATIC MODE (Range-based Distribution)")
    print("="*60)
    
    # Test static mode
    os.environ["WORKER_ID"] = "worker-1"
    os.environ["CAMERA_START_ID"] = "1"
    os.environ["CAMERA_END_ID"] = "5000"
    os.environ["DISTRIBUTION_MODE"] = "static"
    
    dist = CameraDistributor()
    filtered = dist.filter_cameras(test_cameras)
    
    print(f"\nWorker 1 (Static): Assigned {len(filtered)} cameras")
    for cam in filtered:
        print(f"  - Camera ID {cam['id']}")
