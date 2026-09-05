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

It also owns the User-Agent, for the same reason: how this app identifies
itself on the wire is one fact, and it was previously three. `musicbrainz.py`
and `commons.py` each carried their own copy of the same string, and
`api/remotes.py` a third variant.

⚠ All three were also WRONG in ways that matter:

  * Two of them said "FluxAudio" and pointed at
    github.com/flux3000/fluxaudio, a repository that no longer exists.
    MusicBrainz REQUIRES a descriptive agent naming the application and a
    working contact, and throttles or rejects clients without one — so a
    404 contact is an API-compliance problem, not a cosmetic one.
  * All three hardcoded version 1.0 while the app was on 0.2.0, which
    defeats the entire purpose of telling a server which version is calling.

Deriving it from version.py means it can never drift again. The import is
plain rather than guarded on purpose: run.py already imports version.py at
module level, so if this could fail the app would not start at all.

The format is MusicBrainz's convention, `Application/Version ( contact )`,
which also satisfies the other caller. `api/remotes.py` needs a non-default
agent because urllib's default is a textbook automated-traffic signature and
Cloudflare bot protection blocks it — traced live to a "library refused the
invite (403)" on 2026-08-30. Any real agent string fixes that; this one is
also honest about who is calling.
"""

import ssl

import certifi

from version import __version__

SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())

USER_AGENT = f"TrellisMusicLibrary/{__version__} ( https://trellismusiclibrary.com )"
