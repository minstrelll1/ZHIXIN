"""SU17 six-UAV competition orchestration backend."""

__version__ = "0.2.0"
# Source checkout support; installed wheels include competition_shared.
import sys
from pathlib import Path
_project = Path(__file__).resolve().parents[2]
if (_project / "competition_shared").is_dir() and str(_project) not in sys.path:
    sys.path.insert(0, str(_project))

