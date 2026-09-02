"""
app/utils/entity_images.py — image behaviour shared by Performer and Venue.

Ryan's call (2026-08-07): PARALLEL TABLES, shared LOGIC. `performer_image` and
`venue_image` are separate tables so each keeps a real foreign key — this
project turned FK enforcement on deliberately in July, and a polymorphic
(entity_type, entity_id) pair cannot be enforced by SQLite at all. What is NOT
duplicated is the behaviour: one-primary maintenance, promotion on delete, and
the upload/serve/delete endpoint bodies all live here, once.

Each image model declares `__parent_fk__` (the column naming its owner), which
is the only thing that differs between them. That single hook is what lets
`set_primary()` work on either without a model argument or an isinstance ladder.

PRIMARY IS ENFORCED IN APP LOGIC. SQLite can't express "at most one row per
parent with is_primary = 1" as a partial unique index portably through
SQLAlchemy's `create_all`, so `set_primary()` clearing its siblings in the same
transaction IS the constraint. It is the only sanctioned way to set the flag.
"""

import os
import secrets

from flask import jsonify, request, send_file
from werkzeug.utils import secure_filename

from app.extensions import db

ALLOWED_IMAGE_EXTS = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                      ".png": "image/png", ".webp": "image/webp"}


def _parent_col(model):
    """The InstrumentedAttribute naming this image model's owner."""
    return getattr(model, model.__parent_fk__)


def set_primary(image):
    """
    Make `image` its parent's primary, clearing whichever sibling held it.

    Does NOT commit — callers own the transaction, matching every other
    mutation helper in the app.
    """
    model = type(image)
    parent_id = getattr(image, model.__parent_fk__)
    (db.session.query(model)
     .filter(_parent_col(model) == parent_id,
             model.id != image.id,
             model.is_primary.is_(True))
     .update({"is_primary": False}, synchronize_session=False))
    image.is_primary = True
    return image


def primary_for(model, parent_id):
    """
    The parent's primary image, or its oldest image if none is flagged.

    The fallback matters: deleting the primary must not leave an entity with
    photos but no face on its card. Callers get a usable image whenever one
    exists at all, so `is_primary` is a preference rather than a precondition.
    """
    return (db.session.query(model)
            .filter(_parent_col(model) == parent_id)
            .order_by(model.is_primary.desc(), model.sort_order, model.id)
            .first())


def image_payload(img, url_prefix):
    """One image's JSON shape. `url_prefix` is e.g. '/api/venues/images'."""
    return {
        "id":         img.id,
        "is_primary": bool(img.is_primary),
        "origin":     img.origin,
        "caption":    img.caption,
        "credit":     img.credit,
        "url":        f"{url_prefix}/{img.id}",
    }


# ── Endpoint bodies ─────────────────────────────────────────────────────────
# Written once and parameterised, so Performer and Venue photo management can
# never drift apart in behaviour — only in which table they write to.

def handle_upload(parent, model, images_dir, url_prefix):
    """
    Accept one or more `image` parts. Returns a Flask response tuple.

    The FIRST image an entity ever gets becomes primary automatically — a fresh
    upload should not leave a card faceless pending a second click.

    Partial success is a 200 with an `errors` list, NOT a blanket 400: a drop of
    five photos where one is a .heic should land the other four, and the
    rejected one must say why rather than vanishing.
    """
    files = [f for f in request.files.getlist("image") if f and f.filename]
    if not files:
        return jsonify({"error": "No image file provided"}), 400

    images_dir.mkdir(parents=True, exist_ok=True)
    existing = list(parent.images)
    had_any = bool(existing)
    next_order = max((i.sort_order for i in existing), default=-1) + 1

    created, errors = [], []
    for f in files:
        ext = os.path.splitext(secure_filename(f.filename))[1].lower()
        if ext not in ALLOWED_IMAGE_EXTS:
            errors.append(f"{f.filename}: unsupported type '{ext}' — use jpg, png, or webp")
            continue
        # Random basename, not the uploaded one: two files called cover.jpg from
        # different folders must not collide, and the original name carries no
        # meaning here (unlike audio, where collectors encode lineage in it).
        fname = f"img_{secrets.token_hex(6)}{ext}"
        f.save(str(images_dir / fname))
        img = model(**{model.__parent_fk__: parent.id},
                    filename=fname, ext=ext, sort_order=next_order,
                    origin="upload")
        next_order += 1
        db.session.add(img)
        created.append(img)

    if not created:
        return jsonify({"error": "; ".join(errors)}), 400

    db.session.flush()          # ids are needed before set_primary can compare
    if not had_any:
        set_primary(created[0])
    db.session.commit()

    out = {"images": [image_payload(i, url_prefix) for i in created]}
    if errors:
        out["errors"] = errors
    return jsonify(out)


def handle_serve(img, images_dir):
    if not img:
        return jsonify({"error": "No image"}), 404
    path = images_dir / img.filename
    if not path.exists():
        return jsonify({"error": "Image file missing on disk"}), 404
    return send_file(str(path), mimetype=ALLOWED_IMAGE_EXTS.get(img.ext, "image/jpeg"))


def handle_delete(img, images_dir):
    """
    Delete one image, promoting a survivor if the primary was removed.

    Promotion is deliberate rather than relying on primary_for()'s fallback:
    without it `is_primary` quietly stops meaning anything and the Set-primary
    UI shows nothing selected.
    """
    model = type(img)
    parent_id = getattr(img, model.__parent_fk__)
    was_primary = bool(img.is_primary)
    path = images_dir / img.filename

    db.session.delete(img)
    db.session.flush()

    if was_primary:
        survivor = (db.session.query(model)
                    .filter(_parent_col(model) == parent_id)
                    .order_by(model.sort_order, model.id)
                    .first())
        if survivor:
            set_primary(survivor)

    db.session.commit()

    # File removed AFTER the commit: a failed unlink then leaves a harmless
    # orphan rather than a row pointing at nothing, and the reverse order would
    # show a broken image if the commit rolled back.
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass

    return jsonify({"ok": True})


# ── Route registrar (2026-09-01) ─────────────────────────────────────────────
#
# The endpoint BODIES have been shared since 2026-08-07; the five route
# DECLARATIONS around them were still copy-pasted per blueprint. With four
# photographed entities (Performer, Venue, Artist, Event) that is four places
# to forget `require_library`, four URL shapes to get subtly different, and
# four chances for a `has_image` to mean something slightly else.
#
# So the declarations move here too. One call per blueprint mounts the whole
# standard set:
#
#     GET    <bp>/<id>/images            list
#     POST   <bp>/<id>/images            upload (multipart, repeated `image`)
#     GET    <bp>/images/<image_id>      serve bytes
#     POST   <bp>/images/<image_id>/primary
#     DELETE <bp>/images/<image_id>
#
# Note the asymmetry, which is deliberate and predates this: list/upload are
# addressed by PARENT, everything else by IMAGE ID. A parent now has several
# images, so "the venue's image" no longer identifies one.
#
# api/performers.py deliberately does NOT use this. It carries two extra routes
# (the Wikimedia Commons fetch and a PUT for caption/credit) whose bodies are
# not shared, and its wrappers are already thin. Rewriting a working, tested
# surface to save five decorators is the wrong trade; if a fifth photographed
# entity ever appears, revisit.


def register_image_routes(bp, *, parent_model, image_model, url_prefix,
                          images_dir_for, login_required, require_library):
    """
    Mount the five standard photo routes on `bp`.

    parent_model    the owning model (Venue, Artist, Event)
    image_model     its image table (VenueImage, ArtistImage, EventImage)
    url_prefix      public prefix for image URLs, e.g. '/api/venues/images'
    images_dir_for  fn(parent) -> pathlib.Path of that parent's _images dir
    login_required / require_library
                    passed in rather than imported, so this module stays free
                    of a Flask-Login and app.api.system dependency and can be
                    unit-tested on its own.

    Endpoint names are namespaced by the blueprint, so two blueprints
    registering these cannot collide.
    """
    kind = parent_model.__tablename__          # 'venue', 'artist', 'event'

    def _parent_or_404(parent_id):
        return db.session.get(parent_model, parent_id)

    @bp.route(f"/<int:{kind}_id>/images", methods=["GET"],
              endpoint=f"list_{kind}_images")
    @login_required
    def _list(**kw):
        parent = _parent_or_404(kw[f"{kind}_id"])
        if not parent:
            return jsonify({"error": "Not found"}), 404
        return jsonify([image_payload(i, url_prefix) for i in parent.images])

    @bp.route(f"/<int:{kind}_id>/images", methods=["POST"],
              endpoint=f"upload_{kind}_images")
    @login_required
    def _upload(**kw):
        parent = _parent_or_404(kw[f"{kind}_id"])
        if not parent:
            return jsonify({"error": "Not found"}), 404
        return handle_upload(parent, image_model, images_dir_for(parent), url_prefix)

    # kind="image" returns a placeholder SVG rather than a broken image when
    # the library drive is away — a red X in a gallery reads as data loss.
    @bp.route("/images/<int:image_id>", methods=["GET"],
              endpoint=f"serve_{kind}_image")
    @login_required
    @require_library(kind="image")
    def _serve(image_id):
        img = db.session.get(image_model, image_id)
        if not img:
            return jsonify({"error": "No image"}), 404
        return handle_serve(img, images_dir_for(getattr(img, kind)))

    @bp.route("/images/<int:image_id>/primary", methods=["POST"],
              endpoint=f"make_{kind}_image_primary")
    @login_required
    def _primary(image_id):
        img = db.session.get(image_model, image_id)
        if not img:
            return jsonify({"error": "Not found"}), 404
        set_primary(img)
        db.session.commit()
        return jsonify(image_payload(img, url_prefix))

    @bp.route("/images/<int:image_id>", methods=["DELETE"],
              endpoint=f"delete_{kind}_image")
    @login_required
    def _delete(image_id):
        img = db.session.get(image_model, image_id)
        if not img:
            return jsonify({"error": "Not found"}), 404
        return handle_delete(img, images_dir_for(getattr(img, kind)))


def entity_images_dir(library_root, bucket, name, sanitize):
    """
    LIBRARY_ROOT/<bucket>/<sanitized name>/_images.

    `bucket` is '_venues' / '_artists' / '_events' — an underscore-prefixed
    namespace so an entity and an ACT sharing a name ("Fillmore", "Bill Evans")
    cannot write into one folder. Performers deliberately have no bucket: their
    photos have lived beside their recording folders since 2026-07-22 and
    moving them would orphan every existing file.
    """
    from pathlib import Path
    return Path(library_root) / bucket / sanitize(name) / "_images"
