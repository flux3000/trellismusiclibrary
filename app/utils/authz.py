"""
app/utils/authz.py -- shared authorization decorators.

`admin_required` lived in app/api/peers.py until 2026-09-17, when a second
settings surface (library layout, in api/system.py) needed the same gate.
Copying it was the obvious move and the wrong one: two implementations of an
auth check that must agree is the same shape as the three-list peer-surface
bug, and each copy drifts silently in its own direction.

It lives in utils/ rather than in one of the blueprints because system.py is
imported BY five other api modules -- pointing it at a leaf blueprint for a
decorator would invert the dependency and invite a cycle the first time
peers.py wants something from system.py.
"""

from functools import wraps

from flask import jsonify
from flask_login import login_required, current_user


def admin_required(f):
    """login_required + role == admin.

    For control surfaces that change what the whole install does -- peer
    management, sharing address, library layout -- rather than ordinary
    library editing, which goes through the role check each endpoint already
    carries. Deliberately NOT the frontend's canEditLibrary(): that is a UI
    mode, and this is a server-side boundary.
    """
    @wraps(f)
    @login_required
    def wrapper(*args, **kwargs):
        if getattr(current_user, "role", None) != "admin":
            return jsonify({"error": "Admin only"}), 403
        return f(*args, **kwargs)
    return wrapper
