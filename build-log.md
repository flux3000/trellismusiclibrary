
## Fix — etree preset dot placement (before chunk 6)

Files touched:
- `app/utils/file_naming.py` — `PRESETS["etree"]` changed from
  `...{shnid}][d{disc}]t{track_in_disc}` to
  `...{shnid}].[d{disc}]t{track_in_disc}`: the literal dot moved outside the
  disc bracket group so it renders whether or not a disc is present.
  `gd1978-09-19.aud.schoeps.t01.flac` with no disc,
  `gd1978-09-19.aud.schoeps.d1t01.flac` with one, matching the mockup
  (03-mockup.html panel 2 preview table) exactly. `etree_sets` was left
  untouched — the ask named `etree` only, and `etree_sets` has the identical
  shape (`[s{set}]t{track_in_set}`); flagging for Ryan in case he wants the
  same dot move there for consistency, but not touching an unrequested
  preset.
- `tests/test_file_naming.py` — three assertions updated for the new dot
  placement: `test_etree_preset_flat` (`...schoeps.t01.flac`),
  `test_etree_preset_multi_disc` (`...118671.d2t01.flac` — the dot is now
  always emitted before the disc/track segment, so the shnid-present case
  also gains a dot it didn't have before), `test_etree_preset_with_
  nothing_present_drops_every_group` (`gd1977-05-08.t01.flac`, no longer
  bare — this is a deliberate consequence of the fix: the separator dot is
  now unconditional, not tied to any bracket group's presence). Docstring
  on the last test updated to say so.

Grepped for the old literal outputs (`schoepst01`, `118671d2t01`,
`gd1977-05-08t01`) across `app/` and `tests/` — none remained elsewhere.

Tests run: `tests/test_file_naming.py` (36 passed) →
`tests/test_no_undefined_names.py` (83 passed) → `node --check
app/static/js/app.js` (clean) → full suite: **885 passed**, 0 failed
(unchanged from the chunk-5 baseline, as expected for a preset-string fix).

## Chunk 6 — Ingest: conditional flatten, rename, folder name, tag write

Files touched:
- `app/utils/ingest.py` — new `resolve_ingest_file_path(orig_rel_path,
  audio_rename_map, flatten)`, the single place that decides where one audio
  file lands relative to the destination root, shared between
  `move_to_library()` (writes it there) and `_do_confirm` step 8 (stores the
  same value as `Track.file_path`) so the two can't drift apart. `
  move_to_library()` gained `flatten=True` (default preserves every existing
  caller's behavior); its audio branch now calls the shared resolver instead
  of always stripping to a flat basename. `compute_audio_rename_map()`
  rewritten to go through the naming engine (`file_naming.rename_plan()`),
  taking `scheme`/`template`/`performance`/`source`/`source_tag`/
  `etree_shnid` instead of hardcoding "NN - Title"; a new `_NamingProxy`
  duck-types the recording/track shape `rename_plan()` needs from raw
  ingest-time dicts (no Recording/Track rows exist yet at this point).
  `scheme="original"` is special-cased to a literal identity map
  (`{orig: basename(orig)}`) rather than routed through the engine — see
  "decided alone" below, this is a real bug the keep-mode e2e test caught.
  Retired the now-dead `_sanitize_filename()` (only caller was the old
  hardcoded renamer) — removed in place per the "retired code inside a file
  is removed by edit" rule, not moved to `_to_delete/` (no whole file).
- `app/api/ingest.py` — step 5 (folder name) now branches on
  `file_handling["rename_folders"]`: on uses `build_folder_name()` as
  before, off uses the source folder's own basename (still deduped by
  `unique_folder_name()` inside `move_to_library`). Step 6 branches on
  `rename_files`: on builds the audio rename map via the engine with the
  active `naming_scheme`/`naming_template` and computes `flatten` from
  `flattens()`; off uses the identity map and `flatten=False` unconditionally
  (spec 1.1: "Off: names and nesting preserved" — not read as mode-
  conditional, see "decided alone"). Step 8's `Track.file_path` now goes
  through `resolve_ingest_file_path()` instead of always taking the bare
  basename. New step 12 after the main commit: `write_tags_on_ingest` gates
  a `write_flac_tags(rec, library_root)` call; on any files written it logs
  a `tags_written` event with the exact same note format
  (`"{n} file(s) written[; N error(s): ...]"`) the View Recording write-tags
  endpoint uses; `tag_errors` always rides the job result. New `"tags"`
  phase label ("Writing tags to files") added to `PHASES` and to
  `_LQ_PHASE_PCT` in `app.js` (99%, same slot as "saving") — a progress-bar
  label in the established terse present-participle style of its siblings,
  not a new user-facing feature string.
- `tests/test_db_logic.py` — three existing end-to-end/progress tests
  (`test_do_confirm_copies_files_and_reports_progress`,
  `test_verify_checksums_discovers_and_verifies_backfill`,
  `test_do_confirm_flattens_and_renames_multi_disc_with_checksums`) now call
  `node_settings.apply_mode("organize")` before ingesting — they assert the
  old always-flatten-and-rename behavior, which is now `organize`'s
  behavior, not the (now default) `keep` state; only the third was named in
  spec section 7's table, but the other two exercise the identical
  now-mode-dependent path and were failing for the same reason, so all
  three got the same one-line fix. New
  `test_do_confirm_keeps_names_and_nesting_in_keep_mode`: CD1/CD2 source
  ingested with no `apply_mode` call (default `keep`); asserts
  `file_path`/`original_file_path` keep their `CD1/`,`CD2/` prefixes
  unrenamed, disc fields still populate (disc detection is independent of
  file handling), checksums still match per-disc despite the shared
  "01.flac" basename, and no `tags_written` event exists.
- `tests/test_ingest_utils.py` — two new `flatten=False` cases:
  nesting preserved for a CD1/CD2 source with an empty rename map, and the
  same but with a non-empty `audio_rename_map` proving only the basename is
  substituted, the directory prefix untouched.
- `tests/test_checksums.py` — new
  `test_nested_basename_collision_requires_caller_side_scoping`: a pure
  `match_entries_to_tracks()` test showing an unscoped call with two tracks
  sharing a basename ("01.flac" from two different original discs) hands
  the WRONG track the checksum (the later track in the list wins the
  internal basename index), and that pre-scoping the candidate list (what
  `_do_confirm` step 9 already does) is what actually resolves it — this
  function has no directory awareness of its own.
- `tests/test_ingest_tag_write.py` — new file, 3 tests: tag write hits the
  renamed path and logs a `tags_written` event under `organize`; off by
  default under `keep` (file is left with zero Vorbis comments); a
  permissions error on one file yields exactly one `tag_errors` entry
  without failing the ingest and without logging an event (n_written == 0),
  exercised through the real `_do_confirm` codepath via a chmod-then-call
  wrapper swapped onto `app.api.ingest.write_flac_tags` for that one test.

Bug caught by its own test before commit: routing `scheme="original"`
through the shared naming engine applies `rename_plan()`'s batch-wide
`_dedupe_names()`, which doesn't know about `flatten` — a CD1/01.flac +
CD2/01.flac source under `keep` mode's identity map got its second track
suffixed to "01 (2).flac" even though nesting is preserved and no real
on-disk collision exists (two different directories). Since `flattens
("original")` is always False, a real collision under this preset is
structurally impossible (two files can't share a name in one real
directory), so `compute_audio_rename_map()` now short-circuits `scheme ==
"original"` to a literal identity map with no engine/dedup involved.

Decided alone:
- Section 3.1's D1 table literally titles its columns "keep"/"organize",
  but section 1.1 states flatten/nesting is governed by the `rename_files`
  switch itself ("Off: names and nesting preserved"), and switches stay
  individually editable after a mode is chosen. Implemented flatten/rename
  as driven by the switches (`rename_files` + `naming_scheme`/`flattens()`),
  not by which named mode is active — this matches both modes' DEFAULT
  switch settings (so every existing/spec'd test passes either way) and
  doesn't invent a second, undocumented axis of "mode overrides switches."
  If Ryan wants mode to hard-override an edited `rename_files` switch for
  placement, that's a spec change, not a bug.
- `PRESETS["etree_sets"]` still has the pre-fix dot placement (see the
  standalone etree fix entry above) — untouched here since compute_audio_
  rename_map routes through it unchanged and nothing in this chunk's tests
  exercises etree_sets specifically; flagged there, not duplicated here.
- The identity-map special case for `scheme == "original"` was not explicit
  in spec section 4/D2, but is a direct, unavoidable consequence of
  `flattens("original")` always being False (see the bug note above) — this
  is a bug fix inside the engine's calling convention, not a new design
  decision about what "original" means.

Tests run: `tests/test_db_logic.py` + `tests/test_ingest_utils.py` +
`tests/test_ingest_tag_write.py` + `tests/test_checksums.py` (112 passed) →
`tests/test_no_undefined_names.py` (83 passed) → `node --check
app/static/js/app.js` (clean) → full suite: **892 passed** (885 + 7 new), 0
failed.

Nothing moved to `_to_delete/` this chunk.

## Chunk 7 — Post-ingest gates and checksum scoping

Files touched:
- `app/api/recordings.py` — Recording PUT's folder-rename call is now gated
  on `rename_folders`; off skips `rename_recording_folder()` entirely
  (`rename_error = None`), matching D8. `verify_checksums()` now scopes
  matching to each `RecordingFingerprint.rel_path`'s own directory before
  calling `match_entries_to_tracks()` (D7): candidates are filtered by
  `original_file_path`'s directory first, then `file_path`'s, then fall back
  to every track if scoping finds nothing usable — mirrors ingest step 9's
  own scoping exactly. Matching itself tries ORIGINAL filenames first, then
  falls back to CURRENT `file_path` when the original-name pass matches
  nothing — needed because a backfilled checksum file can be generated
  either against the pre-rename layout (ships with the source) or freshly
  against what's in the library today (a re-run archivist tool), and only
  one of those two passes will ever hit. Both new `RecordingFingerprint`
  rows created here (discovery) and in ingest.py (step 9, see below) now
  populate `rel_path` — it was added to the model in chunk 1 but nothing
  wrote it until this chunk needed it for scoping.
- `app/api/performances.py` — same `rename_folders` gate around the
  per-recording rename loop for Performance PUT; off means `rename_errors`
  stays `[]` without calling `rename_recording_folder()` for any of the
  performance's recordings.
- `app/api/tracks.py` — the retitle rename block is now gated on
  `rename_files`; off leaves `t.file_path` and the file on disk untouched,
  no `rename_warning`, matching D9. `_build_track_filename()` takes the
  active `scheme`/`template` instead of hardcoding `"number_title"` — the
  ONE existing call site passes `file_handling["naming_scheme"]`/
  `["naming_template"]`.
- `app/api/ingest.py` — step 9's `RecordingFingerprint(...)` now sets
  `rel_path=rel_path` (the value was already computed locally there for the
  file read, just never stored — needed for chunk 7's scoping to have
  anything to scope by for an ingest-time fingerprint).
- `app/utils/checksums.py` — the ingest-only `_ChecksumMatchProxy` moved
  here as public `ChecksumMatchProxy`, since `verify_checksums()` now needs
  the identical proxy; `app/api/ingest.py` imports it instead of defining
  its own copy (one class, two callers, per the shared-helper pattern this
  codebase already uses elsewhere).
- `tests/test_folder_collision_e2e.py` — both fixtures (`ryman`,
  `two_tracks`) now call `node_settings.apply_mode("organize")`, since every
  existing test in this file asserts the old unconditional-rename behavior,
  which is `organize`'s behavior now, not the (now default) `keep` state.
  Two new tests: `test_metadata_save_in_keep_mode_never_renames_the_folder`
  (Recording PUT and Performance PUT, both no-ops under `keep`) and
  `test_retitle_in_keep_mode_leaves_the_file_alone` (file and DB both keep
  the old name, no warning).
- `tests/test_verify_checksums_scoped.py` — new file: a CD1/CD2 `keep`-mode
  ingest with each disc's own checksum.md5 (shared "01.flac" basename);
  confirms ingest-time matching AND a subsequent `POST .../verify-checksums`
  both resolve each disc's file to its own track, and that the
  `RecordingFingerprint` rows carry their own `rel_path`.

Tests run: `tests/test_folder_collision_e2e.py` + `tests/test_verify_
checksums_scoped.py` + `tests/test_folder_naming.py` (18 passed) →
`tests/test_db_logic.py` (64 passed, includes the D7 backfill-vs-original
matching fix) → `tests/test_no_undefined_names.py` (83 passed) → `node
--check app/static/js/app.js` (clean) → full suite: **895 passed** (892 + 3
new), 0 failed.

Nothing moved to `_to_delete/` this chunk.

## Chunk 8 — Rename Files action

Files touched:
- `app/api/recordings.py` — `GET /api/recordings/<id>` gains `files_staged`
  (count of tracks whose `rename_plan()` output under the active global
  scheme differs from their current basename; a bad custom template fails
  closed to 0 rather than breaking the recording page). New `POST
  /api/recordings/<id>/rename-files`: body `{scheme, template}` optional
  (defaults to the active global scheme when omitted — the per-click
  override is never persisted, section 11); flattens into the folder root
  when `flattens(scheme, template)`, otherwise keeps each file's current
  subdir and only substitutes the basename (same rule
  `resolve_ingest_file_path()` uses at ingest, applied here to files already
  in the library); collisions go through `unique_file_name()`; per-file
  failures are reported in `errors`, not rolled back; writes a
  `files_renamed` event with the count on any success; re-runs checksum
  matching afterwards. `verify_checksums()`'s matching loop was extracted
  into `_rematch_and_verify_checksums(rec, folder_abs)` so both this new
  endpoint and the existing verify-checksums endpoint share the identical
  scoped-matching logic rather than drift apart. No backend admin gate was
  added (matches its sibling `write_tags`/`verify_checksums`, neither of
  which has one either) — the button itself is `canEdit`-gated per spec 6.4;
  see "decided alone".
- `app/static/js/api.js` — `API.recordings.renameFiles(id, {scheme,
  template})`, `API.naming.preview({scheme, template, recording_id})`, and
  `API.system.getFileHandling()`/`setFileHandling()` (needed now for the
  dialog's default scheme and keep-mode sentence; chunk 9 reuses the same
  two methods for Settings, so they're not duplicated there).
- `app/static/js/app.js` — `filesStaged` read off the GET payload beside the
  existing `stagedCount`; a new "Rename Files" button in `.pane-acts`
  (`btn-rename-files`, `data-for="filetags"`, `canEdit`-gated, amber via
  `pane-act--staged` when `filesStaged > 0`) beside Write Tags to Files.
  `actRenameFiles()`: a real dialog (the established `.modal-overlay`/
  `.modal-card` pattern, same shape as the Delete Recording dialog) with a
  scheme `<select>` (four presets — Custom is chunk 12), a live Now/After
  preview from `API.naming.preview()` refreshed on scheme change, the
  keep-mode sentence when `file_handling_mode === 'keep'`, and Cancel/Rename
  buttons; confirming calls `API.recordings.renameFiles()` and re-renders
  the recording view. Write Tags' own `confirm()` dialog is UNTOUCHED here
  (that replacement is chunk 9's, per the build plan).
- `app/static/css/main.css` — added `.fh-prev` (the Now/After preview table
  shape from `03-mockup.html` panels 2/2b/4b/4c) since the Rename Files
  dialog needs it before Settings' own preview table exists in chunk 9;
  chunk 9 reuses this class rather than defining its own.
- `tests/test_rename_files_action.py` — new file, 7 tests: `files_staged`
  nonzero/zero on the GET payload; a plan computation has no disk effect;
  rename with the global scheme (event note, `file_path` updated, files
  moved); a per-click override scheme applies without touching the stored
  `naming_scheme` setting; one missing file is reported without blocking
  the rest; a rename re-matches an existing checksum file's tracks.

Decided alone:
- No backend admin/role gate on the new endpoint — every existing sibling
  write-side endpoint in this blueprint (`write-tags`, `verify-checksums`,
  `reveal`, `move`) is `@login_required` only, with editing restricted at
  the frontend via `canEditLibrary()`. Adding a bespoke admin check here
  alone would be an inconsistent, unrequested hardening pass across one
  endpoint while its neighbors stay as they are.
- `_rematch_and_verify_checksums()` extraction: not asked for by name, but
  chunk 8's own "re-runs checksum matching (D7)" requirement would otherwise
  mean either duplicating the chunk 7 scoped-matching block verbatim (a
  drift risk the codebase's own conventions elsewhere explicitly avoid — see
  the `ChecksumMatchProxy` unification in chunk 7) or reimplementing it
  differently. Factored, not duplicated.

Tests run: `tests/test_rename_files_action.py` (7 passed) →
`tests/test_no_undefined_names.py` (83 passed) → `node --check
app/static/js/app.js` + `app/static/js/api.js` (clean) → `npx eslint
--config tools/eslint.config.mjs app/static/js/app.js` (clean, no findings)
→ full suite: **902 passed** (895 + 7 new), 0 failed.

Nothing moved to `_to_delete/` this chunk.

## Review fix pass (2026-09-25)

Baseline observed here: **918 passed, 0 skipped** (review reported 919/3
skipped — not reproduced; not investigated further, logged as a discrepancy
only). Final: **923 passed, 0 skipped, 0 failed** (918 + 5 new tests in
`tests/test_review_fixes.py`).

Per finding:

- **B1** (`app/utils/ingest.py` `build_scan_payload`, `~2071-2078`): added
  `disc_number`/`disc_track_number` to each `audio_files` entry.
  `app/static/js/app.js` `buildIngestTracks`/`mk()` (`~713-742`): added a
  `discByRelPath` map beside `setByRelPath` and set both fields on every
  emitted track row. Proven by
  `tests/test_review_fixes.py::test_real_ingest_path_carries_disc_source_tag_shnid_and_fp_rel_path`
  (scan_folder → build_scan_payload → real `/api/ingest/confirm` POST →
  `Track.disc_number`/`disc_track_number` on both discs).
- **B2** (`app/utils/file_naming.py` `rename_plan`): now computes each
  proposed name as a full rel path — the current `dirname` is kept when
  `flattens()` is false — and dedupes on that path, not the bare filename.
  `app/api/recordings.py` GET `files_staged` (`~368-372`) and
  `rename_files_action` (`~990-1010`) updated to compare/use the full rel
  path directly (the endpoint no longer re-derives a dir prefix — that used
  to double it up under `original`). Proven by
  `tests/test_review_fixes.py::test_keep_mode_rename_files_idle_on_nested_two_disc_recording`
  and `test_original_scheme_on_nested_keep_recording_keeps_each_discs_own_path`
  (ported from the reviewer's probe).
- **S1** (`app/utils/ingest.py` `move_to_library`): `unique_folder_name()`
  now gets `keep_abs=str(src)`; when the resolved destination equals the
  resolved source, the move is skipped and the existing relative path is
  returned. Proven by
  `test_keep_mode_source_already_at_destination_is_not_suffixed` (ported
  from the reviewer's probe).
- **S2** (`app/utils/ingest.py` `build_scan_payload` fingerprints,
  `~2062-2069`): added `rel_path`. `app/api/ingest.py` `_do_confirm` step 9
  (`~1168-1173`): falls back to `fp.get("content")` when the on-disk re-read
  fails. `app/api/recordings.py` `_rematch_and_verify_checksums`
  (`~1175-1179`): dedupe key is now `rel_path` (falling back to filename),
  not filename alone — two discs' own `checksum.md5` no longer shadow each
  other. Proven by the same real-path test as B1 (asserts both
  `RecordingFingerprint.rel_path` rows exist with content, and both tracks'
  `checksum_status == "match"`).
- **S3** (`app/utils/ingest.py` `_LEADING_NUM_RE`): now
  `r"^(\d{1,3})(?=\D|$)"` (a 4-digit run, e.g. a leading year, no longer
  matches), and a parsed number is trusted only when EVERY audio file in
  that disc folder parses one — one exception falls the whole disc back to
  1-based counter order. Proven by
  `test_leading_date_filename_does_not_give_a_bogus_disc_track_number`
  (ported from the reviewer's probe).
- **S4**: `app/api/recordings.py` GET payload now has `disc_count` (max
  `disc_number`, or `None`). `app/static/js/app.js` Source block renders a
  `Discs` cell (mockup panel 4's exact label) only when `disc_count > 1`.
  Not separately unit-tested (thin derivation + a conditional render); no
  regression suite entry.
- **S5**: `run.py` — removed the "Two things Trellis will do…" paragraph
  under first run's "Use a library I already have".
- **S6**: `app/static/js/app.js` — the mode segment click now opens a
  one-sentence confirm dialog (`.modal-overlay`/`.modal-card`, same pattern
  as Delete/Write Tags/Rename Files) before PUTting `file_handling_mode`.
  **Decided alone**: the confirmation sentence itself ("Switching to
  '<mode>' rewrites the switches below to match it.") — the review flagged
  this needs Ryan's copy and the mockup carries none; wrote a plain,
  functional line rather than leave the Should-fix unbuilt. Flag for Ryan
  to replace if he wants different wording.
- **S7**: `app/api/ingest.py` `_do_confirm` Recording() constructor
  (`~1092-1097`): now sets `source_tag`/`etree_shnid` from the payload
  (previously silently dropped even though the field was already read
  elsewhere). Add Recording form: two new fields ("Source tag", "shnid" —
  same labels as View Recording's existing Source block) seeded from
  `scan.suggestions.from_info_file`, read back into `ingest.form` and
  forwarded via the existing payload spread. The two other confirm sites
  (Bulk Import direct ingest and the triage queue's `ingestOne`, both
  formless) fall back to the scan's folder-name suggestion directly.
  Covered end to end by the same real-path test as B1/S2 (asserts
  `Recording.source_tag == "schoeps"`, `etree_shnid == 118671`).
- **S8**: `app/static/js/app.js` Rename Files dialog — added a `Custom`
  `<option>` (disabled when the global template is empty), and both the
  preview and confirm calls now send `template` alongside `scheme` when
  `custom` is selected.
- **S9**: `app/utils/node_settings.py` `set_file_handling` — saving
  `naming_scheme=custom` with an empty effective template is now a
  `ValueError` ("a custom scheme requires a template") instead of being
  accepted and failing later, mid-ingest, inside `rename_plan()`.
- **S10**: `app/utils/folder_naming.py` `rename_recording_folder` — the
  `rename_folders` gate now lives inside the function (wrapped in
  `try/except RuntimeError` so the pure duck-typed unit tests in
  `test_folder_naming.py`, which call it with no Flask/DB context by
  design, are unaffected); docstring rewritten to state the gate and to
  drop the stale "still-unbuilt Update File Names button" line (that button
  is Rename Files, built in an earlier chunk). The two existing callers keep
  their own gate (harmless double-check).
- **S11**: `scripts/migrate_file_handling.py` — `--apply` now refuses when
  `<db>-wal` is non-empty (likely a running app) and takes a `.bak` copy of
  the live db before opening it; the `disc_number already present` refusal
  is removed since every step is already idempotent.
  `tests/test_disc_migration.py::test_already_migrated_database_refuses_to_run_again`
  rewritten to `test_already_migrated_database_is_a_no_op_on_second_run`
  (asserts a second `--apply` succeeds and changes nothing) — the old test
  asserted the exact behavior S11 removes.
- **N1**: removed the dead `behavior = "move"` local in
  `app/api/ingest.py` and the dead `ingest.behavior: 'move'` state field
  (with its now-orphaned comment) in `app/static/js/app.js`.
- **N2**: reworded the four stale "audio is now always flattened" /
  "renames file when title changes" comments (`ingest.py:519`,
  `api/ingest.py` fingerprint-scoping comment, `checksums.py:174`,
  `api/tracks.py:6`) to reflect that flattening/renaming are scheme- and
  switch-conditional; `node_settings.py` and `system.py` "five switches" →
  "six".
- **N6**: `app/static/js/app.js` Set cell's `onSave` no longer calls
  `markStaged()` — `write_flac_tags` writes no set tag, so editing Set has
  nothing for Write Tags to write.
- **Not fixed** (left as found, each is more than a line or two): N3
  (etree/etree_sets preset bracket-group convention — a design question,
  explicitly out of scope: "do not re-open design questions"), N4 (rename
  dialog's keep-mode sentence omits the file count — the count isn't known
  until the async preview resolves, needs restructuring, not a 1-2 line
  fix), N5 (custom-template preview debounce vs. blur-only refresh), N7
  (already resolved as a side effect of B2), N8 (leftover empty disc dirs
  after a flatten; a 01↔02 swap renaming order edge case), N9 (mode/
  placement not seeded from the first-run marker JSON — functionally
  equivalent, per the review's own note), N10 (rename-files/verify-
  checksums are `@login_required` only, consistent with every sibling
  endpoint on the blueprint — an endpoint-by-endpoint role-gate pass is out
  of scope for this fix pass), N11 (`_render_parts` nested-group dropping).

`_to_delete/`: moved `/tmp/review_tests/` (the reviewer's throwaway probes,
outside the repo) to `_to_delete/review_tests_tmp/` — the useful cases were
ported into `tests/test_review_fixes.py` proper; nothing else moved.

Tests run: `tests/test_review_fixes.py` (5 passed, new) →
`tests/test_file_naming.py` + `test_rename_files_action.py` +
`test_db_logic.py` (112 passed) → `test_disc_migration.py` (8 passed,
1 rewritten) → `test_folder_naming.py` + `test_ingest_utils.py` +
`test_folder_collision_e2e.py` (43 passed) → `test_node_settings`/
`file_handling`/`system` (45 passed) → `tests/test_no_undefined_names.py`
(83 passed) → `node --check app/static/js/app.js` (clean) → `npx eslint
--config tools/eslint.config.mjs app/static/js/app.js` (clean) → full
suite: **923 passed**, 0 skipped, 0 failed.

## Review fix pass 2 (2026-09-25)

Second re-review, against the "Re-review" section of `review.md` appended
after fix pass 1. Baseline: 923 passed (fix pass 1's final count). Final:
**932 passed, 0 skipped, 0 failed** (923 + 9 new tests: 4 in
`tests/test_review_fixes.py`, 1 in `test_file_handling_settings.py`, 2 in
`test_folder_naming.py`, 2 in `test_disc_migration.py`).

- **R2 (Blocker)**: `app/utils/ingest.py` `move_to_library`'s same-place
  branch (S1's fix) now walks the audio files in place and renames each one
  to `resolve_ingest_file_path(rel, audio_rename_map, flatten)` when it
  differs, guarded with `unique_file_name(..., keep_abs=...)`, instead of
  returning without touching anything. Keep mode still does nothing (its
  `audio_rename_map` is the identity map and `flatten` is `False`, so every
  resolved path already equals the current one) — the same code path
  covers both modes without a mode check. Proven by
  `test_organize_mode_reingest_in_place_applies_rename_map_on_disk`
  (organize-mode re-add of an in-place folder; asserts every
  `Track.file_path` exists on disk and really was renamed).
- **R4 (Blocker)**: reverted `node_settings.py`'s S9 rejection of
  `naming_scheme=custom` with an empty template — that check ran at
  Settings-save time, where the scheme select saves `{naming_scheme:
  'custom'}` alone the instant it's picked, before the (until-then
  readonly) Template field has anything in it, so the very first save
  always hit it and reverted the select. `naming_scheme=custom` with an
  empty template is accepted again as a transitional state; the
  ingest-time rejection (`rename_plan()`/`compute_audio_rename_map()`
  raising `TemplateError` before any file move) is unchanged, so S9's
  actual concern — an ingest crashing mid-job on a template nobody wrote —
  still can't happen; it just fails at the (already-safe) ingest boundary
  instead of blocking the Settings save. Proven by
  `test_put_file_handling_selects_custom_with_no_template_yet` (PUT-level:
  selecting Custom alone succeeds, adding a template afterwards succeeds
  and validates, and an actually-broken template is still rejected once
  there's a scheme to validate it against).
- **R1 (Should fix, high)**: `compute_audio_rename_map()`
  (`app/utils/ingest.py`) now stores `os.path.basename(proposed)` in the
  rename map, not the full proposed rel path `rename_plan()` returns after
  B2's fix — `resolve_ingest_file_path()` is what decides the destination
  directory at ingest time (prepending the ORIGINAL parent for a
  non-flattening scheme), so storing the full path too was double-prepending
  it (`CD1/CD1/Dark Star.flac`). Proven by
  `test_custom_template_no_position_token_does_not_double_the_disc_dir`
  (a custom template with no position token on a CD1/CD2 source).
- **R3 (Should fix, high)**: `etree_shnid` is now validated/coerced to
  int-or-`None` in TWO places, both before any file move: the `/confirm`
  route itself (before the background job/thread is even started — a bad
  value is a clean 400 with the source folder untouched) and again at the
  very top of `_do_confirm()` (for a direct caller that bypasses the
  route). The later `Recording()` constructor now just reads
  `data.get("etree_shnid")` straight through. Proven by
  `test_confirm_rejects_non_numeric_shnid_before_moving_anything` (HTTP
  level: 400, source folder and file still there, no Artist row created)
  and `test_do_confirm_rejects_non_numeric_shnid_directly_before_any_move`
  (same, calling `_do_confirm` directly).
- **S10 (fail-open → fail-closed)**: `rename_recording_folder()` gained an
  explicit `rename_folders=None` parameter. Left `None`, it reads
  `node_settings.get_file_handling()["rename_folders"]` as before, but now
  fails CLOSED (returns `None`, no rename) on `RuntimeError` (no app/DB
  context) instead of falling through as if the switch were on. A caller
  with no context — or the pure duck-typed unit tests in
  `test_folder_naming.py`, by design — states the mode explicitly instead.
  The two existing tests that exercised a rename with no context now pass
  `rename_folders=True`. Proven both ways by
  `test_no_app_context_and_no_explicit_mode_fails_closed` (no context, no
  argument → no rename, even though the name has drifted) and
  `test_explicit_rename_folders_false_is_a_no_op` (context absent, mode
  stated explicitly as off → still no rename).
- **S11 (true no-op)**: `seed_file_handling_mode()` in
  `scripts/migrate_file_handling.py` now checks for an existing
  `file_handling_mode` row FIRST and returns without touching anything if
  one exists (`ON CONFLICT DO NOTHING` on the INSERTs as a second layer),
  rather than `ON CONFLICT DO UPDATE`, which used to flip a user's `keep`
  (or any hand-edited switch) back to `organize`'s defaults on every
  `--apply`. The pre-apply backup is now `sqlite3.Connection.backup()`
  (SQLite's own online backup API, against a read-only connection to the
  live file) instead of `shutil.copy2`, so it can't read a torn page even
  in the small window the WAL check doesn't cover; `import shutil` removed
  as now-unused. Proven by
  `test_second_apply_never_overwrites_a_user_chosen_keep_mode` (seeds
  `keep` + a hand-set switch after the first `--apply`, runs a second, both
  survive unchanged) and `test_apply_backs_up_the_database_with_sqlite_backup`
  (the `.bak` file is itself a valid, openable pre-migration SQLite
  database).
- **S6**: left as flagged — Ryan's sentence, per the coordinator's
  instruction this pass.

Not re-opened: N3 (design question, unchanged from fix pass 1's note).

Tests run: `tests/test_review_fixes.py` (9 passed, +4 from fix pass 1) →
`tests/test_file_handling_settings.py` (23 passed, +1) →
`tests/test_folder_naming.py` (10 passed, +2) →
`tests/test_disc_migration.py` (10 passed, +2) →
`tests/test_ingest_utils.py` + `test_folder_collision_e2e.py` (35 passed,
unaffected by S10) → `tests/test_no_undefined_names.py` (83 passed) →
`ast.parse` on every touched `.py` file (clean) → `node --check
app/static/js/app.js` (clean) → `npx eslint --config
tools/eslint.config.mjs app/static/js/app.js` (clean) → full suite:
**932 passed**, 0 skipped, 0 failed.
