import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from uptime.config import Settings  # noqa: E402


def make_settings(**kw) -> Settings:
    return replace(Settings(), **kw)
