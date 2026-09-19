#!/usr/bin/env python3
"""
tools/release.py — cut a Trellis release, start to finish.

    python3 tools/release.py 0.2.2

Four phases, in the only order that works: bump version.py, build and sign the
artifacts, commit and tag and push, publish the GitHub release. Each phase
notices when it has already been done and skips, so a failure ten minutes into
notarization is fixed by re-running the same command rather than by unpicking
where you got to.

Everything that can be checked cheaply is checked BEFORE the first phase runs.
The release notes file, the gh login and the signing identity are all things
that would otherwise fail at the very end, after the slow part, which is the
worst moment to discover them.

Flags:
    --dry-run    say what each phase would do, change nothing
    --rebuild    build even if this version's artifacts are already in dist/
    --yes        skip the confirmation prompts (for a re-run you have already
                 eyeballed once; not the way to do a first run)

Why this lives in tools/ alongside build_macos.sh and sign_macos.sh: it is
build tooling, not application behaviour. Nothing here decides how a recording
is understood, so nothing here is being kept out of the app.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
NOTES_DIR = REPO / "docs" / "release-notes"

# The keychain profile holding the notarization credentials. A profile NAME is
# not a credential: the app-specific password lives in the keychain under it,
# and this script never sees it. Override with TRELLIS_NOTARY_PROFILE.
DEFAULT_NOTARY_PROFILE = "trellis-notary"


# ── plumbing ─────────────────────────────────────────────────────────────────

class Stop(Exception):
    """A refusal with a reason the user can act on."""


def run(cmd, **kw):
    """Run a command, inheriting stdout/stderr so build output streams live."""
    return subprocess.run(cmd, cwd=REPO, check=True, **kw)


def out(cmd):
    """Run a command and return its stdout, or None if it failed."""
    p = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else None


def ok(cmd):
    """True when the command exits zero. For existence probes."""
    return subprocess.run(cmd, cwd=REPO, capture_output=True).returncode == 0


def say(msg=""):
    print(msg, flush=True)


def rule(title):
    say()
    say(f"── {title} " + "─" * max(0, 62 - len(title)))


def confirm(question, auto_yes):
    if auto_yes:
        say(f"  {question} yes (--yes)")
        return
    answer = input(f"  {question} [y/N] ").strip().lower()
    if answer not in ("y", "yes"):
        raise Stop("Stopped at your request. Nothing after this point ran.")


# ── version ──────────────────────────────────────────────────────────────────

VERSION_RE = re.compile(r'^__version__\s*=\s*"([^"]+)"', re.M)


def current_version():
    text = (REPO / "version.py").read_text(encoding="utf-8")
    m = VERSION_RE.search(text)
    if not m:
        raise Stop("Could not find __version__ in version.py.")
    return m.group(1)


def write_version(new):
    path = REPO / "version.py"
    text = path.read_text(encoding="utf-8")
    patched, n = VERSION_RE.subn(f'__version__ = "{new}"', text, count=1)
    if n != 1:
        raise Stop("Refusing to write version.py: expected exactly one __version__ line.")
    path.write_text(patched, encoding="utf-8")


def as_tuple(v):
    """Sortable form, so 0.2.10 beats 0.2.9. Non-numeric parts sort last."""
    return tuple(int(p) if p.isdigit() else -1 for p in v.split("."))


def artifacts(version):
    base = f"TrellisMusicLibrary-{version}-macOS"
    return REPO / "dist" / f"{base}.dmg", REPO / "dist" / f"{base}.zip"


def notes_file(version):
    return NOTES_DIR / f"v{version}.md"


# ── preflight ────────────────────────────────────────────────────────────────

def signing_identity():
    """
    The Developer ID to sign with.

    Read from the environment when set, otherwise found in the keychain. Asking
    the keychain rather than hardcoding it keeps a Team ID out of a public repo
    and means a renewed certificate needs no edit here.
    """
    env = os.environ.get("TRELLIS_SIGN_IDENTITY")
    if env:
        return env

    listing = out(["security", "find-identity", "-v", "-p", "codesigning"]) or ""
    found = re.findall(r'"(Developer ID Application: [^"]+)"', listing)
    unique = sorted(set(found))
    if not unique:
        raise Stop(
            "No Developer ID Application identity in the keychain, and\n"
            "  TRELLIS_SIGN_IDENTITY is not set. An unsigned build cannot be\n"
            "  released: Gatekeeper refuses it on every Mac including yours."
        )
    if len(unique) > 1:
        joined = "\n    ".join(unique)
        raise Stop(
            "More than one Developer ID Application identity is in the keychain,\n"
            "  so I will not guess. Set TRELLIS_SIGN_IDENTITY to one of:\n    " + joined
        )
    return unique[0]


def claude_cli():
    """
    Path to the claude CLI, or None.

    Not just shutil.which: the native installer puts the binary in
    ~/.local/bin, which is on PATH in a login shell and frequently not in
    whatever shell a build is running from. A 0.2.4 release stopped on exactly
    that, one release after the same CLI had worked, because the PATH export
    lived in one terminal session. Set CLAUDE_CLI to override.
    """
    override = os.environ.get("CLAUDE_CLI")
    if override:
        return override if Path(override).is_file() else None

    found = shutil.which("claude")
    if found:
        return found

    for candidate in (
        Path.home() / ".local" / "bin" / "claude",     # native installer
        Path("/opt/homebrew/bin/claude"),              # Homebrew, Apple silicon
        Path("/usr/local/bin/claude"),                 # Homebrew, Intel
    ):
        if candidate.is_file():
            return str(candidate)
    return None


def preflight(version, dry_run):
    """Every cheap check, before the expensive phase. Order is deliberate."""
    rule("Preflight")

    if sys.platform != "darwin":
        raise Stop("A Mac app is built on a Mac. PyInstaller does not cross-compile.")

    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise Stop(f"'{version}' is not a version number. Expected something like 0.2.2.")

    if not ok(["git", "rev-parse", "--git-dir"]):
        raise Stop(f"{REPO} is not a git checkout.")

    now = current_version()
    if as_tuple(version) < as_tuple(now):
        raise Stop(f"version.py says {now}, which is newer than {version}. Releases go forward.")
    say(f"  ✓ version.py is at {now}")

    # Notes are settled here, not at publish time, because publish is the last
    # thing that happens and a missing file at that point means the whole slow
    # half already ran for nothing.
    nf = notes_file(version)
    if nf.exists() and nf.read_text(encoding="utf-8").strip():
        say(f"  ✓ release notes: {nf.relative_to(REPO)}")
    elif not claude_cli():
        raise Stop(
            f"No release notes at {nf.relative_to(REPO)}, and the claude CLI was\n"
            f"  not found on PATH, in ~/.local/bin or in Homebrew's bin. Install\n"
            f"  it, set CLAUDE_CLI to its path, or write the notes file yourself."
        )
    else:
        say(f"  · release notes will be drafted by {claude_cli()}")

    if not shutil.which("gh"):
        raise Stop("The gh CLI is not on PATH, so the release cannot be published.")
    if not ok(["gh", "auth", "status"]):
        raise Stop("gh is installed but not logged in. Run: gh auth login")
    say("  ✓ gh is logged in")

    # A tag that already points at a commit OFF this branch means two different
    # binaries would claim one version. That is the failure this script exists
    # to make impossible, so it is fatal.
    #
    # But the tag being BEHIND head is ordinary and must not stop anything: you
    # tag the commit you built, then keep working. The question is ancestry,
    # not equality. Getting this wrong refused a legitimate publish on 0.2.2,
    # where the tag sat one commit back.
    tagged = out(["git", "rev-list", "-n1", f"v{version}"])
    if tagged:
        if not ok(["git", "merge-base", "--is-ancestor", tagged, "HEAD"]):
            raise Stop(
                f"Tag v{version} points at {tagged[:7]}, which is not in this\n"
                f"  branch's history. Two different commits would ship as one\n"
                f"  version. Bump, or delete that tag if it was a mistake."
            )
        head = out(["git", "rev-parse", "HEAD"])
        where = "on HEAD" if tagged == head else f"at {tagged[:7]}, behind HEAD"
        say(f"  ✓ tag v{version} already exists, {where}")

    identity = signing_identity()
    say(f"  ✓ signing as: {identity}")

    profile = os.environ.get("TRELLIS_NOTARY_PROFILE", DEFAULT_NOTARY_PROFILE)
    say(f"  ✓ notary profile: {profile}")

    if dry_run:
        say()
        say("  DRY RUN. Nothing below this line will actually run.")
    return identity, profile


# ── release notes ────────────────────────────────────────────────────────────

# House rules, handed to the model because they are the difference between
# usable copy and copy Ryan has to rewrite. They are the same rules that govern
# every other user-facing string in Trellis.
NOTES_RULES = """Audience: people who collect live concert recordings and are
comfortable with the technical side. They run their own libraries, mind where
their files live, and some of them share over a tunnel or a VPN. Write for
someone competent, not someone who needs protecting from detail.

Rules, all of them firm:
- No em dashes anywhere. Use a comma, a colon, a full stop or parentheses.
- Never the word "absolutely".
- Plain words. Say "live concert recordings", never "ROIO" or insider jargon.
- No offers of help, support or future work. No "let us know", no "feel free".
- Do not compare this release to anything, do not apologise for it, and do not
  hedge. State what changed.
- No headings, no bullet list, no title. Short bolded lead-ins followed by
  prose, one paragraph per change.
- Describe ONLY the committed changes listed below. The working tree may hold
  uncommitted work; it is not in this release and must not appear.
- Output the notes body and nothing else. Never address the reader, never
  explain what you did or did not include, never comment on the task.

Lead with what a user would notice. When a release changes nothing visible,
say what it does change and why that reader would care: packaging, signing,
what gets written to disk, dependencies, performance, anything affecting how
the app installs, runs or handles their library. Every release has something
worth stating to this audience. Pure churn is the exception: test refactors,
formatting passes and editor settings are not worth a paragraph, and a release
that is genuinely only those says so in one sentence and stops."""


def previous_tag(version):
    """The highest existing release tag below this one, or None."""
    listed = out(["git", "tag", "--list", "v*", "--sort=-version:refname"]) or ""
    for tag in listed.splitlines():
        if tag.strip() and tag.strip() != f"v{version}":
            return tag.strip()
    return None


def generate_notes(version, dry_run):
    """
    Draft docs/release-notes/vX.Y.Z.md with the claude CLI.

    Never overwrites an existing file: once you have edited the notes, they are
    yours, and a re-run must not quietly replace them.

    Commit subjects alone are far too thin here. "Persistence fix" and "AI
    assist transformation" are real commit messages from 0.2.2, and no amount
    of prompting turns those three words into a paragraph a collector can use.
    The diffstat is what carries the actual shape of the release, so it goes in
    too.
    """
    nf = notes_file(version)
    if nf.exists() and nf.read_text(encoding="utf-8").strip():
        return None

    prev = previous_tag(version)
    span = f"{prev}..HEAD" if prev else "HEAD"
    rule("Release notes")
    say(f"  Drafting from {span}. This calls the claude CLI and takes a moment.")

    commits = out(["git", "log", span, "--pretty=%s"]) or ""
    diffstat = out(["git", "diff", "--stat", span]) or ""

    prompt = (
        f"Write the release notes body for Trellis Music Library {version}, a "
        f"macOS app for people who collect live concert recordings.\n\n"
        f"{NOTES_RULES}\n\n"
        f"Commit subjects since {prev or 'the beginning'}:\n{commits}\n\n"
        f"Files changed:\n{diffstat}\n\n"
        f"Output only the notes body. No preamble, no sign-off."
    )

    p = subprocess.run([claude_cli(), "-p", prompt], cwd=REPO,
                       capture_output=True, text=True)
    body = p.stdout.strip()
    if p.returncode != 0 or not body:
        raise Stop(
            "The claude CLI did not return any notes.\n"
            f"  {(p.stderr or '').strip()[:400]}\n"
            f"  Write {nf.relative_to(REPO)} by hand and run this again."
        )

    if dry_run:
        say(f"  Would write {nf.relative_to(REPO)}. Drafted below, not saved.")
        return body

    nf.parent.mkdir(parents=True, exist_ok=True)
    nf.write_text(body + "\n", encoding="utf-8")
    say(f"  ✓ wrote {nf.relative_to(REPO)}")
    return body


def show_notes(version, draft=None):
    """
    Print the notes, in full, immediately before they are published.

    Here rather than at draft time because this is the last moment before the
    text is public, and because notes Ryan wrote or edited himself deserve the
    same look as generated ones. Long lines wrap in the terminal; that is the
    terminal's job, not this function's.
    """
    nf = notes_file(version)
    if nf.exists():
        body = nf.read_text(encoding="utf-8").rstrip()
    elif draft:
        # A dry run drafted this but deliberately did not save it.
        body = draft.rstrip()
    else:
        say()
        say("  (no notes drafted)")
        say()
        return

    say()
    say("  ┌─ release body ───────────────────────────────────────────────")
    for line in body.splitlines():
        say(f"  │ {line}")
    say("  └──────────────────────────────────────────────────────────────")
    say()


# ── phases ───────────────────────────────────────────────────────────────────

def phase_bump(version, dry_run):
    rule("1. Version")
    if current_version() == version:
        say(f"  Already {version}. Nothing to bump.")
        return
    say(f"  {current_version()} → {version} in version.py")
    if dry_run:
        return
    write_version(version)
    say(f"  ✓ version.py now says {current_version()}")


def phase_build(version, identity, profile, dry_run, rebuild):
    rule("2. Build, sign, notarize")
    dmg, zipf = artifacts(version)
    if dmg.exists() and zipf.exists() and not rebuild:
        say(f"  {dmg.name} and {zipf.name} are already in dist/.")
        say("  Skipping the build. Pass --rebuild to force one.")
        return
    say("  Running tools/build_macos.sh. This is the slow part: PyInstaller,")
    say("  then notarization, which waits on Apple.")
    if dry_run:
        return

    env = dict(os.environ)
    env["TRELLIS_SIGN_IDENTITY"] = identity
    env["TRELLIS_NOTARY_PROFILE"] = profile
    run(["./tools/build_macos.sh"], env=env)

    # build_macos.sh calls sign_macos.sh, which is what actually produces these
    # two. If they are missing the signing half did not run, whatever the exit
    # code said.
    for f in (dmg, zipf):
        if not f.exists():
            raise Stop(f"The build finished but {f.name} is missing.")
    say(f"  ✓ {dmg.name}")
    say(f"  ✓ {zipf.name}")


def phase_tag(version, dry_run, auto_yes):
    rule("3. Commit, tag, push")
    tag = f"v{version}"
    branch = out(["git", "rev-parse", "--abbrev-ref", "HEAD"]) or "main"

    dirty = out(["git", "status", "--porcelain"])
    if dirty:
        say("  Working tree, about to be committed in full:")
        say()
        run(["git", "status", "--short"])
        say()
        say("  version.py:")
        run(["git", "--no-pager", "diff", "--", "version.py"])
        say()
        if dry_run:
            say(f'  Would run: git add -A && git commit -m "Version {version}"')
        else:
            confirm(f'Commit all of that as "Version {version}"?', auto_yes)
            run(["git", "add", "-A"])
            run(["git", "commit", "-m", f"Version {version}"])
            say(f'  ✓ committed "Version {version}"')
    else:
        say("  Working tree is clean. Nothing to commit.")

    if ok(["git", "rev-parse", "-q", "--verify", f"refs/tags/{tag}"]):
        say(f"  Tag {tag} already exists.")
    elif dry_run:
        say(f"  Would run: git tag -a {tag} -m {tag}")
    else:
        run(["git", "tag", "-a", tag, "-m", tag])
        say(f"  ✓ tagged {tag}")

    if dry_run:
        say(f"  Would run: git push origin {branch} && git push origin {tag}")
        return

    say(f"  Pushing {branch} and {tag} to origin.")
    run(["git", "push", "origin", branch])
    run(["git", "push", "origin", tag])
    say("  ✓ pushed")


def phase_publish(version, dry_run, auto_yes, draft=None):
    rule("4. GitHub release")
    tag = f"v{version}"
    dmg, zipf = artifacts(version)
    nf = notes_file(version)

    if ok(["gh", "release", "view", tag]):
        say(f"  Release {tag} already exists on GitHub. Nothing to publish.")
        return

    say(f"  {tag}, with {dmg.name} and {zipf.name},")
    say(f"  and the body from {nf.relative_to(REPO)}:")
    show_notes(version, draft)
    if dry_run:
        say("  Would run: gh release create ...")
        return

    confirm(f"Publish {tag} with the notes above?", auto_yes)
    # --latest is explicit rather than inferred. v0.2.2 published without the
    # Latest label and it stayed on v0.2.1, which meant the download link on
    # the website pointed at the previous build. Not worth leaving to GitHub's
    # ordering rules.
    run([
        "gh", "release", "create", tag,
        str(dmg.relative_to(REPO)), str(zipf.relative_to(REPO)),
        "--title", tag, "--notes-file", str(nf.relative_to(REPO)),
        "--latest",
    ])
    say(f"  ✓ published {tag}")


# ── entry point ──────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Cut a Trellis release.")
    ap.add_argument("version", help="the version to release, e.g. 0.2.2")
    ap.add_argument("--dry-run", action="store_true", help="change nothing")
    ap.add_argument("--rebuild", action="store_true", help="build even if artifacts exist")
    ap.add_argument("--yes", action="store_true", help="skip confirmations")
    args = ap.parse_args()

    try:
        identity, profile = preflight(args.version, args.dry_run)
        draft = generate_notes(args.version, args.dry_run)
        phase_bump(args.version, args.dry_run)
        phase_build(args.version, identity, profile, args.dry_run, args.rebuild)
        phase_tag(args.version, args.dry_run, args.yes)
        phase_publish(args.version, args.dry_run, args.yes, draft)
    except Stop as e:
        say()
        say(f"  ✗ {e}")
        return 1
    except subprocess.CalledProcessError as e:
        say()
        say(f"  ✗ {' '.join(str(c) for c in e.cmd)} failed with exit code {e.returncode}.")
        say("    Fix it and run the same command again. Finished phases will skip.")
        return e.returncode
    except KeyboardInterrupt:
        say()
        say("  ✗ Interrupted.")
        return 130

    rule("Done")
    say(f"  https://github.com/flux3000/trellismusiclibrary/releases/tag/v{args.version}")
    say()
    say("  To correct the notes after the fact, edit the file and then:")
    say(f"    gh release edit v{args.version} "
        f"--notes-file docs/release-notes/v{args.version}.md")
    say()
    return 0


if __name__ == "__main__":
    sys.exit(main())
