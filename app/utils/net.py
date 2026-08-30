"""
app/utils/net.py — one shared TLS context for the app's hand-rolled HTTPS calls.

Nothing here used `certifi` until 2026-08-30. `musicbrainz.py`, `commons.py`
and `api/remotes.py` all call `urllib.request` directly and relied on
OpenSSL's implicit default CA search path. That's present on a Homebrew
Python, silently ABSENT on a python.org framework build until its own
Install Certificates.command has been run, and unverified inside the
PyInstaller-frozen app — three different "why is this machine special"
failure modes for what is really one gap.

It bit for real 2026-08-27: a tester's node (python.org build) failed to
enroll with `CERTIFICATE_VERIFY_FAILED`, which reads to a tester as "the
sharer's library is down" rather than what it actually is.

Building the context from `certifi.where()` makes every outbound call use
the SAME bundled CA file regardless of what's installed system-wide, so
behaviour is identical from source, from any Python build, and inside the
packaged app. Build it once here and import it everywhere that needs it —
never call `ssl.create_default_context()` bare at a call site again.
"""

import ssl

import certifi

SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
