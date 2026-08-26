# Trellis Music Library

A desktop application for collectors of **ROIO** — Recordings of Independent
Origin, the live concert recordings that taper-friendly bands permit to be
recorded and traded freely, provided no money changes hands.

Trellis does two things. It is an **archivist's tool**: ingest a folder of
FLACs, read the taper's info file, resolve the show against MusicBrainz,
score the recording's listening quality, and file it. And it is a **listener's
app**: browse a library by performer, venue, artist, genre or date and play it.

The two halves meet at **peer sharing**. Every install is a node. You can hand
someone an invite and they browse and stream your library from their own copy
of Trellis, without ever being able to change it.

> The repository is still called `fluxaudio` and the database file is still
> `fluxaudio.db`. The app was renamed to Trellis in August 2026; user-visible
> strings were changed first, and the repo, paths and filename are a deliberate
> later step. Nothing is broken — the old name simply survives in places nobody
> looks at.

---

## Requirements

- **macOS.** Windows is not supported yet — the paths and the drive-mount
  monitor assume a Mac. The credential storage is already cross-platform.
- **Python 3.11+, installed as a framework build.** This matters: a Mac app
  with a window needs one, and the packaged build will fail confusingly without
  it.
- **ffmpeg**, if you intend to share. Peers never receive raw FLAC — the
  sharing node transcodes to MP3 on first play. A listener does not need it.
- A place to keep the audio. Trellis never moves or rewrites your files except
  when you explicitly ask it to.

## Setup

```bash
git clone https://github.com/flux3000/fluxaudio.git
cd fluxaudio
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create the virtual environment from the same `python3` you intend to run the
app with. Everything below assumes `.venv` is active; the shell prompt tells you.

`requirements.txt` states what Trellis depends on, loosely.
`requirements-lock.txt` states the exact versions a release was built from.
Develop from the first, build from the second.

## Running

```bash
python3 run.py
```

That is the app: a native window, with Flask serving it on a background thread.
On first launch with no database, it creates one and opens an empty library.

```bash
python3 run_headless.py
```

The same application with no window, for machines that only serve. Configured
entirely by environment variables — `FLUX_PORT`, `FLUX_DB_PATH`,
`SHARE_BASE_URL`, `SECRET_KEY`, `FLUX_COOKIE_NAME`.

### Sharing your library over the internet

Sharing runs as a **second process** in share-only mode:

```bash
SERVER_MODE=true \
SECRET_KEY="$(python3 -c 'import secrets;print(secrets.token_hex(32))')" \
FLUX_PORT=5760 \
TRUSTED_CLIENT_IP_HEADER=CF-Connecting-IP \
python3 run_headless.py
```

`SERVER_MODE` is not a permission setting — it changes what the process
*builds*. Only the peer-facing blueprint is registered, so the login page, the
editing endpoints and the frontend do not exist in that process at all. They
answer 404 because Flask has never heard of them. `tests/test_server_mode_surface.py`
asserts that as a property of the whole route table.

Exposure is via a Cloudflare Tunnel — outbound only, so no router ports are
opened and the home address is never published. `SHARE_BASE_URL` should be the
public hostname; invites are minted as a single `https://address#CODE` string
and are useless without it.

## Tests

```bash
python3 -m pytest tests/ -q
```

594 tests, no network, no audio files, no library mount required. Some check
behaviour; several exist to assert that a specific past bug cannot come back,
and say so in their docstrings.

## Building the app

```bash
./tools/build_macos.sh          # → dist/Trellis.app
```

Must run on macOS — PyInstaller does not cross-compile. The build is unsigned,
so the first launch on any machine is refused by Gatekeeper: right-click →
Open → Open, once.

The icon is generated rather than stored as an opaque asset:

```bash
python3 tools/design/make_icon.py assets/icon
```

## How sharing works, briefly

- **Every install is a node** — both a server and a client. Identity is
  per-node; there is no global account. In your database you are the admin and
  I am a peer; in mine, the reverse.
- **Peers are not users.** A peer authenticates with a bearer token into a
  blueprint that contains no editing endpoints at all — it is structurally
  incapable of changing anything, rather than relying on every check to
  remember to exclude peers.
- **Grants are collection-level.** Whole-library sharing is itself a collection,
  resolved by query rather than by membership rows.
- **Catalog metadata versus holdings.** A peer sees any performer, venue or
  artist page in full — that is reference data about the world. Every *list of
  recordings* on it is filtered to what they were granted.
- **Your listening history and your favourites stay home.** A peer gets their
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
  models/               SQLAlchemy models
  utils/                ingest, quality scoring, checksums, transcode, peer auth
  static/               the frontend — plain HTML/JS/CSS, no build step
scripts/                one-off migrations and backfills, kept after running
tools/                  developer surfaces: build, icon, design specimens
tests/
```

## Conventions worth knowing before changing things

- **The database is the source of truth.** FLAC tags are written only when
  explicitly exported, never automatically.
- **No migration framework.** Alembic was considered and declined. Schema comes
  from `create_all` plus the numbered scripts in `scripts/`, which are kept
  after they run.
- **Destructive filesystem work happens before the database write**, so a disk
  failure leaves the row — and therefore the handle on the folder — intact.
- **There is exactly one frontend gate on editing**, `canEditLibrary()`. New
  editable surfaces go through the shared helpers that honour it; hand-gating
  call sites is how a page stays editable when it should not be.
- **The frontend has no build step.** It is served as written.

The reasoning behind decisions that look arbitrary — and the traps that have
already cost someone a day — is kept outside this repo, in `CONTEXT.md`
alongside the meeting notes.
