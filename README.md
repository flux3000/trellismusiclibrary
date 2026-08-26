# Trellis Music Library

A home for a live music collection — built first for the people who keep one.

Trellis is for collectors of **ROIO** (Recordings of Independent Origin): the
live concert tapes that taper-friendly artists have long permitted fans to
record and trade, on the understanding that no money ever changes hands. If
you have a folder of FLACs with a taper's info file next to each show, this is
built around exactly that.

It does two things. As an **archivist's tool**, it ingests a folder of audio,
reads the info file, resolves the show against MusicBrainz, scores the
recording's listening quality, and files it properly — fast metadata editing
with the source notes and the player right there. As a **listener's app**, it
is simply a good way to browse a library — by performer, venue, artist, genre,
or date — and play what you find.

The two halves meet at **peer sharing**. Every install of Trellis is a full
node: your own library, your own database, running on your own machine. There
is no central server and no account with a company in the middle — you invite
someone directly, and they browse and stream your collection from their own
copy of Trellis, with no ability to change anything on your end. That also
means you don't have to be a collector to use it: if a friend shares their
library with you, you get a real browsing and listening app for it, not a
bare file listing or a torrent client. And because a Trellis library is just
files and a database on a disk someone owns, it doesn't depend on any
platform staying online — the same durability that has kept tape trading
alive longer than any single site built to host it.

---

## Download

The easiest way to run Trellis is to grab the latest build from
[**Releases**](https://github.com/flux3000/trellis/releases) — no Python, no
command line.

Builds are currently unsigned, so the first launch will be refused by
Gatekeeper. Right-click the app → **Open** → **Open**. You only need to do
this once per machine.

The rest of this README is for building from source or working on the code.

## Requirements

- macOS 12 or later.
- **Python 3.11+, installed as a framework build.** A Mac app with a window
  needs one; the packaged build will fail confusingly without it.
- **ffmpeg**, if you intend to share your library. Peers never receive raw
  FLAC — the sharing node transcodes to MP3 on first play. A listener doesn't
  need it.
- Somewhere to keep the audio. Trellis never moves or rewrites your files
  except when you explicitly ask it to.

## Setup

```bash
git clone https://github.com/flux3000/trellis.git
cd trellis
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create the virtual environment from the same `python3` you intend to run the
app with. Everything below assumes `.venv` is active — the shell prompt tells
you.

`requirements.txt` states what Trellis depends on, loosely.
`requirements-lock.txt` states the exact versions a given release was built
from. Develop from the first, build from the second.

## Running

```bash
python3 run.py
```

That's the app: a native window, with Flask serving it on a background
thread. On first launch with no database, it creates one and opens an empty
library.

```bash
python3 run_headless.py
```

The same application with no window, for a machine that only serves.
Configured entirely by environment variables — `FLUX_PORT`, `FLUX_DB_PATH`,
`SHARE_BASE_URL`, `SECRET_KEY`, `FLUX_COOKIE_NAME`.

### Sharing your library over the internet

Sharing runs as a **second process**, in share-only mode:

```bash
SERVER_MODE=true \
SECRET_KEY="$(python3 -c 'import secrets;print(secrets.token_hex(32))')" \
FLUX_PORT=5760 \
TRUSTED_CLIENT_IP_HEADER=CF-Connecting-IP \
python3 run_headless.py
```

`SERVER_MODE` doesn't just restrict what the process will answer — it changes
what the process *builds*. Only the peer-facing blueprint is registered, so
the login page, the editing endpoints, and the frontend don't exist in that
process at all; they answer 404 because Flask has never heard of them.
`tests/test_server_mode_surface.py` asserts that as a property of the whole
route table.

Exposure is via a Cloudflare Tunnel — outbound only, so no router ports are
opened and your home address is never published. `SHARE_BASE_URL` should be
the public hostname; invites are minted as a single `https://address#CODE`
string and are useless without it.

## Tests

```bash
python3 -m pytest tests/ -q
```

No network calls, no audio files, and no library mount required. Several
tests exist to assert that a specific past bug can't come back, and say so in
their own docstrings.

## Building the app

```bash
./tools/build_macos.sh          # → dist/Trellis Music Library.app
```

Must run on macOS — PyInstaller doesn't cross-compile. The build is unsigned,
so the first launch on any machine (including yours) is refused by
Gatekeeper: right-click → Open → Open, once.

The icon is generated from code rather than stored as an opaque asset:

```bash
python3 tools/design/make_icon.py assets/icon
```

## How sharing works, briefly

- **Every install is a node** — both a server and a client at once. Identity
  is per-node; there's no global account. In your database you're the admin
  and I'm a peer; in mine, the reverse.
- **Peers are not users.** A peer authenticates with a bearer token into a
  blueprint that contains no editing endpoints at all — it's structurally
  incapable of changing anything, rather than relying on every check to
  remember to exclude peers.
- **Grants are collection-level.** Sharing your whole library is itself a
  collection, resolved by query rather than by membership rows.
- **Catalog metadata versus holdings.** A peer sees any performer, venue, or
  artist page in full — that's reference data about the world. Every *list of
  recordings* on that page is filtered to what they've actually been granted.
- **Your listening history and your favorites stay home.** A peer gets their
  own stars, stored on their side.

## Layout

```
run.py                  desktop app (Flask thread + native window)
run_headless.py         server-only entry point
config.py               all configuration; the single source of paths
version.py              the version, read by the app and the build
trellis.spec            PyInstaller recipe
app/
  api/                  Flask blueprints — one per resource
    share.py            the peer-facing door: read-only by construction
    remotes.py          the other direction: consuming someone else's library
  models/                SQLAlchemy models
  utils/                 ingest, quality scoring, checksums, transcode, peer auth
  static/                the frontend — plain HTML/JS/CSS, no build step
scripts/                 one-off migrations and backfills, kept after running
tools/                   developer surfaces: build, icon, design specimens
tests/
```

## Conventions worth knowing before changing things

- **The database is the source of truth.** FLAC tags are written only when
  explicitly exported, never automatically.
- **No migration framework.** Schema comes from `create_all` plus the
  numbered scripts in `scripts/`, which stay in the repo after they've run.
- **Destructive filesystem work happens before the database write**, so a
  disk failure leaves the row — and therefore the handle on the folder —
  intact.
- **There's exactly one frontend gate on editing**, `canEditLibrary()`. New
  editable surfaces go through the shared helpers that honor it, rather than
  checking the role directly at each call site.
- **The frontend has no build step.** It's served as written.

## License

No license has been chosen yet — until one is, all rights are reserved by
default. If you'd like to use, fork, or build on this, open an issue or reach
out directly.
