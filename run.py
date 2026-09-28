#!/usr/bin/env python
"""pic_manage entry point.

Usage:
    python run.py scan --source D:\\photos
    python run.py serve --port 8000
    python run.py status
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make the package importable when run as a plain script from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from picmanage.console import configure_stdio  # noqa: E402

configure_stdio()

from picmanage.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
