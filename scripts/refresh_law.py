"""Build cache/law/ecfr.json from saved eCFR section XML files.

The law lane quotes 29 CFR 778.107 and 778.110 (FLSA overtime). To refresh, save each section's XML from
the eCFR versioner API into cache/law/raw/, then run this script:

  https://www.ecfr.gov/api/versioner/v1/full/<YYYY-MM-DD>/title-29.xml?part=778&section=778.107
  -> cache/law/raw/29-cfr-778.107.xml

Usage: python scripts/refresh_law.py [--as-of YYYY-MM-DD]
Federal regulation text is in the public domain.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import date
from html import unescape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "cache" / "law" / "raw"
OUT = ROOT / "cache" / "law" / "ecfr.json"


def text_of(xml: str, tag: str) -> list[str]:
    found = re.findall(rf"<{tag}[^>]*>(.*?)</{tag}>", xml, re.S)
    return [re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", "", item))).strip() for item in found]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--as-of", default="2026-10-29", help="eCFR date the XML was saved for")
    args = parser.parse_args()
    sections = {}
    for path in sorted(RAW.glob("29-cfr-*.xml")):
        number = path.stem.removeprefix("29-cfr-")
        xml = path.read_text(encoding="utf-8")
        sections[f"29 CFR {number}"] = {
            "heading": text_of(xml, "HEAD")[0],
            "excerpt": text_of(xml, "P")[0],
            "url": f"https://www.ecfr.gov/current/title-29/section-{number}",
        }
    if not sections:
        parser.error(f"no section XML files in {RAW.relative_to(ROOT)}")
    payload = {"source": "eCFR versioner API", "as_of": args.as_of, "built": date.today().isoformat(), "sections": sections}
    OUT.write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)}: {', '.join(sections)} as of {args.as_of}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
