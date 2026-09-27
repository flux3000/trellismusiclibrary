# Listening Quality

Standalone analyser for how enjoyable a live recording is to listen to.
Runs independently of the app — own server, own UI, no database.

## Run it

```bash
cd ~/Workshop/dev/trellis/tools/quality
pip3 install flask numpy scipy soundfile pyloudnorm --break-system-packages
python3 quality_app.py
```

Then open <http://127.0.0.1:5055> and paste a folder path.

Point it at a **single show** or a **whole artist folder** — every recording
underneath gets found and analysed. Roughly 15 seconds each.

```bash
python3 quality_app.py --port 8080          # different port
python3 quality_app.py --host 0.0.0.0       # reachable from the LAN
```

## What it does

Decodes about 2 minutes of audio per recording — 3 tracks x 2 windows x 20 s —
and produces a 0-100 **Listening Quality** score from three groups:

| Group | Weight | What it measures |
|---|---|---|
| **Tone** | 50% | boominess, tinniness, hollowness — frequency balance |
| **Noise** | 30% | hiss, mains hum, background noise |
| **Dynamics** | 20% | how squashed vs. how open the loud-to-quiet range is |

Separately, **Technical Issues** (clipping, dead channel, out of phase,
dropouts) are pass/fail and only deduct when they actually trip.

Every score comes with a plain-English sentence from a fixed band ruleset, so
explanations always match the numbers they describe.

You can stream the exact tracks that were analysed straight from the results,
and click a timestamp to jump to the precise window that was measured, so
every finding can be checked by ear.

## Files

The engine lives in **`app/utils/quality/`**, shared with the app's ingestion
flow. Everything here is a thin client of that package.

| File | Purpose |
|---|---|
| `quality_app.py` | Flask server — scanning, jobs, audio streaming |
| `quality_app.html` | The whole UI, single file, the app's design tokens |
| `run_extract.py` | CLI batch extraction to JSON |
| `track_variance.py` | Diagnostic: feature spread across tracks in one recording |
| `width_probe.py` | Diagnostic: stereo width (not used in scoring) |

| Engine (in `app/utils/quality/`) | Purpose |
|---|---|
| `quality_features.py` | Audio → raw measurements |
| `quality_scoring.py` | Measurements → scores (pure function, no audio) |
| `quality_interpret.py` | Scores → plain English, band-driven |

Feature extraction and scoring are separate: scoring is a pure
function over stored measurements, so curves and weights can be retuned without
decoding any audio again.

These scripts put the repo root on `sys.path` and import `app.utils.quality`,
so they must be run from a checkout with the app's dependencies available.

## Accuracy

Against 20 human-graded recordings across three artists (Allman Brothers Band,
the Di Meola/McLaughlin/de Lucía trio, and Bill Evans Trio): **average error 4.9
points, worst case 7.9, correlation 0.857.**

Known limitation: it separates good from bad well (correlation 0.885 on the
corpus spanning C to A) but ranks good-from-good poorly (0.34 on the two
corpora that are all A/A−/B+).

## Notes

- Handles un-flattened recordings with `CD1/`/`CD2/` subdirectories.
- Track selection uses no database flags — drop first and last, keep tracks
  4 minutes or longer, take 3 spread across the running order. Duration alone
  separates music from banter and tuning reliably.
- The streaming endpoint only serves files inside a folder analysed this
  session.
