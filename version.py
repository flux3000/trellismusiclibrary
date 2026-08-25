"""
version.py — one place that says which Trellis this is.

Read by the packaged app's bundle metadata, the build script's output filename,
and the debug drawer. A version that lives in three places is a version that
disagrees with itself, and "which build is Jim actually running?" is the first
question every support conversation starts with.

Bump this, then tag the commit to match.
"""

__version__ = "0.1.0"

# Shown in the app and in the bundle. Kept separate from __version__ so a build
# can be identified more precisely than a release ("0.1.0" vs "0.1.0 (mini)")
# without polluting the number things compare against.
BUILD_NAME = "Trellis"
