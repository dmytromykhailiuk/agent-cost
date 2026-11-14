#!/usr/bin/env python3
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from costlib.report import PLACEHOLDER, inject  # noqa: E402,F401

TEMPLATE = Path(__file__).with_name("report.html")


def main(argv):
    if len(argv) != 3:
        print("usage: preview.py <data.json> <out.html>", file=sys.stderr)
        return 2
    data = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    out = Path(argv[2]).resolve()
    out.write_text(inject(TEMPLATE.read_text(encoding="utf-8"), data), encoding="utf-8")
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
