"""Double-clickable launcher for the desktop window.

`.pyw` so no console window appears. Same as `py -m bml.main gui`.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bml import config  # noqa: E402
from bml import gui  # noqa: E402

config.load_env()
sys.exit(gui.run())
