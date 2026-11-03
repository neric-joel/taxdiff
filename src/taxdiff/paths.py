"""Locations of the files that ship with the repository, and how cache files are written."""

import os
import threading
from pathlib import Path


def _root() -> Path:
    """The repository root: TAXDIFF_ROOT if set, else the checkout this file is in. A copy of the
    package installed elsewhere (a host that installs the project as a wheel) uses the working directory."""
    if os.environ.get("TAXDIFF_ROOT"):
        return Path(os.environ["TAXDIFF_ROOT"]).resolve()
    here = Path(__file__).resolve().parents[2]
    return here if (here / "rules").is_dir() else Path.cwd()


ROOT = _root()
RULES = ROOT / "rules" / "ty2025.yaml"
BOOK = ROOT / "data" / "book_ty2025.json"
DOCUMENTS = ROOT / "data" / "documents"
CHANGES = ROOT / "changes"
PARITY_CACHE = ROOT / "cache" / "policyengine.json"


def write_text_atomic(path: Path, text: str) -> None:
    """Write through a temporary file in the same folder, then rename it over the target.

    A reader never sees half a file. The temporary name carries the process and thread, so two sessions of
    one server never share it. When two writers race, the last rename wins.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)
