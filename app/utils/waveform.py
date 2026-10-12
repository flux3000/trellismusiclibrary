"""utils/waveform.py — Downsample a track's stored peak envelope for compact
display.

Pure function: no audio decode, no Flask, no filesystem. Takes whatever is
already sitting in TrackAnalysis.waveform_json (already parsed from JSON by
the caller) and buckets it down to `n` magnitude values (0.0-1.0), one per
bar of the strip.

Accepts both analysis shapes that exist in the live DB (see
app/utils/analysis.py):
  v2 (current) — {"min": [...], "max": [...]}, signed, -1.0.1.0, ~2000 points
  v1 (legacy)   — flat list of unsigned 0.0.1.0 RMS magnitudes, ~300 points
Older v1 rows are still real usable peaks (
section) — not re-decoded, just accepted as-is.
"""


def _to_magnitudes(peaks):
    """Normalize either accepted shape into a flat list of magnitudes (>=0)."""
    if not peaks:
        return []
    if isinstance(peaks, dict):
        mins = peaks.get("min") or []
        maxs = peaks.get("max") or []
        length = max(len(mins), len(maxs))
        return [
            max(
                abs(mins[i]) if i < len(mins) else 0.0,
                abs(maxs[i]) if i < len(maxs) else 0.0,
            )
            for i in range(length)
        ]
    # Flat list (v1, or already-magnitude data).
    return [abs(v) for v in peaks]


def downsample_peaks(peaks, n=100):
    """
    Bucket a track's peak envelope down to `n` magnitude values.

    - Empty/missing input  → n zeros (a flat strip, not an error).
    - Input shorter than n → nearest-index upsample so the strip still fills
      the requested width.
    - Input length == n    → passthrough (magnitude only).
    - Input longer than n  → n contiguous buckets, each reduced to its max
      magnitude (a waveform strip's job is to show the loudest point in each
      slice, not the average — averaging would smooth away transients).
    """
    if n <= 0:
        return []

    magnitudes = _to_magnitudes(peaks)
    total = len(magnitudes)
    if total == 0:
        return [0.0] * n

    if total <= n:
        return [magnitudes[min(int(i * total / n), total - 1)] for i in range(n)]

    buckets = []
    for i in range(n):
        start = int(i * total / n)
        end = int((i + 1) * total / n)
        if end <= start:
            end = start + 1
        chunk = magnitudes[start:end]
        buckets.append(max(chunk) if chunk else 0.0)
    return buckets
