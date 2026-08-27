# Trellis Music Library

Trellis is a music library manager for live recording collectors and aficionados.

---

## Download

Grab the latest build from [**Releases**](https://github.com/flux3000/trellismusiclibrary/releases)
— no Python, no command line required.

The app isn't signed yet, so macOS blocks it outright the first time, with
no Open option in sight. Two ways past it, once, and only once:

- **Terminal:** `xattr -dr com.apple.quarantine "/path/to/Trellis Music Library.app"`,
  then open it normally.
- **No Terminal:** try to open it, dismiss the blocked dialog, then go to
  System Settings → Privacy & Security → scroll down to the blocked-app
  notice → **Open Anyway**. Open the app again and this time there's a real
  Open button.

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
