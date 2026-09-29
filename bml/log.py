"""Logging that survives having no console.

Always writes to a file, and to stdout only when there is one. Under
pythonw.exe a bare print() raises.
"""

from __future__ import annotations

import sys
from datetime import datetime

from . import config

def log(message: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {message}"
    try:
        # Looked up per call: the setup wizard can move the app folder while
        # the process is running.
        with (config.LOGS / "run.log").open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
    if sys.stdout is not None:
        try:
            print(line)
        except (OSError, ValueError, UnicodeEncodeError):
            pass
