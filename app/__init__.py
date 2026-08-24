"""
app/__init__.py — Flask application factory.

Usage:
    from app import create_app
    app = create_app()
"""

from flask import Flask
from config import Config, DEV_SECRET_DEFAULT
from app.extensions import db, login_manager


def _validate_server_mode(app):
    """
    Boot-time guards for SERVER_MODE (a shared/public deployment, as opposed
    to a single-user local machine). Refuses to start rather than boot into
    a known-insecure configuration.
    """
    if not app.config.get("SERVER_MODE"):
        return

    if app.config.get("DEV_MODE"):
        raise RuntimeError(
            "Refusing to boot: SERVER_MODE and DEV_MODE are both enabled. "
            "DEV_MODE auto-logs-in the first admin user with no credentials — "
            "combined with SERVER_MODE (a publicly reachable instance), this "
            "is an open admin panel. Unset DEV_MODE or disable SERVER_MODE."
        )

    secret_key = app.config.get("SECRET_KEY")
    if not secret_key or secret_key == DEV_SECRET_DEFAULT:
        raise RuntimeError(
            "Refusing to boot: SERVER_MODE is enabled but SECRET_KEY is "
            "unset or is the known dev default. Set a unique, unpredictable "
            "SECRET_KEY in the environment before running in SERVER_MODE."
        )


def create_app(config_class=Config):
    app = Flask(__name__, static_folder="static")
    app.config.from_object(config_class)

    _validate_server_mode(app)

    # ── Initialize extensions ──────────────────────────────────
    db.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"

    # ── Unauthenticated requests (2026-08-08) ──────────────────
    # Without this, flask_login 302s an unauthenticated caller to `login_view`
    # — which is auth.login, a POST-ONLY route. No rule matches GET there, so
    # Flask falls through to serve_frontend's catch-all and returns index.html
    # with status 200. An API call that failed authentication therefore
    # answered "200 OK" with a page of HTML.
    #
    # That is not merely untidy:
    #   * the frontend detects logged-out only because res.json() chokes on
    #     the HTML — it works by accident, not by design
    #   * the peer-door probe read it as a peer token reaching the admin API
    #   * the milestone-2 consumer proxy has to distinguish "auth failed" from
    #     "success" WITHOUT sniffing the body, which a 200 makes impossible
    #
    # So: JSON 401 for anything under /api/, and a redirect to the SPA root
    # for everything else (the SPA shows its own login screen).
    @login_manager.unauthorized_handler
    def _unauthorized():
        from flask import request as _req, jsonify as _jsonify, redirect as _redirect
        if _req.path.startswith("/api/"):
            return _jsonify({"error": "Authentication required"}), 401
        return _redirect("/")

    # ── Register blueprints ────────────────────────────────────
    from app.api.auth         import bp as auth_bp
    from app.api.artists      import bp as artists_bp
    from app.api.performers    import bp as performers_bp
    from app.api.collections   import bp as collections_bp
    from app.api.performances import bp as performances_bp
    from app.api.recordings   import bp as recordings_bp
    from app.api.tracks       import bp as tracks_bp
    from app.api.stream       import bp as stream_bp
    from app.api.ingest       import bp as ingest_bp
    from app.api.venues       import bp as venues_bp
    from app.api.genres       import bp as genres_bp
    from app.api.events       import bp as events_bp
    from app.api.debug        import bp as debug_bp
    from app.api.preferences  import bp as preferences_bp
    from app.api.share        import bp as share_bp
    from app.api.peers        import bp as peers_bp
    from app.api.remotes      import bp as remotes_bp
    from app.api.remote_favorites import bp as remote_favorites_bp
    from app.api.quality      import bp as quality_bp
    from app.api.search       import bp as search_bp
    from app.api.system       import bp as system_bp

    app.register_blueprint(auth_bp,         url_prefix="/api/auth")
    app.register_blueprint(artists_bp,      url_prefix="/api/artists")
    app.register_blueprint(performers_bp,   url_prefix="/api/performers")
    app.register_blueprint(collections_bp,  url_prefix="/api/collections")
    app.register_blueprint(performances_bp, url_prefix="/api/performances")
    app.register_blueprint(recordings_bp,   url_prefix="/api/recordings")
    app.register_blueprint(tracks_bp,       url_prefix="/api/tracks")
    app.register_blueprint(stream_bp,       url_prefix="/api/stream")
    app.register_blueprint(ingest_bp,       url_prefix="/api/ingest")
    app.register_blueprint(venues_bp,       url_prefix="/api/venues")
    app.register_blueprint(genres_bp,       url_prefix="/api/genres")
    app.register_blueprint(events_bp,       url_prefix="/api/events")
    app.register_blueprint(debug_bp,        url_prefix="/api/debug")
    app.register_blueprint(preferences_bp,  url_prefix="/api/preferences")
    app.register_blueprint(share_bp,        url_prefix="/api/share")
    app.register_blueprint(peers_bp,        url_prefix="/api/peers")
    app.register_blueprint(remotes_bp,      url_prefix="/api/remotes")
    # Local by definition — MY favourites inside libraries I have joined. Its
    # own prefix so it can never be mistaken for, or shadowed by, the generic
    # /api/remotes/<id>/<path> proxy.
    app.register_blueprint(remote_favorites_bp, url_prefix="/api/remote-favorites")
    app.register_blueprint(quality_bp,      url_prefix="/api/quality")
    app.register_blueprint(search_bp,       url_prefix="/api/search")
    app.register_blueprint(system_bp,       url_prefix="/api/system")

    # ── Dev mode: auto-login as first admin ───────────────────
    if app.config.get("DEV_MODE"):
        from flask_login import login_user, current_user
        from flask import request as _req

        @app.before_request
        def dev_auto_login():
            if current_user.is_authenticated:
                return
            # Skip for static assets
            if _req.path.startswith("/css") or _req.path.startswith("/js"):
                return
            admin = db.session.query(User).filter_by(role="admin", is_active=True).first()
            if admin:
                login_user(admin, remember=True)

    # ── Serve the frontend SPA ─────────────────────────────────
    from flask import send_from_directory
    import os

    @app.route("/", defaults={"path": ""})
    @app.route("/<path:path>")
    def serve_frontend(path):
        static_dir = os.path.join(app.root_path, "static")
        if path and os.path.exists(os.path.join(static_dir, path)):
            return send_from_directory(static_dir, path)
        return send_from_directory(static_dir, "index.html")

    return app


# Flask-Login user loader
from app.extensions import login_manager
from app.models.user import User
from app.models.track_analysis import TrackAnalysis  # noqa: F401 — ensures table is created

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))
