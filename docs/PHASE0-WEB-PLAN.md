# Phase 0 — v2 plan and design plan

Plan only. No code until both halves are approved.

Companion to [PHASE0-PLAN.md](PHASE0-PLAN.md), which covers the CLI the web console
sits on. Section numbers below refer to `PROMPT-WEB.md` unless marked *v1*.

---

# Part 1 — What the existing code gives us, and what has to move

The core is 50 modules / 9,337 lines with 385 tests. Most of it is reusable as-is.
Three things are not, and one of them is a genuine conflict inside the brief.

## 1.1 Reusable untouched

| Module | Why it needs no change |
|---|---|
| `images/pipeline.py`, `images/validator.py` | §0 forbids touching them. The API reads `ImageResult.method` and `.reason` and displays them; there is no write path. |
| `pipeline.process_url` | Already the single per-URL coroutine. The API calls it exactly as `batch` does. |
| `extract/*`, `enrich/*`, `policy/*` | Pure functions over HTML and records. |
| `output/csv_writer.py` | Already the only module that knows column names, already header-locked. Needs one new capability (§1.4) but no change to what it emits. |
| `batch.DomainLimiter`, `RobotsCache` | The politeness layer is per-domain and process-wide; a job queue of one (§10.6) keeps it correct. |

## 1.2 F1 — §2.1 and §2.3 are in direct tension

> §2.1: *"Row order is input order, not completion order."*
> §2.3: *"The file is written incrementally, never held in memory."*

You cannot append a row the moment it completes *and* have input order, because row 7
routinely finishes before row 3. Something has to give, and the brief doesn't say which.

The three ways out, and why two are wrong:

- **Write on completion, sort at the end.** Fails §2.3 — a job that dies at #480 leaves
  a file in the wrong order, and the UI's mid-run download is unsorted.
- **Buffer completed `ProductRecord`s until their predecessor lands.** Fails §2.3's spirit
  and its memory intent. If row 3 is on a 20s timeout while 4–200 complete, the buffer
  holds 197 full records — each carrying description text, per-candidate validation
  results and image metadata. That is the exact profile v1's batch mode was built to avoid.
- **What I propose:** the **ledger is the source of truth; `listings.csv` is a projection
  of it.** Each row is committed to SQLite the instant it completes, tagged with its input
  index. A watermark writer then appends to the CSV in index order, holding only *rendered
  CSV lines* (~1 KB each) for rows that finished ahead of the watermark. 500 rows of
  worst-case buffer is ~500 KB of strings, not ~50 MB of records.

This resolution earns its keep four more times over:

- §2.3's "download what's done so far" becomes trivially valid — regenerate from the
  ledger, in index order, on demand.
- §4's re-export is the same operation with edits applied.
- §7's *"every SSE event and API response is derived from the ledger, not from in-memory
  state"* falls out for free rather than being a second mechanism.
- §5's resume knows exactly which indices are missing.

**This is the single most consequential decision in Part A, so it is F1.**

## 1.3 F2 — the job model collides with v1's cross-run append

Today `listings.csv` lives at the repo root, is **appended across runs**, and
`HaatCsvWriter` skips any row whose canonical URL is already in the ledger. That was
right for a CLI whose ledger was global.

§1 makes every run a job with its own directory. Two consequences:

1. **Dedupe must become job-scoped.** Under the current rule, running a second job over
   the same catalogue would produce an empty `listings.csv` — every row "already listed".
   Correct behaviour: dedupe within the job's input (§2.2), and let a *different* job
   re-emit the same product freely.
2. **`rows` needs a job identity.** Today its key is `canonical_url`, globally unique.
   Two jobs over the same URL must both be able to store a row.

Proposed schema change — additive, no migration of existing columns:

```sql
CREATE TABLE jobs (
    job_id      TEXT PRIMARY KEY,          -- j_ + 8 url-safe chars
    created_at  TEXT NOT NULL,
    state       TEXT NOT NULL,             -- queued|running|cancelled|done|error
    settings    TEXT NOT NULL,             -- the job.json payload
    input_count INTEGER NOT NULL
);

CREATE TABLE job_urls (                    -- the §2.4 accounting ledger
    job_id      TEXT NOT NULL,
    input_index INTEGER NOT NULL,          -- position in the pasted list
    source_url  TEXT NOT NULL,             -- exactly as pasted
    canonical   TEXT NOT NULL,
    outcome     TEXT,                      -- NULL|listed|failed|skipped_robots|duplicate
    PRIMARY KEY (job_id, input_index)
);

CREATE TABLE row_edits (                   -- §4: originals are never overwritten
    job_id      TEXT NOT NULL,
    row_key     TEXT NOT NULL,
    field       TEXT NOT NULL,
    value       TEXT NOT NULL,
    edited_at   TEXT NOT NULL,
    PRIMARY KEY (job_id, row_key, field)
);
```

and `rows` gains `job_id` + `input_index`, with the primary key becoming
`(job_id, row_key)`.

`job_urls` is what makes §2.4's assertion checkable rather than aspirational: every input
line has a row in it from the moment the job is created, and the job cannot reach `done`
with `outcome IS NULL` anywhere.

**Keeping the v1 CLI behaviour:** `--out PATH` still writes wherever you point it, and
`single` keeps writing to the repo root. Only `batch` and the web console adopt job
directories. I'd rather not break a command you already have muscle memory for.

## 1.4 What `csv_writer.py` needs

One addition, no change to output bytes: an **index-ordered projection mode** that takes
`(input_index, rendered_row)` pairs and emits them in order behind a watermark. The
existing `checkpoint()` (added in v1 Phase 9) already handles making partial files
durable; this composes with it.

`review.csv` and `image_manifest.csv` are already whole-file rewrites, so they simply
regenerate from the ledger in index order. `failed.csv` is a new fourth writer with the
five columns §2.5 names.

## 1.5 F3 — the SSRF guard will block the demo shop, and probably your first test

§10.2 requires rejecting loopback and private ranges. `demo/shop.py` serves on
`127.0.0.1:8799`, and anyone testing a staging site on a LAN address hits the same wall.

A guard with a documented escape hatch is a real guard; one people disable wholesale
because it blocked their first attempt is not. Proposal:

- Guard **on by default**, enforced in one place (`utils/netguard.py`) and called from
  both the page fetcher and the image downloader, re-checked on **every redirect hop**
  via an httpx event hook — §10.2 is explicit that checking only the input makes it
  decorative.
- `fetch.allow_private_hosts: []` in `config.yaml` — an explicit allowlist of hosts, not
  a boolean. Shipping with `127.0.0.1` in it would defeat the point, so it ships empty and
  `demo/urls.txt` gets a comment saying to add `127.0.0.1` to run the demo.
- The API **never** accepts an allowlist from the request body. It's config-file only.

I want your call on whether shipping empty is too much friction for the demo.

## 1.6 Smaller findings

| # | Finding |
|---|---|
| F4 | §4 says edits get `source="human"`. The `FieldSource` enum has `OPERATOR`, which already means this. I'll use `OPERATOR` and surface it as "you" in the UI rather than adding a synonym. |
| F5 | §11 wants `web/dist/` committed. `.gitignore` currently has no rule that would catch it, but I'll add an explicit negation so a future `dist/` rule can't silently break the no-Node install path. |
| F6 | §7's `row_stage` list (`fetching → extracting → image:tier1 → …`) doesn't exist in the core — `process_url` is a straight-line coroutine with no progress callback. Adding one is ~10 lines and an optional parameter, and it belongs in the core per §0's architecture rule, not in the API. |
| F7 | §12 Phase 6 is "done when listings.csv downloads **and imports into haat**". That's still the one thing I can't verify for you — same open item as v1. |
| F8 | §10.6 "one job at a time" plus §5 resume means a cancelled job's `DomainLimiter` state is lost on restart. Harmless: the limiter is politeness, and a fresh one is *more* polite, not less. |
| F9 | Playwright is already installed (v1 Phase 10 uses it for Stage B). §13's E2E tests get it for free — but note it's currently a `[render]` extra, so the dev extra needs it too. |

---

# Part 2 — Build plan

Following §12's phases. Estimated sizes are for calibration, not commitment.

| Phase | What lands | Verified by |
|---|---|---|
| **1** | `store/jobs.py` (the three tables), `output/ordered_writer.py`, `failed_writer.py`, `runs/<job_id>/` layout, `job.json`, job-scoped dedupe, the §2.4 assertion. `batch` moves onto it. | `test_output_order_matches_input_order` with jittered completion; `test_every_input_url_accounted_for`; a real 200-URL run against `demo/shop.py` |
| **2** | `api/app.py`, `serve` command, `/api/health`, `/api/config`, static mount, `127.0.0.1` binding, `--token` gate for `0.0.0.0` | `haat-lister serve` → page reports config status; `test_config_endpoint_never_returns_keys` |
| **3** | `api/runner.py` (single-slot queue, cancel, resume), `api/events.py` (SSE), preflight, the `row_stage` callback in core | job runs from `curl`; `test_cancel_preserves_completed_rows`, `test_resume_processes_only_remainder`, `test_sse_reconnect_no_duplicate_rows` |
| **4** | `tokens.css`, app shell, Compose, Settings, Preflight | one URL end to end in a browser |
| **5** | Running screen, row stream, **the fill grid**, tier badges | 50-URL job against the demo shop is watchable |
| **6** | Complete screen, four downloads + zip | header byte-identical; `test_partial_download_mid_job_is_valid_csv` |
| **7** | Review table, inline edit, bulk apply, re-export | `test_row_patch_validates_against_taxonomy`, `test_gi_region_patch_rejected`, `test_reexport_preserves_original_extraction_values` |
| **8** | History, empty/error states, keyboard pass, reduced motion | full keyboard run; contrast audit against the numbers in Part 3 |
| **9** | `utils/netguard.py` + redirect hook, path-traversal guard, upload caps, README | `test_ssrf_blocks_metadata_endpoint_after_redirect`, `test_download_path_traversal_rejected` |

**Note on Phase 9.** §12 puts security last. I'd rather land `netguard.py` in **Phase 1**,
because from Phase 3 onward the tool is accepting arbitrary URLs over HTTP from a browser,
and a guard added after that window is a guard that was absent while the surface existed.
The rest of §10 can stay in Phase 9. Flagging rather than deciding.

---

# Part 3 — Design plan

§9 asks for two passes: brainstorm, then critique against the generic default before any
code. Both are below, in that order.

## 3.1 Pass one — candidate directions

Five, judged against the pinned subject: *the workbench behind the boutique, for someone
turning 200 messy pages into 200 disciplined rows before dinner.*

| Direction | The idea | Verdict |
|---|---|---|
| **Indigo by depth** (the brief's) | Dye depth encodes confidence. Repeated dips = deeper blue. | **Survives.** The only candidate where the aesthetic *is* the data encoding. |
| Ledger / register | Ruled paper, column rules, running balance. The tool is bookkeeping. | **Rejected** — it is anti-default #3 (broadsheet, hairlines) almost verbatim. |
| Loom / warp and weft | Rows as weft threads; the grid as cloth being woven. | **Rejected** twice over: cultural signalling as texture, which §9 forbids by name; and dishonest — a CSV isn't woven, it's *filled*, one cell at a time. The metaphor would fight the thing it depicts. |
| Contact sheet / darkroom | Frames on a sheet, density as confidence. | **Rejected** — implies a dark canvas (anti-default #2), and images are secondary here. The 19 columns are the artefact; photos are one of them. |
| Kiln / firing schedule | Heat as progress, cones as milestones. | **Rejected** — heat maps to orange and terracotta, straight into anti-default #1. |

Indigo wins on merit, not deference. But "use indigo" is not by itself a design, which is
what pass two is for.

## 3.2 Pass two — the critique, and what changed

**What would the generic default produce, given "indigo" and "operator tool"?** A
slate-blue admin panel: `#1E293B` sidebar with icon nav, white `rounded-xl shadow-sm`
cards on a `#F8FAFC` page, `blue-600` primary buttons, pill badges, a progress bar. That
is Tailwind's default palette with the accent set to blue. It would be *indigo-ish* and
completely anonymous. Five changes, each with a reason that isn't taste:

### C1 · Indigo is spent entirely on the data. Chrome gets none.

If depth encodes confidence, every other indigo on screen competes with the encoding. So:
no indigo buttons, no indigo nav, no indigo links, no indigo focus ring. Buttons are ink
on ground with a 1px border. Links are underlined ink. **The only saturated colour in the
chrome is none.** This is the single biggest departure from the default and the reason it
won't read as an admin panel.

### C2 · No cards.

The generic tool floats white rounded cards on a grey page. Here white is *load-bearing*:
resist-white is the ground of an **editable** cell (§9's bandhani resist — an intentional
absence of dye). A white card would say "editable" everywhere and destroy the semantic.
Separation is by rule and space instead.

### C3 · The left rail carries the artefact, not navigation.

Rail-plus-main *is* the dashboard shape flagged in anti-default #2, so it needs a reason
to exist. Here the rail holds the fill grid and the job's identity — the thing being
made — and navigation is a single line of text links along the top. No icon sidebar, no
collapsible tree. You are looking at your CSV the whole time you work.

### C4 · The confidence ramp is a **fill** ramp, not a text ramp — this came from measurement.

I computed the contrast of every proposed value against the ground rather than assuming:

| Token | Hex | on ground | on resist |
|---|---|---|---|
| `dye-4` (high / ink) | `#16203C` | **14.47:1** | 16.08:1 |
| `dye-3` (medium) | `#2C4372` | **8.77:1** | 9.75:1 |
| `dye-2` (low) | `#5A6E9B` | **4.56:1** | 5.07:1 |
| `dye-1` (none) | `#AFB9CE` | **1.77:1** ✗ | 1.97:1 ✗ |
| `madder` (failed) | `#96261F` | **7.28:1** | 8.10:1 |
| `brass` (needs a human) | `#7A5A12` | **5.73:1** | 6.37:1 |

`dye-1` fails AA badly and can never carry text. That's not a defect to fix — it's the
design telling the truth. The "no value" tier **has** no text; it renders as an empty cell
with a dotted resist outline. Text uses `dye-4`, `dye-3`, `madder`, `brass` only. `dye-2`
at 4.56:1 clears AA but has no margin, so it is reserved for short mono labels at 13px and
never for prose.

Adjacent ramp steps sit 1.65–2.57:1 apart — enough to read as fills side by side, nowhere
near enough to carry meaning alone. So §9's redundancy rule is not optional here:

| State | Colour | Glyph | Weight | Fill |
|---|---|---|---|---|
| high | `dye-4` | — | 500 | solid |
| medium | `dye-3` | `·` | 400 | solid |
| low | `dye-2` | `~` | 400 | 50% dither |
| none | `dye-1` | `–` | — | dotted outline |
| needs a human | `brass` | `⚑` | 500 | solid |
| failed | `madder` | `✕` | 500 | solid |
| edited by you | `ink` on `resist` | `✎` | 500 | resist ground |

### C5 · The signature element needs a second half, or it breaks at N=200.

The brief's fill grid is right, and §9 invites an argument. Here's mine: 19 columns × N
rows is legible at N=50 and meaningless at N=500, where a row is sub-pixel and the promised
click-a-cell interaction is impossible. Worse, the operationally useful fact the brief
itself names — *"`price_inr` is a pale stripe and `title` is solid"* — is a **column**
fact, not a row fact, and column facts are readable at any N.

So the signature element becomes two coupled parts:

- **The column profile** — 19 vertical bars, one per CSV column, each filling by
  confidence-weighted completeness. Always visible, always readable, fixed height. This is
  the part that answers "what will I have to fix?"
- **The fill grid** — 19 × N cells at 8×4px, virtualised, clickable at ≤150 rows and a
  minimap above that. This is the part that answers "how far along am I, and where are the
  holes?"

The grid stays the memorable thing; the profile makes it useful when it matters most.
A plain text readout (`142/200 · ~3 min`) sits beside them, because a picture of progress
is not a substitute for a number.

## 3.3 The token system

Four named roles plus one four-step ramp — the deepest step of the ramp *is* the ink,
which is what makes the encoding feel like one material rather than a palette.

```css
:root {
  /* ground — cool, greyed, deliberately not the storefront's warm #F5F1EA */
  --ground:  #F1F3F6;
  --resist:  #FFFFFF;   /* editable cell only. Never a card, never a panel. */

  /* the vat: one dye, four depths. Also the type ramp. */
  --dye-1:   #AFB9CE;   /* undyed  — fills only, never text */
  --dye-2:   #5A6E9B;   /* one dip — mono labels ≥13px only */
  --dye-3:   #2C4372;   /* two dips */
  --dye-4:   #16203C;   /* full depth. This is the ink. */

  --madder:  #96261F;   /* failures. Nothing else, ever. */
  --brass:   #7A5A12;   /* needs a human. Nothing else, ever. */

  --rule:    #D8DDE6;   /* 1px separation, derived not decided */
}
```

Two colours are **reserved words**: if madder appears on anything that isn't a failure, or
brass on anything that isn't awaiting a human, the encoding is broken. Worth a lint rule.

**No dark mode.** §9 permits it only if done well. This palette is a dye ramp on undyed
cloth; inverted it becomes four blues on black, the depth metaphor reverses, and the
measured contrasts all have to be redone. A bad dark mode is worse than none.

## 3.4 Typography

| Role | Face | Why this one |
|---|---|---|
| Display | **Bricolage Grotesque** (variable, one axis in use) | Genuine character in the proportions without being a craft-marketplace serif. Wordmark and screen titles only. |
| Data | **JetBrains Mono** | True tabular figures, and it holds up at 12px where Martian Mono gets too wide for 19 columns. Every URL, slug, HS code, price, dimension and status. |
| Body | **Public Sans** | High x-height, humanist, and *not* Inter — Inter is the default smell this brief is trying to avoid. |

Scale, with small text doing the heavy lifting: `11 / 12 / 13 / 15 / 18 / 24 / 34`. The
review table lives at 13px mono; the fill-grid legend at 11px.

All three subset to Latin, woff2, ~150 KB total, self-hosted in `web/public/fonts/`. No
CDN — §6 requires this to work with the wifi off.

## 3.5 Layout — "the vat and the bench"

Left rail is the vat (what's being made); main area is the bench (what you're doing).

### Compose

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ haat-lister                                    Compose    Jobs    Health      │
├──────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  Product links                                          52 pasted            │
│  ┌──────────────────────────────────────────────┐       48 unique            │
│  │ https://yourshop.com/products/indigo-stole   │        3 duplicates        │
│  │ https://yourshop.com/products/kalamkari-scarf│        1 not a link        │
│  │ not-a-url                              ← fix │                            │
│  │ ▌                                            │                            │
│  └──────────────────────────────────────────────┘                            │
│  One per line. Drop a .txt or .csv, or paste a spreadsheet column.           │
│                                                                              │
│  Who made this content?                                          required    │
│  ○ I made or own it    ○ I have permission    ○ Neither                      │
│  Photos and product copy belong to whoever made them, and haat's seller      │
│  rules turn on this. It's a fact only you know, so this tool won't guess.    │
│                                                                              │
│  manifest mode · price left blank · descriptions as written        Change ▾  │
│                                                                              │
│                                                    [ Start processing ]      │
└──────────────────────────────────────────────────────────────────────────────┘
```

No rail here: nothing is being made yet, so the vat would be an empty box pretending to
be furniture.

### Running

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ haat-lister                                    Compose    Jobs    Health      │
├───────────────────┬──────────────────────────────────────────────────────────┤
│ j_7fk2m9qa        │  Processing                                              │
│ own · manifest    │  142 of 200 · about 3 min left            [ Cancel ]     │
│                   │                                                          │
│ ███ ███ ██▌ ░░░ ▓ │  ┌────────────────────────────────────────────────────┐  │
│ ███ ███ ██▌ ░░░ ▓ │  │ ███ ███ ██▌ ██▌ ░░░ ░░░ ▓▓▓ ██▌ ██▌ ██▌ ░░░ ███ … │  │
│ ███ ███ ██▌ ░░░ ▓ │  │ ttl dsc cat sub cus pri hsc wgt len wid hgt avl … │  │
│ ███ ███ ██▌ ░░░ ▓ │  └────────────────────────────────────────────────────┘  │
│ ███ ███ ██▌ ░░░ ▓ │  price_inr 0% · hs_code 100% suggested · sizes 12%       │
│ ▓▓▓ ██▌ ░░░ ░░░ ░ │                                                          │
│ ░░░ ░░░ ░░░ ░░░ ░ │  142 written    38 ⚑ need a human    4 ✕ failed          │
│ ░░░ ░░░ ░░░ ░░░ ░ │  direct 118 · local 24 · hosted 0 · host calls 0         │
│                   │  ──────────────────────────────────────────────────────  │
│ 142/200           │  ✓ 141  yourshop.com/products/indigo-stole      direct   │
│ the fill grid     │  ✕ 140  yourshop.com/products/gone-away        http_404  │
│                   │  ⠋ 143  yourshop.com/products/kalamkari    image:tier1   │
│                   │  ⠋ 144  yourshop.com/products/block-print    fetching    │
└───────────────────┴──────────────────────────────────────────────────────────┘
```

The rail is 200px: 19 columns × 8px + gaps. The grid is the whole reason for the rail's
width, which is how a layout earns its proportions instead of picking them.

### Review

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ ← j_7fk2m9qa   Review · 38 rows need a human      [ Re-export · 12 edits ]   │
├──────────────────────────────────────────────────────────────────────────────┤
│ ⚑ needs a price (38)  ⚑ hs code (200)  ⚑ category (6)  ✕ failed (4)          │
├──────┬───────────────────────┬──────────┬─────────┬──────────┬───────────────┤
│      │ title                 │ category │ price   │ hs_code  │ gi_region     │
├──────┼───────────────────────┼──────────┼─────────┼──────────┼───────────────┤
│ 003  │ Indigo cotton stole   │ apparel· │ ▁▁▁▁▁ ⚑ │ 6214  ·  │ ▨ locked      │
│ 004  │ Kalamkari scarf       │ apparel· │ ▁▁▁▁▁ ⚑ │ 6214  ·  │ ▨ locked      │
│ 005  │ Brass jhumka          │ ~more-cr │ ▁▁▁▁▁ ⚑ │  –    –  │ ▨ locked      │
└──────┴───────────────────────┴──────────┴─────────┴──────────┴───────────────┘
  ↑↓ move · enter edit · esc cancel · shift-click select · ⌘↵ apply to selection
```

Editable cells sit on resist-white; extracted cells sit on the ground. `gi_region` renders
as a hatched, disabled cell with a one-line reason — visibly *locked*, not merely absent,
because a blank cell invites someone to fill it in.

## 3.6 Motion

One moment: a cell **dyes in** over 120ms — opacity plus a one-step depth ramp — staggered
by *arrival*, not by index, so the animation is a truthful picture of concurrency rather
than a decorative wave. Everything else is under 200ms and functional.

`prefers-reduced-motion`: cells appear at final depth instantly, nothing else moves,
including the spinner glyphs in the row stream, which become static `·`.

## 3.7 Copy

Operator's side of the screen, per §9. "Product links", not "URL input". "Needs a price",
not "null field". One word per concept through the whole flow: **Start processing →
Processing → Processed**. Errors name what happened and what to do next, in that order,
and never apologise.

---

# Part 4 — What I need from you

1. **F1** — approve the ledger-as-source-of-truth resolution of §2.1 vs §2.3, or tell me
   you'd rather have simple completion-order-then-sort.
2. **F3** — should the SSRF allowlist ship empty (and `demo/urls.txt` carries an
   instruction), or ship with `127.0.0.1` so the demo runs out of the box?
3. **Phase 9 → Phase 1** for `netguard.py` — I'd rather the guard exist before the HTTP
   surface does. Your call.
4. **The design plan** — in particular C5, where I've split the signature element into a
   column profile plus the fill grid, and C1, where indigo is banned from the chrome.
5. Still open from v1, unchanged: the made-to-order availability string, the 14 `derived:
   true` slugs, and an authoritative HS-code list.
