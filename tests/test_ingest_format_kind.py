"""
tests/test_ingest_format_kind.py -- FORMAT and TYPE (2026-09-27) reach
batch_scan() (Bulk Import, #/batch) and the Review & Ingest staging payload
(app/api/quality.py's _scan_metadata) the same way bulk_ingest.py's
extract()/classify() compute them for Bulk Ingest, so all three ingest
queue tables show matching pills for the same folder. See
app/utils/bulk_ingest.py's folder_format()/classify_kind().
"""
import numpy as np
import soundfile as sf
from mutagen.flac import FLAC

from app.api.quality import _scan_metadata


def _flac_with_tags(path, **tags):
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.zeros(4410, dtype="int16"), 44100, format="FLAC")
    audio = FLAC(str(path))
    for k, v in tags.items():
        audio[k] = v
    audio.save()


# -- Staging payload (Review & Ingest) ---------------------------------------

def test_scan_metadata_yields_format_and_kind_for_a_studio_album(app, tmp_path):
    # _scan_metadata now sources its fields from resolve() (Ingest Field
    # Resolver spec v1, Fix 2) instead of a standalone tag/info merge, and
    # resolve()'s artist rule reads LIBRARY_ROOT / node_settings, both of
    # which need a real app + DB context -- the same context every one of
    # its real callers (Review & Ingest's own route) already has.
    show = tmp_path / "show"
    _flac_with_tags(show / "01.flac", ARTIST="Phish", ALBUM="A Picture of Nectar")
    _flac_with_tags(show / "02.flac", ARTIST="Phish", ALBUM="A Picture of Nectar")

    with app.app_context():
        app.config["LIBRARY_ROOT"] = str(tmp_path)
        payload = _scan_metadata(str(show))
    assert payload["extracted"]["format"] == "FLAC"
    assert payload["extracted"]["kind"] == "studio"


def test_scan_metadata_yields_live_kind_with_a_date(app, tmp_path):
    show = tmp_path / "show"
    _flac_with_tags(show / "01.flac", ARTIST="Phish", DATE="1995-07-08",
                     VENUE="Deer Creek")

    with app.app_context():
        app.config["LIBRARY_ROOT"] = str(tmp_path)
        payload = _scan_metadata(str(show))
    assert payload["extracted"]["format"] == "FLAC"
    assert payload["extracted"]["kind"] == "live"


# -- batch_scan() (Bulk Import, #/batch) -------------------------------------

def test_batch_scan_yields_format_and_kind_for_a_studio_album(app, tmp_path):
    app.config["LOGIN_DISABLED"] = True
    client = app.test_client()

    src = tmp_path / "Import"
    show = src / "The Band - The Band"
    _flac_with_tags(show / "01.flac", ARTIST="The Band", ALBUM="The Band")
    _flac_with_tags(show / "02.flac", ARTIST="The Band", ALBUM="The Band")

    resp = client.post("/api/ingest/batch-scan", json={"source_dir": str(src)})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    items = resp.get_json()["items"]
    assert len(items) == 1
    assert items[0]["extracted"]["format"] == "FLAC"
    assert items[0]["extracted"]["kind"] == "studio"


def test_batch_scan_yields_live_kind_with_a_date(app, tmp_path):
    app.config["LOGIN_DISABLED"] = True
    client = app.test_client()

    src = tmp_path / "Import"
    show = src / "Phish - 1995-07-08"
    _flac_with_tags(show / "01.flac", ARTIST="Phish", DATE="1995-07-08",
                     VENUE="Deer Creek")

    resp = client.post("/api/ingest/batch-scan", json={"source_dir": str(src)})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    items = resp.get_json()["items"]
    assert len(items) == 1
    assert items[0]["extracted"]["format"] == "FLAC"
    assert items[0]["extracted"]["kind"] == "live"
