"""Camera uptime checker.

During the scheduled shift hours of live exams, checks every camera one at a time per
Camview server (plus one HLS lane), captures a frame and records whether it is available.

No vLLM and no camera classification here: the only verdict is "frame captured or not".
The service reads exam/schedule/camera tables and writes only its own camera_uptime_* tables.
"""

__version__ = "1.0.0"
