#!/usr/bin/env python3
"""Change the single version source; builds and tags derive from it."""
import re
import sys
from pathlib import Path

if len(sys.argv) != 2 or not re.fullmatch(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)', sys.argv[1]):
    raise SystemExit('Usage: python scripts/set_version.py MAJOR.MINOR.PATCH')
path = Path(__file__).resolve().parents[1] / 'gracenote_lookup' / '__init__.py'
text = path.read_text()
current = re.search(r'__version__ = "([\d.]+)"', text)[1]
version = sys.argv[1]
if tuple(map(int, version.split('.'))) <= tuple(map(int, current.split('.'))):
    raise SystemExit(f'New version must be higher than {current}.')
path.write_text(text.replace(f'__version__ = "{current}"', f'__version__ = "{version}"'))
print(f'{current} → {version}')
