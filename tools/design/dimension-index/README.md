# Dimension index redesign (draft)

Mockups: `artists.html`, `musicians.html`, `venues.html`, `events.html`, `genres.html`.
They load the repo's real `app/static/css/main.css` (via `../../../`), then `dimension-index.css`
(the proposed new rules, `dx-` prefixed, tokens only). A palette picker sits top right in each
mockup (all 12 schemes, light and dark). Sort, filter and search work on the sample.
Open them straight from disk. No app code was touched.

Data: `db/_cowork.db`, opened `mode=ro&immutable=1`.
That predates Lomax (10-05), so bios and photos added since are not counted.
`build.py` regenerates everything; `coverage.json` holds the numbers below.

Pictures: the app's image files are not in the repo, so the mockups show the fallback state until
you run, on the Mac mini: `python3 fetch_photos.py "$HOME/Library/Application Support/Trellis Music Library"`
(copies the sample's ~95 files into `photos/`, read-only on the app's data; adjust the path if the data dir differs).

## Sample rule (no hand picking)

Per dimension, 25 rows (Events has 30 rows total, so 25 of 30): rank rows by richness (has picture, has text),
take the 8 richest by lowest id, fill the rest with a systematic sample (every k-th row, A-Z) of what remains.
The sample is therefore richer than the library. Real coverage is below.

## Coverage, full snapshot

| Dimension | Rows | Picture | Description | Other usable fields |
|---|---|---|---|---|
| Artists | 277 | 137 (49%) | 16 (6%): 13 `bio` + 3 more from legacy `dossier_json` | genre 204, MB area 129, members listed 161, 100 have a Wikidata link |
| Musicians | 482 | 2 (0.4%) | 9 (2%) | instrument 73, on record 383, in 2+ artists 68 |
| Venues | 549 | 6 (1%) | 1 (0.2%) | city 535, country 534, 532 have a recording |
| Events | 30 | 2 (7%) | 0 (the `notes` column is empty) | venue/place/year 30 of 30 (derived from performances; the event row itself stores no dates or venue) |
| Genres | 13 | none exist | 0 | colour 12 of 13 (Hindustani has none) |

Related: 73 artists have no genre; 108 of 803 recordings sit under an artist with no genre colour.
Artist photos cover 583 of 803 recordings (73%) because the photographed acts are the busy ones.

Consequence for design: only Artists can lead with a picture. Everything else must be good with none.

## Layout rationale

Shared idea: one full-width ROW per record, hairline separators, no boxes, no pills. The row is a
small editorial entry: name in the display face, a quiet line of facts, an optional text excerpt
(3 lines), then a counts column on the right in the display face with tracked small caps labels
(the handbill idiom: numbers and dates are the identity). Text only shows if it exists, so empty
fields cost no space. Corners: `--r-img` on pictures, nothing larger.

- **Artists**: 128px square on the left, the only dimension that earns a big picture. Fallback is the
  genre colour with big initials (same as Top Shelf), so the 51% without a photo still read as
  designed and colour-coded. Genre name carries a tiny colour key. Members line, bio excerpt,
  recordings and year span at right. Row height about 160px.
- **Musicians**: deliberately simple. 52px slot, neutral field when empty (no genre colour: a
  person is not a genre). Grouped under large letter gutters (A, B, C...), because 482 rows need
  a way to find a place. Line 2 is instrument(s), line 3 is the artists they belong to.
- **Venues**: text-led, no picture slot at all (1% have one). If a venue has a picture it rides at
  the right as a 16:10 landscape thumbnail, so the left edge never goes ragged. Line 2 is place,
  line 3 is who recorded there.
- **Events**: the date is the headline (tracked caps above the name), grouped by year with a year
  gutter, as the listing of a gig poster wall. Venue and place, lineup of the top artists. Optional
  right-hand landscape picture as for Venues.
- **Genres**: three alternatives in `genres.html` (no photos, no descriptions, colour only):
  - **A. Composition strip and ledger.** One strip across the page, segments sized by recordings
    share, then plain rows. Most honest "colour is the signal" option, cheapest to build.
  - **B. Colour key and roster.** A 96px colour square holding the recording count, the name, and a
    run of 8 artist faces (photos where they exist, genre-colour initials otherwise). Brings the
    photos into a page that has none. Most engaging, needs an extra top-artists query.
  - **C. Decades.** A shared eight-column strip (50s to 20s); each genre's cells fill by how much
    was recorded then. Tells you something the other two do not. Needs a per-decade count query.
  My lean: B if you want engagement, A if you want it quiet. C is the one a collector would stare at.

## Controls proposed

Existing furniture kept: search box, text sort buttons, count. Additions are one select each.

| Page | Sort | Filter |
|---|---|---|
| Artists | A-Z (default), Recordings, Earliest | Genre select |
| Musicians | A-Z (default, grouped by letter), Recordings | none |
| Venues | A-Z (default), Recordings, Place | Country select |
| Events | Date (default, grouped by year), A-Z, Recordings | none (30 rows) |
| Genres | Recordings (default); A-Z if B or C | none (13 rows) |

Admin-only, hidden in Playback (not mocked): a "No picture" / "No description" filter on Artists, to
work the curation backlog (140 artists without a picture, 261 without a description).

## Fallback rules (missing picture)

Applies per row, never leaves a hole:
1. Own primary image (`is_primary`, else first by `sort_order`).
2. Artists: genre-colour field plus initials. No genre: neutral `--bg-4` field with quiet initials.
3. Musicians: neutral field plus initials. Do NOT borrow the band photo; a group portrait labelled as a
   person is wrong. (The recording chain, own image then artist image then initials, is right for
   recordings and wrong here.)
4. Venues and Events: no slot reserved; the row is complete without it.
5. Genres: the colour. No colour (Hindustani): `--t3` field.
Text: show nothing when the field is empty. No "No description" placeholder.
Commons pictures carry required credit; the index shows none, the record page already does.

## Not done / caveats

- Not visually verified in a browser from this sandbox. JS was run against a DOM stub (all five render
  without errors); layout and every palette still need your eye.
- `.dim-tile` and `.dim-grid` rules stay in main.css untouched; the mockups do not use them.
- Em dashes: none in UI strings. Date and year ranges use a plain hyphen.
- The page title block is simplified; the real pages keep `entityShellHtml` (hero, stats, tabs).
- Musician recording counts follow the app's existing definition (through membership), so a
  musician with two stints in one act is counted once per join row there, as today.

# Research: can artist photos be pulled automatically?

No network calls were made for this; it is from the code and from what I know of each source.
Terms change, so each "yes" needs the source's current terms checked at build time.

## What the app already does

`app/utils/commons.py` + `POST` photo route in `app/api/artists.py`: MusicBrainz (gate: score 88+, margin 12+)
→ `mb_links_json` Wikidata URL → QID → P18 (then Commons category P373 as a fallback) → Commons
`file_info` (machine-readable licence, NC/ND/fair-use refused, unreadable licence refused) → 900px
thumbnail stored with credit. Per-artist click ("Find a free photo"), never batch, never automatic, fails soft.

Measured on the snapshot:
- 277 artists; 144 have an MBID (135 matched, 9 linked by hand); 122 looked up and found nothing, 4 ambiguous, 7 never looked up.
- 100 have a Wikidata link (the other 44 matched on MB have none).
- 85 of those 100 already have a picture; 15 do not (no P18 or licence refused).
- 69 artists hold a Commons picture (80 rows); 68 more artists have uploaded pictures.
- So the chain is close to its ceiling for artists: at most about 15 more from re-running it, plus
  Wikidata links MB gains later. It reaches notable acts only; the 133 unmatched artists are mostly
  obscure bluegrass and local acts that will never have a Commons photo.

## Other legitimate sources

| Source | Usable? | Licence and risk |
|---|---|---|
| Commons by Wikidata P18 (adopted) | Yes | Free licences only; attribution captured. Never use Wikipedia article HTML or the REST page summary image: it can be a local non-free upload. |
| Musicians via MB person → Wikidata → P18 | Yes, new work | `musician` has no MBID or Wikidata columns. MB lists band members as person records; resolving them needs a new match step (and the "members never written back" rule means it must stay a separate, human-gated link). Only famous people have P18. |
| Venues via MB place → Wikidata → P18 | Yes, new work | Buildings photograph well on Commons (Fillmore, Winterland, Red Rocks). Same licence handling. Best yield after artists. |
| Events via Wikidata (festival items) | Partly | P18 is sometimes a logo or poster; logos are often trademarked or not free. Human pick only. |
| Commons search by name, no Wikidata | Only with a human choosing | High wrong-match risk (same name, different act). |
| Openverse API (CC search incl. Flickr) | Possible, human pick | Returns licence per item, but includes NC and ND variants; filter to CC0, BY, BY-SA. Per-image attribution. Quality and identity unverified. |
| Flickr API directly | Possible, human pick | CC filter available, but licences include NC/ND, and API terms limit caching and commercial use. Weaker than Commons for the same photos. |
| Wikidata `description` (text, CC0) | Yes, for rows | One-line descriptions ("American rock band") for ~100 artists with zero attribution burden. Not stored today. |
| Wikipedia extract (text, CC BY-SA) | Possible | Needs visible attribution and share-alike on the stored text. Different from the "free text stays out" rule for the Atlas; your call. |
| MusicBrainz data | Core data CC0 | Annotations and ratings are under a non-commercial licence; do not store those. |
| Cover Art Archive | Release covers only | Not artist photos; relevant to studio records, rights unclear per image. |
| Discogs API images | No | Contributor copyrighted, terms restrict storing and redistribution. |
| Spotify artist images | No | Developer terms require attribution to Spotify and limit storage and redistribution; incompatible with peer sharing. |
| Last.fm | No | Stopped serving real artist images years ago; terms forbid it anyway. |
| Fanart.tv, TheAudioDB | No | Crowd uploaded, licence unstated, commercial use gated. |
| Internet Archive / Live Music Archive | No | Item thumbnails are auto-generated or uploader-owned; no rights statement. |
| Press kits, official sites | Manual only | Permission by email. Cannot be automated, and the credit has to say so. |

Conclusion: Commons via Wikidata is the only automatic source worth having, and it is already built.
Growth is by extending the same chain to venues (and maybe musicians), not by adding a second
source. Everything else is either unlicensed, restricted, or needs a human to pick.

## Open decisions

1. Genres: A, B or C (or drop to just keeping the current swatch tile)?
2. Musicians: is "neutral initials, never the band photo" right, or fall back to the band's picture?
3. Descriptions: show Wikidata one-liners (CC0) as the lowest-priority description for artists, or only your own bio and Lomax bios?
4. Extend the Commons chain to venues (MB place → Wikidata → P18)? To musicians (needs MB person matching)?
5. Venue and event pictures at the right as landscape: right, or should these dimensions drop pictures from the index entirely until coverage is real?
6. Admin "No picture" / "No description" filters on the Artists index: wanted?
7. Row density: artist rows are about 160px tall (about 5 per screen). Acceptable, or shrink the picture to 96px?
