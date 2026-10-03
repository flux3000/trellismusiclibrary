"""
utils/audio_convert.py -- unsupported audio (SHN, WAV, AIFF, APE, WV) to FLAC.

Only FLAC and MP3 are ever imported (Ryan, 2026-10-02), so every other
lossless container is "unsupported" and Convert to FLAC is how it becomes
importable.

## The rules that are not obvious

**Bit depth is preserved, never forced.**  A 24-bit taper's master converted
to "FLAC 16-bit" has quietly lost a third of its resolution, and FLAC is
lossless at either depth.  ffmpeg keeps the source's sample format for FLAC
output, so the encode passes no `-sample_fmt` at all.

**Compression level 8**, fixed (Ryan, 2026-10-02): smallest files, and the
encode time is irrelevant next to reading the source.

**Originals are deleted, but only once their FLAC is proven.**  Each file is
encoded to a `.part` name, checked (ffmpeg exited cleanly and the output is
non-empty), renamed into place, and only then is the source removed.  A file
that fails keeps its original and is reported; a re-run skips what is done.

**A mixed folder converts only its unsupported files.**  Existing FLAC/MP3
are never touched.
"""

import os
import subprocess

# Legacy: earlier versions kept originals here. Still skipped when walking so
# a folder converted by an old build is not re-offered its own leftovers.
ORIGINALS_DIRNAME = "_originals"

# What we can convert FROM: every format Trellis refuses to import.
CONVERTIBLE_EXTS = (".shn", ".wav", ".aiff", ".aif", ".ape", ".wv")
FLAC_COMPRESSION_LEVEL = "8"


class ConversionUnavailable(RuntimeError):
    """ffmpeg is missing, or cannot decode this format."""


def _walk_audio(folder_path):
    """Relative paths of every file under `folder_path` (scan_folder reads
    subfolders too, e.g. CD1/CD2), skipping hidden and legacy _originals dirs."""
    out = []
    for dirpath, dirnames, filenames in os.walk(folder_path):
        dirnames[:] = sorted(d for d in dirnames
                             if not d.startswith(".") and d != ORIGINALS_DIRNAME)
        for f in sorted(filenames):
            out.append(os.path.relpath(os.path.join(dirpath, f), folder_path))
    return out


def detect_convertible(folder_path):
    """
    Does this folder hold unsupported audio, and what?

    Returns None, or {"kind": "shn"|"wav", "ext": first ext, "exts": [...],
    "count": n}.  `kind` is "shn" when any Shorten is present (the label the
    older UI keys on), else "wav" for every other format.  FLAC beside the
    unsupported files no longer suppresses the offer: a mixed folder is
    exactly the case that needs it.
    """
    counts = {}
    for n in _walk_audio(folder_path):
        e = os.path.splitext(n)[1].lower()
        if e in CONVERTIBLE_EXTS:
            counts[e] = counts.get(e, 0) + 1
    if not counts:
        return None
    exts = [e for e in CONVERTIBLE_EXTS if e in counts]
    return {"kind": "shn" if ".shn" in counts else "wav", "ext": exts[0],
            "exts": exts, "count": sum(counts.values())}


def convertible_files(folder_path, exts):
    """The files this conversion will act on, in stable order (relative
    paths). `exts` is one extension or an iterable of them."""
    if isinstance(exts, str):
        exts = (exts,)
    exts = {e.lower() for e in exts}
    return sorted(
        n for n in _walk_audio(folder_path)
        if os.path.splitext(n)[1].lower() in exts
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


def convert_folder(folder_path, ffmpeg, exts, *, on_progress=None,
                   should_cancel=None):
    """
    Convert every unsupported file (`exts`: one extension or several) under
    `folder_path` to FLAC beside it, deleting each original once its FLAC is
    verified.  A file that fails keeps its original.

    `on_progress(done, total, name)` is called before each file.
    `should_cancel()` is polled between files; a cancelled run leaves whatever
    it finished in place and is safe to re-run.

    Returns {"converted": [names], "failed": [{name, error}], "cancelled": bool}.
    """
    files = convertible_files(folder_path, exts)
    total = len(files)
    converted, failed = [], []

    for i, name in enumerate(files):
        if should_cancel and should_cancel():
            return {"converted": converted, "failed": failed, "cancelled": True}
        if on_progress:
            on_progress(i, total, name)

        src = os.path.join(folder_path, name)
        dst = os.path.splitext(src)[0] + ".flac"
        # A .part name, so an interrupted encode never looks like a finished
        # track. ffmpeg writes the container header first; a killed process
        # otherwise leaves a plausible-looking .flac that fails much later.
        tmp = dst + ".part"

        if os.path.exists(dst):
            # A FLAC with this name that this run did not write: never delete
            # the original on the strength of it. A person decides, so the
            # file is reported and the folder stays unsupported.
            failed.append({"name": name,
                           "error": "A FLAC with this name already exists."})
            continue

        cmd = [
            ffmpeg, "-nostdin", "-y",
            "-i", src,
            # No -sample_fmt: FLAC output inherits the source depth. See the
            # module docstring.
            "-c:a", "flac",
            "-compression_level", FLAC_COMPRESSION_LEVEL,
            # Explicit so a future ffmpeg default change cannot drop tags.
            "-map_metadata", "0",
            # Output name ends in .part, which ffmpeg cannot infer a muxer from.
            "-f", "flac",
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
        converted.append(os.path.relpath(dst, folder_path))
        _unlink(src)

    if on_progress:
        on_progress(total, total, None)
    return {"converted": converted, "failed": failed, "cancelled": False}


def _unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass
