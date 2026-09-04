"""
version.py — one place that says which Trellis this is.

Read by the packaged app's bundle metadata, the build script's output filename,
and the debug drawer. A version that lives in three places is a version that
disagrees with itself, and "which build is Jim actually running?" is the first
question every support conversation starts with.

Bump this, then tag the commit to match.

⚠ That second half has been skipped before. Tags v0.1.2 and v0.1.3 were both
cut while this file still said 0.1.1, so two releases shipped identifying
themselves as a version they were not — in the About box, in CFBundleVersion,
and in the debug drawer. From 0.2.0 the version is sealed inside a signed,
notarized bundle, where renaming the download cannot correct it. Bump here
FIRST, build, then tag the commit you built.
"""

__version__ = "0.2.0"

# The app has two names on purpose (Ryan, 2026-08-25 — "we should be calling it
# Trellis Music Library, not just Trellis").
#
# APP_NAME is the real name: the Finder icon, the About box, the installer.
# SHORT_NAME is for places with a hard length budget — macOS puts CFBundleName
# in the menu bar and truncates past roughly sixteen characters, and "Trellis
# Music Library" is twenty-one. Apple's own convention is exactly this split.
APP_NAME   = "Trellis Music Library"
SHORT_NAME = "Trellis"
