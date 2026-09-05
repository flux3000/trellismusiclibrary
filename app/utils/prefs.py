"""
app/utils/prefs.py — user preferences + BYOK secret storage.

Non-secret prefs (ai_model, ingest_file_behavior) live in the user_preference
table. The Anthropic API key is a real secret, so it goes in the OS keychain via
the `keyring` package — the DB never stores it, only whether one is present.
"""

from app.extensions import db
from app.models.user_preference import UserPreference

try:
    import keyring
    _HAS_KEYRING = True
except ImportError:
    _HAS_KEYRING = False

# Renamed from "flux_audio" 2026-09-04, with no migration by Ryan's call.
# ⚠ Every credential stored under the old service name becomes invisible the
# moment this line changes: peer tokens and the Anthropic API key both. They
# are not deleted, they are orphaned, and nothing will ever clean them up.
# Re-join any peer and re-enter the API key once.
_SERVICE = "trellis"


def get_pref(user_id, key, default=None):
    row = db.session.query(UserPreference).filter_by(user_id=user_id, key=key).first()
    return row.value if row else default


def set_pref(user_id, key, value):
    row = db.session.query(UserPreference).filter_by(user_id=user_id, key=key).first()
    if row:
        row.value = value
    else:
        db.session.add(UserPreference(user_id=user_id, key=key, value=value))
    db.session.commit()


# ── BYOK secret (OS keychain) ─────────────────────────────────────────────────

def _account(user_id):
    return "anthropic_api_key:%s" % user_id


def get_api_key(user_id):
    if not _HAS_KEYRING:
        return None
    try:
        return keyring.get_password(_SERVICE, _account(user_id))
    except Exception:
        return None


def set_api_key(user_id, key):
    if not _HAS_KEYRING:
        raise RuntimeError("OS keychain unavailable")
    keyring.set_password(_SERVICE, _account(user_id), key)


def delete_api_key(user_id):
    if not _HAS_KEYRING:
        return
    try:
        keyring.delete_password(_SERVICE, _account(user_id))
    except Exception:
        pass


# ── Remote-node tokens (OS keychain) ──────────────────────────────────────────
# Outbound peer sharing (milestone 2, 2026-08-08). The token a remote Flux node
# issued me at enrollment. Same reasoning as the BYOK key above: the DB records
# that a remote exists, never the credential to reach it.
#
# Unlike the inbound peer_token, this CANNOT be hashed — it has to be replayed
# verbatim on every proxied request. Keychain or plaintext were the only two
# options, and trellis.db gets copied to dev rigs and backed up.

def _remote_account(node_id):
    return "remote_token:%s" % node_id


def get_remote_token(node_id):
    """The token for a remote node, or None if the keychain is unavailable or
    holds nothing. Callers must treat None as 'cannot connect', NOT as 'the
    remote returned an empty library' — those look identical in a UI and only
    one of them is the user's problem."""
    if not _HAS_KEYRING:
        return None
    try:
        return keyring.get_password(_SERVICE, _remote_account(node_id))
    except Exception:
        return None


def set_remote_token(node_id, token):
    if not _HAS_KEYRING:
        raise RuntimeError("OS keychain unavailable")
    keyring.set_password(_SERVICE, _remote_account(node_id), token)


def delete_remote_token(node_id):
    """Best effort — leaving a remote must succeed even if the keychain entry
    is already gone."""
    if not _HAS_KEYRING:
        return
    try:
        keyring.delete_password(_SERVICE, _remote_account(node_id))
    except Exception:
        pass


def has_api_key(user_id):
    return bool(get_api_key(user_id))
