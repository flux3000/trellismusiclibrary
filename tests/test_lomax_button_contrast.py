"""Every palette sets its own --lomax and --lomax-ink. The Ask Lomax label must clear WCAG AA at rest
and on hover, and Lomax text must clear it on every surface. Reads the palette tokens straight out of main.css."""
import re
from pathlib import Path

import pytest

CSS = (Path(__file__).resolve().parent.parent / "app" / "static" / "css" / "main.css").read_text(encoding="utf-8")
PALETTES = ("steely", "krauss", "monk", "joni", "hazel",
            "townes", "chet", "gram", "cale", "miles", "alice", "eno")


def _lum(h):
    h = h.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4   # noqa: E731
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def _ratio(a, b):
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _mix(a, b, pct_a):
    a, b = a.lstrip("#"), b.lstrip("#")
    out = [round(int(a[i:i + 2], 16) * pct_a + int(b[i:i + 2], 16) * (1 - pct_a)) for i in (0, 2, 4)]
    return "#%02x%02x%02x" % tuple(out)


def _tokens(name):
    if name == "cale":
        m = re.search(r':root, :root\[data-palette="cale"\], \[data-pal-preview="cale"\]\s*\{(.*?)\n\}', CSS, re.S)
    else:
        m = re.search(r':root\[data-palette="%s"\], \[data-pal-preview="%s"\]\s*\{(.*?)\n\}' % (name, name), CSS, re.S)
    assert m, name
    return dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})", m.group(1)))


def test_the_button_uses_one_dedicated_token_and_nothing_else_does():
    assert re.search(r"\.lx-go\s*\{[^}]*background:\s*var\(--lx-btn\)", CSS)
    assert len(re.findall(r"var\(--lx-btn\)", CSS)) == 2          # the button, and the hover mix
    assert "--lx-btn: var(--lomax)" in CSS
    assert "btn-primary lx-go" not in (Path(__file__).resolve().parent.parent / "app/static/js/app.js").read_text(encoding="utf-8")


@pytest.mark.parametrize("name", PALETTES)
def test_the_label_clears_aa_on_every_palette_at_rest_and_on_hover(name):
    t = _tokens(name)
    ink = t["lomax-ink"]
    assert _ratio(ink, t["lomax"]) >= 4.5, name
    hover = _mix(t["lomax"], t["t0"], 0.86)
    assert _ratio(ink, hover) >= 4.5, name


def test_every_palette_sets_its_own_lomax_colour_and_the_button_reads_it():
    assert "--lx-btn-ink: var(--lomax-ink)" in CSS
    for name in PALETTES:
        t = _tokens(name)
        assert "lomax" in t and "lomax-ink" in t, name


@pytest.mark.parametrize("name", PALETTES)
def test_lomax_values_clear_aa_on_every_surface_they_sit_on(name):
    """.lx-val is --lomax; .lx-same is --lx-quiet, 88% --lomax mixed toward --t3.
    Both are measured against bg-0 to bg-3, the surfaces Lomax values appear on."""
    assert re.search(r"\.lx-val\s*\{\s*color:\s*var\(--lomax\)", CSS)
    m = re.search(r"--lx-quiet:\s*color-mix\(in srgb, var\(--lomax\) (\d+)%, var\(--t3\)\)", CSS)
    share = int(m.group(1)) / 100
    t = _tokens(name)
    quiet = _mix(t["lomax"], t["t3"], share)
    for bg in ("bg-0", "bg-1", "bg-2", "bg-3"):
        assert _ratio(t["lomax"], t[bg]) >= 4.5, (name, bg, "lx-val")
        assert _ratio(quiet, t[bg]) >= 4.5, (name, bg, "lx-quiet")
