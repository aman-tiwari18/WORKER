"""pm2 entry point: `python run.py run` (same as `python -m uptime run`)."""
import sys

from uptime.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
