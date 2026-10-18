"""PanelPlay portal. The renderer modules (narrate, sources, ...) live one folder up, so make them importable."""
import sys
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
