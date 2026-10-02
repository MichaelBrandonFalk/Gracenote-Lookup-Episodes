#!/usr/bin/env python3
"""Build a versioned desktop bundle and ZIP; never replace another version."""
import argparse
import hashlib
import json
import os
import platform
import plistlib
import re
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from gracenote_lookup import __version__


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'downloads')
    args = parser.parse_args()
    system = platform.system()
    arch = platform.machine().lower()
    if system != 'Darwin' or arch != 'arm64':
        raise SystemExit('Build this release on an Apple Silicon Mac (arm64).')
    # A pyenv runtime compiled on a recent Mac can require that same macOS,
    # regardless of the app's Info.plist. Never claim compatibility it lacks.
    from PyInstaller.depend.bindepend import get_python_library_path
    library = get_python_library_path()
    load_commands = subprocess.check_output(['otool', '-l', str(library)], text=True)
    minimums = re.findall(r'\bminos\s+(\d+(?:\.\d+)+)', load_commands)
    minimums += re.findall(r'cmd LC_VERSION_MIN_MACOSX\s+cmdsize \d+\s+version (\d+(?:\.\d+)+)', load_commands)
    def version_tuple(value):
        parts = tuple(map(int, value.split('.')))
        return parts + (0,) * (3 - len(parts))
    if not minimums or max(map(version_tuple, minimums)) > (13, 0, 0):
        raise SystemExit('Python requires macOS newer than 13.0. Build with official python.org Python '
                         'or GitHub Actions, rather than a runtime compiled on this Mac.')
    name = f'Gracenote Episode Lookup v{__version__}'
    asset_name = f'Gracenote_Episode_Lookup_v{__version__}_macOS_arm64.zip'
    args.output_dir.mkdir(parents=True, exist_ok=True)
    archive = args.output_dir / asset_name
    if archive.exists():
        raise SystemExit(f'{archive} already exists. Bump the version for a new release.')
    # Only the Selenium Manager executable for this build platform is needed.
    import selenium.webdriver.common
    manager = Path(selenium.webdriver.common.__file__).parent / 'macos' / 'selenium-manager'
    if not manager.is_file():
        raise SystemExit('Selenium Manager executable is missing from the environment.')
    build_env = dict(os.environ, PYINSTALLER_CONFIG_DIR=str(ROOT / 'build' / 'pyinstaller-cache'))
    subprocess.run([
        sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--windowed',
        '--name', name, '--target-architecture', 'arm64',
        '--osx-bundle-identifier', 'com.michaelbrandonfalk.gracenote-episode-lookup',
        '--add-binary', f'{manager}:selenium/webdriver/common/macos',
        '--add-data', f'{ROOT / "gracenote_lookup" / "assets"}:gracenote_lookup/assets',
        '--exclude-module', 'PyQt5', '--exclude-module', 'PyQt6',
        '--exclude-module', 'PySide2', '--exclude-module', 'tkinter',
        '--distpath', str(ROOT / 'dist'), '--workpath', str(ROOT / 'build'),
        '--specpath', str(ROOT / 'build'), str(ROOT / 'app.py'),
    ], cwd=ROOT, env=build_env, check=True)
    bundle = ROOT / 'dist' / f'{name}.app'
    plist_path = bundle / 'Contents' / 'Info.plist'
    with plist_path.open('rb') as stream:
        plist = plistlib.load(stream)
    plist.update(CFBundleShortVersionString=__version__, CFBundleVersion=__version__,
                 NSHighResolutionCapable=True, LSMinimumSystemVersion='13.0')
    with plist_path.open('wb') as stream:
        plistlib.dump(plist, stream)
    # Modifying Info.plist invalidates PyInstaller's initial ad-hoc signature.
    subprocess.run(['codesign', '--force', '--deep', '--sign', '-', str(bundle)], check=True)
    subprocess.run(['codesign', '--verify', '--deep', '--strict', str(bundle)], check=True)
    subprocess.run(['ditto', '-c', '-k', '--norsrc', '--noextattr', '--keepParent', str(bundle), str(archive)], check=True)
    with zipfile.ZipFile(archive) as zipped:
        if not any(p.endswith('/macos/selenium-manager') for p in zipped.namelist()):
            raise SystemExit('Packaged app is missing Selenium Manager.')
        if not any(p.endswith('/gracenote_lookup/assets/checkmark.svg') for p in zipped.namelist()):
            raise SystemExit('Packaged app is missing its checkbox checkmark.')
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (args.output_dir / f'{asset_name}.sha256').write_text(f'{digest}  {asset_name}\n')
    manifest = {
        'app': 'Gracenote Episode Lookup', 'version': __version__, 'tag': f'v{__version__}',
        'platform': 'macOS', 'architecture': 'arm64', 'filename': asset_name,
        'sha256': digest, 'size_bytes': archive.stat().st_size,
        'download_url': f'https://github.com/MichaelBrandonFalk/Gracenote-Lookup-Episodes/releases/download/v{__version__}/{asset_name}',
    }
    (args.output_dir / f'release_v{__version__}.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(f'App: {bundle}\nDownload: {archive}')


if __name__ == '__main__':
    main()
