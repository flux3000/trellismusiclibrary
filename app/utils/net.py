"""app/utils/net.py — one shared TLS context for the app's hand-rolled HTTPS calls.

`musicbrainz.py`, `commons.py` and `api/remotes.py` all call `urllib.request`
directly and relied on OpenSSL's implicit default CA search path. That's
present on a Homebrew Python, silently ABSENT on a python.org framework build
until its own Install Certificates.command has been run, and unverified inside
the PyInstaller-frozen app — three different "why is this machine special"
failure modes for what is really one gap.

Building the context from `certifi.where()` makes every outbound call use
the SAME bundled CA file regardless of what's installed system-wide, so
behaviour is identical from source, from any Python build, and inside the
packaged app. Build it once here and import it everywhere that needs it —
never call `ssl.create_default_context()` bare at a call site again.

It also owns the User-Agent, for the same reason: how this app identifies
itself on the wire is one fact.

MusicBrainz REQUIRES a descriptive agent naming the application and a
working contact, and throttles or rejects clients without one.

Deriving it from version.py means it can never drift again. The import is
plain rather than guarded on purpose: run.py already imports version.py at
module level, so if this could fail the app would not start at all.

The format is MusicBrainz's convention, `Application/Version ( contact )`,
which also satisfies the other caller. Any real agent string fixes that; this
one is also honest about who is calling.
"""

import ssl

import certifi

from version import __version__

SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())

USER_AGENT = f"TrellisMusicLibrary/{__version__} ( https://trellismusiclibrary.com )"
