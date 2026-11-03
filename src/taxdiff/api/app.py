"""Deploy entry point for the API: `src.taxdiff.api.app:app`.

A host may load this file directly, or import it as `src.taxdiff.api.app`, while also installing the
project as a package elsewhere. Putting this checkout's src/ first on the path makes the package, and the
rules, book and caches it reads relative to itself, load from the repository in every case. Locally,
`uvicorn taxdiff.api.server:app` does the same thing without this file.
"""

import sys
from pathlib import Path

_SRC = str(Path(__file__).resolve().parents[2])
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from taxdiff.api.server import app  # noqa: E402

__all__ = ["app"]
