#!/usr/bin/env python3
"""
tools/mount_library.py — keep the library SMB share mounted, without Finder.

Run by the com.fluxaudio.mountlibrary LaunchAgent at login and every 5 minutes.
Safe to run by hand at any time; it is idempotent and exits fast when the mount
is already healthy (the overwhelmingly common case).

WHY THIS EXISTS
---------------
Two separate failure modes have taken the library offline:

  1. macOS drops the SMB mount on update / reboot / sleep-wake, and nothing
     brings it back. `config.py` hardcodes /Volumes/music/Trellis/..., so
     the whole app goes blind.

  2. Browsing Network -> SynologyRB -> music in Finder opens a real SMB
     *session* but creates no mountpoint. That orphan session then blocks the
     mount we actually want: mount_smbfs returns EEXIST ("File exists"), and
     Finder's own "Connect As" button silently does nothing. Diagnosed
     2026-08-21 via `smbutil statshares -a` showing a live SMB_3.1.1 session
     with SESSION_RECONNECT_COUNT 0 and no corresponding `mount` entry.

So the helper's job is NOT merely "mount the share" — it is "tear down
whatever stale state is in the way, THEN mount, THEN prove it is readable."
A mount(2) returning success is not proof; we stat a sentinel path.

WHY osascript RATHER THAN mount_smbfs
-------------------------------------
`mount volume` (AppleScript) pulls credentials from the login keychain and
lets macOS create and own the mountpoint. mount_smbfs needs a pre-made
directory — and a hand-made /Volumes/music left behind after an unmount is
exactly the squatter that makes the next mount land on /Volumes/music-1.
mount_smbfs also has no non-interactive path to the keychain, so it would
hang or fail under launchd where there is no tty.

ESCALATION
----------
Teardown is deliberately graded. umount + rmdir + NetAuthAgent are invisible
to the user and run freely. Killing Finder is user-visible, so it only happens
after MAX_GENTLE_ATTEMPTS consecutive failures (~15 min offline) — long enough
that we are clearly stuck, short enough that Ryan is not reinstalling macOS.

Exit codes:  0 mounted and readable   1 still down after this attempt
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ── Configuration ────────────────────────────────────────────────────────────
# Env-overridable so the helper is testable against a scratch share without
# editing this file. Defaults mirror config.py.

SHARE_URL  = os.environ.get("FLUX_SHARE_URL",  "smb://SynologyRB.local/music")
MOUNTPOINT = os.environ.get("FLUX_MOUNTPOINT", "/Volumes/music")

# The path we actually care about. `mount` succeeding tells us the kernel is
# happy; only reading this tells us the app will work.
# "Trellis" (Ryan, 2026-08-23) — the NAS folder was renamed from "Flux Audio"
# on disk; mirrors config.py's LIBRARY_ROOT default. This one matters more
# than most: this LaunchAgent runs unattended every 5 minutes with no env
# override, so a stale default here means it perceives a perfectly healthy
# mount as broken forever, escalating all the way to killing Finder every
# cycle (see MAX_GENTLE_ATTEMPTS below) — not a cosmetic miss.
SENTINEL = os.environ.get("FLUX_SENTINEL", "/Volumes/music/Trellis/Library")

# Share name as it appears in `smbutil statshares -a` (last path component).
SHARE_NAME = SHARE_URL.rstrip("/").rsplit("/", 1)[-1]

LOG_DIR    = Path.home() / "Library" / "Logs" / "FluxAudio"
LOG_FILE   = LOG_DIR / "mount_library.log"
STATE_FILE = LOG_DIR / "mount_library_state.json"

MAX_GENTLE_ATTEMPTS = 3      # consecutive failures before we bother Finder
LOG_MAX_BYTES       = 512 * 1024
IO_TIMEOUT          = 10     # seconds; a dead NFS/SMB mount can hang forever


# ── Logging ──────────────────────────────────────────────────────────────────

def log(msg):
    """Append a timestamped line to the log, rotating once it gets chunky."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > LOG_MAX_BYTES:
            LOG_FILE.replace(LOG_FILE.with_suffix(".log.1"))
    except OSError:
        pass  # rotation is a nicety, never a reason to fail the run

    stamp = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    with LOG_FILE.open("a") as fh:
        fh.write("%s  %s\n" % (stamp, msg))


# ── Shell helpers ────────────────────────────────────────────────────────────

def run(cmd, timeout=IO_TIMEOUT):
    """Run a command, never raise. Returns (rc, stdout+stderr)."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "timeout after %ss: %s" % (timeout, " ".join(cmd))
    except OSError as e:
        return 127, str(e)


# ── State probes ─────────────────────────────────────────────────────────────

def is_mounted():
    """True if MOUNTPOINT currently carries an smbfs mount."""
    _, out = run(["/sbin/mount"])
    return (" on %s (smbfs" % MOUNTPOINT) in out


def has_orphan_session():
    """
    True if the kernel holds an SMB session for our share.

    Called only when the mount is absent — in that state a live session is by
    definition an orphan, and it is what makes the next mount fail with EEXIST.
    """
    rc, out = run(["/usr/bin/smbutil", "statshares", "-a"])
    if rc != 0:
        return False
    for line in out.splitlines():
        # Share names sit flush-left; attribute rows are indented, and the
        # header/rule lines start with '=' or 'SHARE'.
        if not line or line[0].isspace() or line.startswith(("=", "SHARE")):
            continue
        if line.split()[0] == SHARE_NAME:
            return True
    return False


def sentinel_readable():
    """
    True if SENTINEL is a directory we can actually read.

    Run in a subprocess with a hard timeout: a half-dead SMB mount will hang
    a bare os.listdir() indefinitely, which would wedge the LaunchAgent and
    leave launchd unable to start the next run.
    """
    rc, _ = run([sys.executable, "-c",
                 "import os,sys; sys.exit(0 if os.path.isdir(sys.argv[1]) "
                 "and os.listdir(sys.argv[1]) is not None else 1)",
                 SENTINEL])
    return rc == 0


def healthy():
    """The only definition of 'working' that matters to the app."""
    return is_mounted() and sentinel_readable()


# ── Failure-streak bookkeeping ───────────────────────────────────────────────

def read_streak():
    try:
        return int(json.loads(STATE_FILE.read_text()).get("consecutive_failures", 0))
    except (OSError, ValueError, AttributeError):
        return 0


def write_streak(n):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        STATE_FILE.write_text(json.dumps({
            "consecutive_failures": n,
            "updated": datetime.now(timezone.utc).isoformat(),
        }))
    except OSError:
        pass


# ── Teardown ─────────────────────────────────────────────────────────────────

def teardown(aggressive=False):
    """
    Clear whatever is blocking a clean mount.

    Ordered cheapest-and-most-invisible first. Every step is best-effort; the
    point is to get to a state where `mount volume` can succeed, not to prove
    any individual step was necessary.
    """
    # 1. Release any mount the kernel still thinks it has.
    if os.path.ismount(MOUNTPOINT):
        rc, out = run(["/sbin/umount", "-f", MOUNTPOINT])
        log("  umount -f %s -> rc=%s %s" % (MOUNTPOINT, rc, out.strip()))

    # 2. Remove a leftover empty directory squatting on the mountpoint name.
    #    If this is left in place macOS mounts at /Volumes/music-1 instead and
    #    every hardcoded path in config.py silently misses.
    if os.path.isdir(MOUNTPOINT) and not os.path.ismount(MOUNTPOINT):
        try:
            os.rmdir(MOUNTPOINT)          # only succeeds if genuinely empty
            log("  removed squatter directory %s" % MOUNTPOINT)
        except OSError as e:
            log("  could not remove %s: %s" % (MOUNTPOINT, e))

    # 3. Reset the SMB auth agent. This is the process that draws the
    #    "Connect As" sheet; when it wedges, that button is a silent no-op.
    #    It relaunches on demand, so killing it costs nothing.
    run(["/usr/bin/killall", "-9", "NetAuthAgent"])
    log("  reset NetAuthAgent")

    # 4. Last resort: Finder holds browse sessions and will not give them up.
    #    User-visible, so gated behind a failure streak.
    if aggressive:
        run(["/usr/bin/killall", "Finder"])
        log("  killed Finder (aggressive teardown — it was holding the session)")

    time.sleep(2)   # let the kernel actually drop the session


# ── Mount ────────────────────────────────────────────────────────────────────

def mount():
    """Mount via AppleScript so macOS owns the mountpoint and uses the keychain."""
    rc, out = run(["/usr/bin/osascript", "-e",
                   'mount volume "%s"' % SHARE_URL], timeout=45)
    if rc != 0:
        log("  mount volume failed rc=%s: %s" % (rc, out.strip()))
    return rc == 0


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    # Fast path. This is what happens ~99% of runs; keep it to two cheap checks
    # and no logging, or the log becomes 288 identical lines a day.
    if healthy():
        write_streak(0)
        return 0

    streak = read_streak() + 1
    log("library offline (attempt %d) — mounted=%s readable=%s"
        % (streak, is_mounted(), sentinel_readable()))

    if not is_mounted() and has_orphan_session():
        log("  orphan SMB session for '%s' with no mountpoint — this is the "
            "Finder-browse failure mode" % SHARE_NAME)

    teardown(aggressive=(streak >= MAX_GENTLE_ATTEMPTS))

    if mount() and healthy():
        log("  mounted OK at %s, sentinel readable" % MOUNTPOINT)
        write_streak(0)
        return 0

    # Distinguish "no mount" from "mounted but unreadable" — the second means
    # the share mounted somewhere unexpected, or permissions changed on the NAS.
    if is_mounted() and not sentinel_readable():
        log("  MOUNTED BUT SENTINEL UNREADABLE: %s — wrong share, or the "
            "Trellis folder moved on the NAS" % SENTINEL)
    else:
        log("  still not mounted after teardown + retry")

    write_streak(streak)
    return 1


if __name__ == "__main__":
    sys.exit(main())
