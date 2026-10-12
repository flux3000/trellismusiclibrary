"""Listening Quality engine.

Three layers, deliberately separated:

    quality_features   audio  -> raw measurements   (slow, decodes audio)
    quality_scoring    measurements -> scores       (PURE, no audio)
    quality_interpret  scores -> plain English      (PURE, band-driven)

The separation is load-bearing, not tidiness: re-weighting the score is a
re-score pass over stored features with no audio decode, which is why
`QUALITY_ANALYSIS_VERSION` and `QUALITY_SCORE_VERSION` are two different
constants that gate two different kinds of recompute.

Depends on numpy / scipy / soundfile / pyloudnorm. Librosa is deliberately NOT
used by this engine (the app uses it elsewhere, in utils/analysis.py).
"""

from .quality_features import (           # noqa: F401
    extract_recording_features,
    analyse_window,
    read_window,
    window_offsets,
    select_tracks,
    QUALITY_ANALYSIS_VERSION,
)
from .quality_scoring import (            # noqa: F401
    score_recording,
    score_tone,
    score_noise,
    score_dynamics,
    technical_issues,
    informational_flags,
    verdict_band,
    predicted_grade,
    guess_source_from_name,
    GROUP_WEIGHTS,
    SOURCE_OFFSET,
    BAND_LABEL,
    QUALITY_SCORE_VERSION,
)
from .quality_interpret import (          # noqa: F401
    interpret,
    interpret_full,
    band,
    metric_rows,
)

__all__ = [
    "extract_recording_features", "analyse_window", "read_window",
    "window_offsets", "select_tracks", "QUALITY_ANALYSIS_VERSION",
    "score_recording", "score_tone", "score_noise", "score_dynamics",
    "technical_issues", "informational_flags", "GROUP_WEIGHTS",
    "verdict_band", "predicted_grade", "guess_source_from_name", "SOURCE_OFFSET", "BAND_LABEL",
    "QUALITY_SCORE_VERSION",
    "interpret", "interpret_full", "band", "metric_rows",
]
