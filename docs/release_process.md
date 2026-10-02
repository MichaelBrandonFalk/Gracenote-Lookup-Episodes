# Release process

Every published build uses a new `MAJOR.MINOR.PATCH` version. Published releases and their assets are retained; fixes receive a new version instead of replacing an old ZIP.

## Prepare a version

1. Run `python scripts/set_version.py 2.0.1` (replace the example with the next version).
2. Add release notes to `CHANGELOG.md`.
3. Run `python -m unittest discover -s tests -v` in the project environment.
4. Review and merge the source changes into `main`.
5. Tag that exact commit with `git tag -a v2.0.1 -m "Release v2.0.1"`, then push the tag.

## Build and publish

The tag workflow verifies that the Git tag matches the app version, builds on an Apple Silicon macOS runner, checks the frozen app's Qt startup and worker, and publishes a new GitHub Release. It refuses to replace an existing release.

Each release includes:

- `Gracenote_Episode_Lookup_vVERSION_macOS_arm64.zip`
- The corresponding SHA-256 checksum file.
- `release_vVERSION.json`, containing the same version, tag, filename, platform, architecture, checksum, size, and download URL.

The public download page reads GitHub's published release list. Its current-version button and previous-version list update automatically after a release is published. A fallback link opens GitHub Releases if the API is unavailable.

For a local build, use `python scripts/build_app.py` with official python.org Python. The builder rejects Python libraries whose native minimum macOS exceeds 13.0; a recently compiled pyenv runtime can require a newer OS even when Info.plist says 13.0. It also refuses to overwrite an existing versioned ZIP. A first release prepared locally may be uploaded with `gh release create ... --verify-tag` using the ZIP, checksum, and manifest from the same build.

## Platforms and signing

This release targets Apple Silicon macOS. Intel and Windows builds need separate platform builds and testing. The app is ad-hoc signed; Apple Developer signing and notarization have not been configured.

Chrome and a Gracenote account are required for live lookups. Site inspection and offline regressions verify specific behavior; the packaged app's complete login-to-output flow should also be exercised before adopting a release for large batches.
