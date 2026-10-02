#!/usr/bin/env python3
"""Reject a release tag that does not match the app version."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gracenote_lookup import __version__

ref = os.environ.get('GITHUB_REF_NAME', '')
if ref and ref != f'v{__version__}':
    raise SystemExit(f'Release tag {ref} does not match app version v{__version__}.')
print(f'Release version: v{__version__}')
