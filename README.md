# Trellis Music Library

Trellis is a music library manager for live recording collectors and aficionados.

---

## Download

Grab the latest `.dmg` from [**Releases**](https://github.com/flux3000/trellismusiclibrary/releases),
drag it to Applications, and open it.

The rest of this README is for building from source.

## Requirements

- macOS 12+, with **Python 3.11+ installed as a framework build**.
- **ffmpeg**, if you intend to share your library (peers stream MP3,
  transcoded on first play; a listener doesn't need it).
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

Running from a source checkout, you'll hit a real login screen. The
installed app signs itself in automatically since there's nothing to log
in to on your own machine, so `first_run_setup()` creates one admin
account with a password that's generated and thrown away on the spot.
Nobody, including the app, knows it. From source, set one of these so the
same auto-login applies:

```bash
SINGLE_USER_DESKTOP=true python3 run.py
```

`DEV_MODE=true` does the same thing and also turns on debug logging.
Neither is needed once the app is built: the packaged `.app` sets this
automatically and the login screen never appears.

Headless mode is configured by environment variables: `TRELLIS_PORT`,
`TRELLIS_DB_PATH`, `SHARE_BASE_URL`, `SECRET_KEY`, `TRELLIS_COOKIE_NAME`.

Sharing over the internet runs as a **second process**, in share-only mode
(`SERVER_MODE=true`). It binds to `127.0.0.1`, so reaching it from another
machine means putting a tunnel or a VPN in front of it yourself. Trellis has no
opinion about which.

## Tests

```bash
python3 -m pytest tests/ -q
```

No network, no audio files, no library mount required.

## Building the app

```bash
./tools/build_macos.sh          # → dist/Trellis Music Library.app
```

Must run on macOS. PyInstaller doesn't cross-compile.

## How sharing works, briefly

Every install is both a server and a client. Identity is per-node, and there's
no global account. Peers authenticate with a bearer token into a blueprint with
no editing endpoints at all, so a peer cannot change anything. A peer gets your
whole library: they see full catalog pages, with every list of recordings
filtered to what they can reach. Your favorites never travel. A library you join
records what you stream, on its owner's machine.

## License

Copyright (c) 2026 Ryan Baker. All rights reserved. See [LICENSE](LICENSE).
