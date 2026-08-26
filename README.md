# Trellis Music Library

A home for a live music collection — built first for the people who keep one.

Trellis is for collectors of **ROIO** (Recordings of Independent Origin): the
live concert tapes that taper-friendly artists have long permitted fans to
record and trade, on the understanding that no money ever changes hands.

It's an **archivist's tool** — ingest a folder of FLACs, and Trellis reads the
taper's info file and files the show properly, with metadata editing and
quality scoring built in. And it's a **listener's app** — browse a library by
performer, venue, artist, genre, or date, and play what you find.

The two meet at **peer sharing**. Every install is a full node — your own
library, your own database, no central server and no company account in the
middle. Invite someone and they browse and stream your collection from their
own copy of Trellis, with no ability to change anything on your end. You
don't have to be a collector to use it that way — and because a Trellis
library is just files and a database someone owns, it doesn't depend on any
platform staying online.

---

## Download

Grab the latest build from [**Releases**](https://github.com/flux3000/trellismusiclibrary/releases)
— no Python, no command line required.

Builds are currently unsigned, so the first launch is refused by Gatekeeper:
right-click the app → **Open** → **Open**, once per machine.

The rest of this README is for building from source.

## Requirements

- macOS 12+, with **Python 3.11+ installed as a framework build**.
- **ffmpeg**, if you intend to share your library (peers stream MP3,
  transcoded on first play — a listener doesn't need it).
- Somewhere to keep the audio. Trellis never moves or rewrites your files
  except when you explicitly ask it to.

## Setup

```bash
git clone https://github.com/flux3000/trellismusiclibrary.git trellis
cd trellis
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

`requirements.txt` states what Trellis depends on; `requirements-lock.txt`
states the exact versions a given release was built from.

## Running

```bash
python3 run.py            # native window, desktop app
python3 run_headless.py   # no window, for a machine that only serves
```

Headless mode is configured by environment variables: `FLUX_PORT`,
`FLUX_DB_PATH`, `SHARE_BASE_URL`, `SECRET_KEY`, `FLUX_COOKIE_NAME`.

Sharing over the internet runs as a **second process**, in share-only mode
(`SERVER_MODE=true`), exposed via an outbound Cloudflare Tunnel — no router
ports opened, home address never published. See `run_headless.py --help`.

## Tests

```bash
python3 -m pytest tests/ -q
```

No network, no audio files, no library mount required.

## Building the app

```bash
./tools/build_macos.sh          # → dist/Trellis Music Library.app
```

Must run on macOS — PyInstaller doesn't cross-compile.

## How sharing works, briefly

Every install is both a server and a client — identity is per-node, there's
no global account. Peers authenticate with a bearer token into a blueprint
with no editing endpoints at all, so a peer is structurally incapable of
changing anything. Sharing is collection-level (your whole library is itself
a collection); a peer sees full catalog pages, but every list of recordings
on them is filtered to what they've been granted. Your own listening history
and favorites never leave your machine.

## License

MIT — see [LICENSE](LICENSE).
