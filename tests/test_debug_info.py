"""GET /api/debug/info: admin only, carries app / schema / startup / backups / update / paths."""
from app.extensions import db as _db
from app.models.user import User


def _login(client, role):
    user = _db.session.query(User).filter_by(role=role).first()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


def test_info_has_version_keys(app):
    client = app.test_client()
    _login(client, "admin")
    r = client.get("/api/debug/info")
    assert r.status_code == 200
    d = r.get_json()
    for k in ("app", "schema", "startup", "backups", "update", "paths", "counts"):
        assert k in d
    assert d["startup"] is None  # upgrade is skipped under TESTING
    assert isinstance(d["schema"]["pending"], list)
    assert isinstance(d["backups"], list)
    for k in ("version", "name", "installed", "python", "sqlite", "alembic"):
        assert k in d["app"]
    for k in ("data_dir", "database", "library_root"):
        assert k in d["paths"]


def test_info_non_admin_gets_404(app):
    _db.session.add(User(username="listener", role="listener", is_active=True, password_hash="x"))
    _db.session.commit()
    client = app.test_client()
    _login(client, "listener")
    assert client.get("/api/debug/info").status_code == 404
