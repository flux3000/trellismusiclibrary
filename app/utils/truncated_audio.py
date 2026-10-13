"""Truncated audio files (Ryan, 2026-10-13): a file the tag reader cannot open
that holds less than about one second of audio after its tags. Typically a
download that died just after the header arrived. The queue row reads
"Truncated file" instead of the reader's raw error."""
import os

_EXTS = (".mp3", ".flac")
_FALLBACK_BYTES_PER_SEC = 40_000          # 320 kbps, the highest common MP3 rate
_MP3_KBPS = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)  # MPEG-1 Layer III


def _id3_size(head):
    if head[:3] != b"ID3" or len(head) < 10:
        return 0
    s = head[6:10]
    return 10 + ((s[0] << 21) | (s[1] << 14) | (s[2] << 7) | s[3])


def _bytes_per_second(data):
    """From the first MPEG-1 Layer III frame header, else the fallback."""
    i = data.find(b"\xff")
    while 0 <= i < len(data) - 3:
        b1, b2 = data[i + 1], data[i + 2]
        if (b1 & 0xFE) == 0xFA:                    # MPEG-1, Layer III
            idx = b2 >> 4
            if 0 < idx < 15:
                return _MP3_KBPS[idx] * 125
        i = data.find(b"\xff", i + 1)
    return _FALLBACK_BYTES_PER_SEC


def is_truncated(path):
    """True when the tag reader fails and under a second of audio remains."""
    try:
        import mutagen
        f = mutagen.File(path)
        if f is not None and getattr(f.info, "length", 0) >= 1:
            return False
    except Exception:  # noqa: BLE001 - any reader failure is the signal
        pass
    try:
        with open(path, "rb") as fh:
            data = fh.read(256 * 1024)
        size = os.path.getsize(path)
    except OSError:
        return False
    audio = data[_id3_size(data):]
    return size - _id3_size(data) < _bytes_per_second(audio)


def truncated_files(folder_abs):
    """Names (relative to the folder) of truncated audio files, sorted."""
    out = []
    for root, _dirs, files in os.walk(folder_abs):
        for name in files:
            if name.lower().endswith(_EXTS) and not name.startswith("._"):
                p = os.path.join(root, name)
                if is_truncated(p):
                    out.append(os.path.relpath(p, folder_abs))
    return sorted(out)
