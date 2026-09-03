"""
utils/audio_convert.py — Shorten (.shn) and WAV → FLAC, in place.

Why this exists at all: a lot of the good early trading material is Shorten,
and Trellis could see those folders but not read them.  `.shn` has been in
RESOLVE_AUDIO_EXTS since 2026-08-26 purely so a folder of it would say
"6 .shn files — not supported yet" instead of vanishing.  This is the other
half: offer to convert, then triage the result (Ryan, 2026-09-02).

WAV is here for a different reason.  It IS ingestable, so nothing is broken —
but a WAV set is roughly twice the size of the same audio as FLAC, losslessly,
and a library that stores its own copy should not carry that.  So WAV is an
OFFER on a folder that works, where SHN is the fix for one that does not.

## The rules that are not obvious

**Bit depth is preserved, never forced.**  A 24-bit taper's master converted
to "FLAC 16-bit" has quietly lost a third of its resolution, and FLAC is
lossless at either depth — there is nothing to gain by choosing.  ffmpeg's
default for FLAC output is to keep the source's sample format, so the encode
passes no `-sample_fmt` at all rather than passing one that happens to match.
Shorten is always 16-bit, so this only ever bites WAV.

**Originals are kept, in `_originals/`.**  Not deleted (a bad conversion would
mean re-downloading a show that may not be seedable any more) and not left
where they were — `resolve_shows`/`scan_folder` only look at audio in the
folder ROOT, so moving them one level down makes them invisible to ingest
while leaving them on the collector's disk.  It also matters for WAV
specifically: `.wav` is in AUDIO_EXTENSIONS, so a folder holding both the
WAVs and the new FLACs would ingest every track TWICE.

**Nothing is destroyed before it is replaced.**  Each file is encoded to a
temporary name, verified non-empty, renamed into place, and only then is the
original moved aside.  A failure part-way through leaves the folder holding
some FLACs and the rest of its originals still where they were — which is a
resumable state, not a broken one, because a re-run skips what it already did.

**A folder with any FLAC in it is not a conversion candidate.**  That is the
one thing that tells "this show needs converting" apart from "this show has
already been converted, or shipped mixed."  Checked by the caller
(`detect_convertible`), which is also what the triage row's offer reads.
"""

import os
import shutil
import subprocess

ORIGINALS_DIRNAME = "_originals"

# What we can convert FROM. Ordered by how much the user gains: SHN cannot be
# ingested at all, WAV merely costs disk.
SHN_EXT = ".shn"
WAV_EXTS = (".wav",)


class ConversionUnavailable(RuntimeError):
    """ffmpeg is missing, or cannot decode this format."""


def _audio_names(folder_path):
    """Immediate file names in `folder_path`, lowercased extension included."""
    try:
        return [f.name for f in os.scandir(folder_path) if f.is_file()]
    except OSError:
        return []


def detect_convertible(folder_path):
    """
    Does this folder want converting, and to what?

    Returns None, or {"kind": "shn"|"wav", "count": int, "ext": ".shn"}.

    The rule is the same for both: files of ONE convertible format in the
    folder root, and no FLAC anywhere in that root.  A folder that already has
    FLAC has either been converted or was mixed to begin with, and in neither
    case is a bulk convert the right offer — it would either duplicate work or
    quietly restructure someone's deliberate arrangement.
    """
    names = _audio_names(folder_path)
    if not names:
        return None
    exts = [os.path.splitext(n)[1].lower() for n in names]
    if ".flac" in exts:
        return None

    shn = sum(1 for e in exts if e == SHN_EXT)
    if shn:
        return {"kind": "shn", "ext": SHN_EXT, "count": shn}

    wav = sum(1 for e in exts if e in WAV_EXTS)
    if wav:
        return {"kind": "wav", "ext": ".wav", "count": wav}

    return None


def convertible_files(folder_path, ext):
    """The files this conversion will act on, in stable order."""
    return sorted(
        n for n in _audio_names(folder_path)
        if os.path.splitext(n)[1].lower() == ext
    )


def probe_decoder(ffmpeg, ext):
    """
    Can this ffmpeg read this format at all?

    Worth asking BEFORE starting a 20-file job.  Shorten is a native FFmpeg
    decoder and present in every ordinary build, including Homebrew's — but
    "ordinary" is not a guarantee, and the failure without this check is 20
    consecutive per-file errors that say nothing about the real cause.
    """
    codec = {".shn": "shorten", ".wav": "pcm_s16le"}.get(ext)
    if not codec:
        return True
    try:
        out = subprocess.run([ffmpeg, "-hide_banner", "-decoders"],
                             capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    return codec in (out.stdout or "")


def convert_folder(folder_path, ffmpeg, ext, *, on_progress=None,
                   should_cancel=None):
    """
    Convert every `ext` file in `folder_path` to FLAC beside it, then move the
    originals into `_originals/`.

    `on_progress(done, total, name)` is called before each file.
    `should_cancel()` is polled between files; a cancelled run leaves whatever
    it finished in place and is safe to re-run.

    Returns {"converted": [names], "failed": [{name, error}], "cancelled": bool}.
    """
    files = convertible_files(folder_path, ext)
    total = len(files)
    converted, failed = [], []

    for i, name in enumerate(files):
        if should_cancel and should_cancel():
            return {"converted": converted, "failed": failed, "cancelled": True}
        if on_progress:
            on_progress(i, total, name)

        src = os.path.join(folder_path, name)
        stem = os.path.splitext(name)[0]
        dst = os.path.join(folder_path, stem + ".flac")
        # A .part name, so an interrupted encode never looks like a finished
        # track. ffmpeg writes the container header first; a killed process
        # otherwise leaves a plausible-looking .flac that fails much later.
        tmp = dst + ".part"

        if os.path.exists(dst):
            # Already done on an earlier run. Not an error — the whole point of
            # converting file by file is that a re-run resumes.
            converted.append(os.path.basename(dst))
            _retire_original(folder_path, name)
            continue

        cmd = [
            ffmpeg, "-nostdin", "-y",
            "-i", src,
            # No -sample_fmt: FLAC output inherits the source depth. See the
            # module docstring — forcing 16-bit would silently downsample a
            # 24-bit master.
            "-c:a", "flac",
            "-compression_level", "5",
            # Tags carry across on their own for WAV; SHN has none. Explicit so
            # a future ffmpeg default change cannot drop them.
            "-map_metadata", "0",
            tmp,
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        except (OSError, subprocess.SubprocessError) as e:
            _unlink(tmp)
            failed.append({"name": name, "error": str(e)})
            continue

        if proc.returncode != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
            _unlink(tmp)
            err = (proc.stderr or "").strip().splitlines()
            failed.append({"name": name,
                           "error": err[-1] if err else f"ffmpeg exited {proc.returncode}"})
            continue

        os.replace(tmp, dst)
        converted.append(os.path.basename(dst))
        _retire_original(folder_path, name)

    if on_progress:
        on_progress(total, total, None)
    return {"converted": converted, "failed": failed, "cancelled": False}


def _retire_original(folder_path, name):
    """
    Move one source file into `_originals/`, out of the ingest's sight.

    Failure is swallowed deliberately: the FLAC is already written and correct,
    and refusing to report a successful conversion because a file could not be
    tidied away would be the tail wagging the dog. The consequence of a failed
    move for WAV — a double ingest — is caught by the caller re-scanning, which
    will see the leftover and decline to offer a second conversion.
    """
    src = os.path.join(folder_path, name)
    if not os.path.isfile(src):
        return
    dest_dir = os.path.join(folder_path, ORIGINALS_DIRNAME)
    try:
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, name)
        if os.path.exists(dest):
            return          # a previous run already retired this one
        shutil.move(src, dest)
    except OSError:
        pass


def _unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass
