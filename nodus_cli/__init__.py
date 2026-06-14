"""
Nodus CLI - Unified command-line interface for Nodus Agent.

Provides subcommands for:
- nodus chat          - Interactive chat
- nodus gateway       - Run gateway in foreground
- nodus gateway start - Start gateway service
- nodus gateway stop  - Stop gateway service
- nodus setup         - Interactive setup wizard
- nodus status        - Show status of all components
- nodus cron          - Manage cron jobs
"""

import os
import sys

__version__ = "0.15.1"
__release_date__ = "2026.6.13"


def _ensure_utf8():
    """Force UTF-8 stdout/stderr on Windows to prevent UnicodeEncodeError."""
    if sys.platform != "win32":
        return
    os.environ.setdefault("PYTHONUTF8", "1")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")


_ensure_utf8()
