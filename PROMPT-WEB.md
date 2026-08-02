# MASTER PROMPT — `haat-lister` v2
## Part A: batch CSV output · Part B: local web console

> **How to use:** save as `PROMPT-WEB.md` in the existing `haat-lister` repo, then send Claude Code:
> *"Read PROMPT-WEB.md. Start with Phase 0 — read the existing codebase, then give me the plan and the design plan only. No code yet."*
>
> This document **extends** the original `PROMPT.md`. Everything in it still holds: the Tier-1-first image gate, the provenance gate, the header-locked 19-column CSV, `gi_region` never inferred, `price_inr` blank by default. Where this document is silent, the original governs. Where they appear to conflict, stop and ask.

---

## §0 — WHAT'S CHANGING AND WHAT ISN'T

You already have a working CLI agent. Two additions:

**A. Batch → one file.** Give it 10, 50, or 500 URLs and it produces **one** `listings.csv` — one row per product, in input order, ready to upload. (Most of this exists; §2 pins the parts that async batching gets wrong.)

**B. A local web console.** A browser UI at `http://127.0.0.1:8000`: paste URLs, press one button, watch it work, download the CSV.

**What must not change:**
- `images/pipeline.py` and `images/validator.py` — the Tier 1 → Tier 2 gate is untouched. The web layer *displays* it, never bypasses it.
- The 19-column header lock.
- The provenance requirement. In the CLI it's a required flag; in the UI it's a required control the operator cannot skip.

**Architecture rule:** the web app is a *thin* layer over the existing package. The API imports and calls the same Python functions the CLI calls — it never shells out to the CLI, and it never reimplements pipeline logic. If a behaviour is needed by both, it belongs in the core package, not in the API. Any logic you find yourself writing twice is a refactor signal: pull it down into the core.

---

# PART A — THE CSV OUTPUT REQUIREMENT

## §1 — Job model

Every run — CLI or web — is a **job** with a short id (`j_` + 8 chars, URL-safe). All artifacts live together:

```
runs/j_7fk2m9qa/
├── listings.csv          ← the import file. This is the deliverable.
├── review.csv            ← the human worklist
├── image_manifest.csv    ← row → image file mapping
├── failed.csv            ← URLs that produced nothing, with reasons
├── images/<row_key>/01.jpg …
├── job.json              ← settings, counts, timings, tool version
└── haat-listings-j_7fk2m9qa.zip   ← built on demand, everything above
```

`job.json` records the exact settings used. Six weeks later, "why is this CSV different" has an answer.

## §2 — The five things async batching gets wrong

1. **Row order is input order, not completion order.** With concurrency 5, URL #7 often finishes before #3. The writer must reassemble by input index. An operator who pasted a list in a considered order expects to get it back that way. Test this with deliberately jittered completion times.

2. **Duplicate input URLs are collapsed before processing, not after.** Canonicalise (strip `utm_*`, `gclid`, `fbclid`, `ref`, fragments; lowercase host; sort remaining query params), then dedupe. Report the collapse in the preflight: "52 URLs pasted → 48 unique." Never silently drop; always show the count.

3. **The file is written incrementally, never held in memory.** Append each completed row, flush, `os.replace` the temp file. Two reasons: a 500-URL job that dies at #480 must not lose 480 rows, and the UI's "download what's done so far" needs a valid file mid-run.

4. **Every input URL is accounted for in exactly one place.** Enforce it as an assertion at job end:
   ```
   len(listings) + len(failed) == len(unique_inputs)
   ```
   A URL that silently vanishes is the worst failure mode this tool has, because nobody notices until a product is missing from the shop.

5. **The three files have distinct jobs — don't blur them.**
   - `listings.csv` — every row that produced a usable title. Includes `needs_review` rows (blank `price_inr` is *expected*, not a failure). This is what gets uploaded.
   - `review.csv` — every row needing a human, **including rows present in listings.csv**. Not a subset by exclusion; a worklist by inclusion.
   - `failed.csv` — `source_url`, `reason`, `stage`, `http_status`, `attempted_at`. Re-runnable: its URL column can be pasted straight back into a new job.

## §3 — Per-URL, the agent extracts (unchanged, restated for the UI's benefit)

Title · description · validated image(s) · category and subcategory tags · plus the remaining haat fields per the original §4 confidence tiers. The UI shows those four as the visible per-row result because they're what the operator recognises; the review file carries the rest.

## §4 — Review-and-fix, then re-export

New capability, and the reason the web UI earns its keep. `price_inr` is blank by default (it's a business decision, not a scrape), `hs_code` is always flagged, subcategories are sometimes ambiguous. Editing 50 blanks in Excel and re-importing is miserable; editing them in a focused table is not.

- `PATCH /api/jobs/{id}/rows/{row_key}` accepts field edits.
- Edits are validated against the same rules as extraction: slug must exist in `taxonomy.yaml`, `price_inr` must be a positive integer, `availability` must be in the enum, `sizes` must be comma-separated-no-spaces.
- **`gi_region` stays uneditable in the UI too.** Same reasoning as the CLI: a GI tag is a government certification and this tool is not the place to assert one. The field renders disabled with a one-line explanation and a link to haat's compliance page.
- Edited fields get `source="human"`, `confidence="high"`, and a timestamp in the ledger.
- Re-export regenerates `listings.csv` from the ledger. Original extraction values are preserved alongside the edit — never destructively overwritten.

## §5 — Partial and resumed jobs

Cancel mid-run → job goes to `cancelled`, completed rows remain downloadable, and a **Resume** action processes only the remainder. Stop-the-world cancellation that discards 200 finished rows is a bug, not a safety feature.

---

# PART B — THE LOCAL WEB CONSOLE

## §6 — Stack and how it runs

```
Backend    FastAPI + uvicorn, in-process job runner (asyncio task + a bounded queue)
Streaming  Server-Sent Events for live progress. Polling fallback if SSE drops.
Frontend   Vite + React + TypeScript, built to web/dist/, served statically by FastAPI
State      TanStack Query for server state; local state stays local. No Redux.
Styling    Plain CSS with custom properties, or vanilla-extract. NOT a component
           library. NOT default-config Tailwind — see §9 on why the look matters.
Fonts      Self-hosted woff2 in web/public/fonts/. No CDN, no Google Fonts <link>:
           this tool must work with the wifi off.
```

SSE rather than WebSockets: the traffic is one-directional server→client, SSE reconnects on its own, and it survives a laptop sleeping mid-job. Don't reach for WebSockets without a reason.

**One command:**

```bash
haat-lister serve            # → builds if needed, serves 127.0.0.1:8000, opens the browser
haat-lister serve --port 8080 --no-open
```

`serve` is a subcommand of the existing CLI, not a separate program. Ship the built `web/dist/` so a user who doesn't have Node can still run it; `--dev` proxies to the Vite dev server for you.

## §7 — API contract

| Method | Route | Does |
|---|---|---|
| `POST` | `/api/jobs` | body: `{urls[], settings{}}` → `{job_id, accepted, duplicates_removed, invalid[]}` |
| `POST` | `/api/jobs/preflight` | dry validation: parse, dedupe, group by domain, robots check, time estimate. **No fetching of product pages.** |
| `GET` | `/api/jobs/{id}` | full state: settings, counts, per-row summaries |
| `GET` | `/api/jobs/{id}/events` | **SSE.** `row_started`, `row_stage`, `row_done`, `row_failed`, `job_progress`, `job_done`, `job_error` |
| `POST` | `/api/jobs/{id}/cancel` · `/resume` | |
| `GET` | `/api/jobs/{id}/rows` | paginated table data + confidence + flags |
| `PATCH` | `/api/jobs/{id}/rows/{row_key}` | §4 edits |
| `POST` | `/api/jobs/{id}/export` | regenerate listings.csv from ledger |
| `GET` | `/api/jobs/{id}/download/{artifact}` | `listings\|review\|manifest\|failed\|zip`, streamed |
| `GET` | `/api/jobs` | history, newest first |
| `GET` | `/api/config` | taxonomy, enums, available image hosts, whether `--llm` is configured |
| `GET` | `/api/health` | config-check as JSON — surfaces incomplete `taxonomy.yaml` |

`row_stage` events carry the stage the row is in, so the UI can show real work rather than a fake spinner: `fetching` → `extracting` → `image:tier1` → `image:download` → `image:upload` → `enriching` → `written`. Include `image_tier` (`direct`/`hosted`/`local`/`none`) on `row_done` — Rule 1's ratio should be visible while it happens, not just in the summary.

**Every SSE event and API response is derived from the ledger, not from in-memory state.** Refreshing the page mid-job must reconstruct the exact view. Job id belongs in the URL (`/jobs/j_7fk2m9qa`) so a refresh, a bookmark, or a second tab all work.

## §8 — Screens and states

The mockup's flow is the spine — paste, one button, progress, download. Everything else is progressive disclosure. Do not bury the primary path under configuration.

**1 · Compose.** Big textarea, one URL per line. Also accepts drag-and-drop of `.txt` / `.csv` and paste from a spreadsheet column. Live counter distinguishing total / valid / duplicate / malformed as they type — inline, not a modal. Malformed lines get marked in place so the operator can fix them without leaving.

**2 · Settings**, collapsed by default, sensible defaults visible in one summary line ("manifest mode · price left blank · descriptions as written"). Contains: **provenance (required, no default, the run cannot start without it)**, image mode, price strategy, description mode, default category, RFQ/bulk defaults, seller note, concurrency, LLM assist toggle. Provenance gets a one-sentence plain explanation of why it's being asked — an unexplained required field feels like bureaucracy; an explained one feels like care.

**3 · Preflight**, after the button, before work starts. "48 unique URLs across 6 domains. Two domains disallow scraping in robots.txt — 5 URLs will be skipped. Estimated 4–6 minutes." Then a confirm. This screen is where a mistake costs seconds instead of ten minutes.

**4 · Running.** Overall progress, elapsed, ETA, cancel. Per-row live list with current stage, and an image-tier badge on completion. Rows stream in as they finish. Failures appear inline in red immediately — never hidden until the end. Live counters for direct vs hosted.

**5 · Complete.** Summary — processed, ready, needs review, failed, direct/hosted split, host calls made, time taken. Primary action: **Download listings.csv**. Secondary: review.csv, image_manifest.csv, failed.csv, the zip. If anything needs review, the button to the review table is prominent and honest: "38 rows need a price before upload."

**6 · Review table.** Dense, keyboard-navigable grid. Filter by flag type. Inline edit per §4. Bulk-apply to a selection (one price, one category, one availability across 20 rows). `gi_region` visibly locked. Re-export button shows how many edits are pending.

**7 · History.** Past jobs, newest first, with counts and re-download links.

**Empty, error, and edge states are designed, not defaulted.** Empty compose is an invitation with one real example URL. A job with zero successes explains the most common cause (usually robots or a blocked domain) rather than shrugging. A dead backend shows "The agent isn't running" with the command to start it. Errors state what happened and the next action; they don't apologise and they aren't vague.

## §9 — DESIGN DIRECTION

Work as the design lead at a studio whose work is never mistaken for anyone else's. Run the full two-pass process: brainstorm a token system (4–6 named hex values, 2–3 typefaces with defined roles, a layout concept with ASCII wireframes, and one signature element), then **critique that plan against the generic default before writing any code** and tell me what you changed and why. Show me the design plan before you build.

### The subject, pinned

This is **not the haat storefront.** The storefront is a boutique, cream and calm, selling one beautiful object at a time. This is the **workbench behind it** — an operator tool where one person turns 200 messy web pages into 200 disciplined rows before dinner. Its audience is a catalogue operator, not a shopper. Its single job: make the state of 200 rows legible at a glance and make the broken ones easy to find. Design for density, legibility, and confidence — not for browsing pleasure. A tool that looks like the shop it feeds would be a category error.

### Anti-defaults — do not ship these

Named because a brief mentioning an Indian craft marketplace pulls hard toward all three:

1. **Cream `#F4F1EA`-ish background + high-contrast serif display + terracotta accent near `#D97757`.** This is the single most likely output and it is a tell. It's also literally haat's storefront palette (their theme-color is `#F5F1EA`) — see the subject note above.
2. Near-black canvas with one acid-green or vermilion accent. Dashboard-by-default.
3. Broadsheet layout: hairline rules, zero radius, dense newspaper columns.

Also avoid, specifically for this brief: mandala/paisley ornament, block-print borders as decoration, saffron-white-green anything, and "Namaste 🙏" copy. Cultural signalling as texture is decoration, not design — and this is a tool, not a souvenir.

### A direction worth considering — indigo, by depth

Offered as a strong starting point, not a mandate. If your two-pass produces something better fitted, propose it with reasoning and I'll take it.

Indigo is genuinely of this world — Indian dye, and the sample catalogue rows literally say *indigo*, *natural-dye*, *kalamkari*. More usefully, **indigo dyeing works by repeated dips: the more dips, the deeper the blue.** That is a direct visual analogue for the thing this tool's data model is actually about — *confidence*. High-confidence extracted fields render deepest; suggested fields sit mid-vat; blank-and-flagged fields are the pale undyed ground. The palette isn't a mood, it's an encoding — which is what §"structure is information" asks for.

- Ground: undyed cotton, a cool off-white with a hint of grey — deliberately *not* the storefront's warm cream.
- Ink: a deep indigo, near-black at full depth, for text and the deepest confidence tier.
- Two to three intermediate indigo steps for the confidence ramp.
- One resist-white for the ground of editable cells (the resist paste in a bandhani or dabu print — an intentional absence).
- Madder red, used *only* for failures. Never for accent, never for emphasis.
- Brass ochre, used *only* for "needs a human" — echoing the brass GI mark haat puts next to a price, which is exactly the "a human certified this" semantic.

**Colour never carries meaning alone.** Every confidence level and status needs a glyph, weight, or fill pattern too — for accessibility, and because operators will print and screenshot these screens.

### Typography

Three roles, three faces, all self-hosted:

- **Display** — used with restraint, for the wordmark and screen titles. Something with a point of view. Candidates worth trying: Bricolage Grotesque, Archivo Expanded, Instrument Serif (if you can justify a serif against the anti-defaults), Redaction.
- **Data/mono** — this is the workhorse and the one to get right. Every URL, slug, HS code, price, dimension, and status is monospace. Tabular figures are non-negotiable: a column of prices that doesn't align vertically is a defect in a CSV tool. Candidates: JetBrains Mono, Martian Mono, IBM Plex Mono.
- **Body/UI** — quiet, humanist, high x-height for small sizes. Inter Tight, Public Sans, Source Sans 3.

Set a real type scale. Small text will be doing heavy lifting; make sure it holds up at 12–13px.

### The signature element

Spend your boldness in exactly one place, then keep everything else disciplined.

A candidate that's true to the content: **the fill grid** — a live miniature matrix, 19 columns wide (one per CSV column) by N rows, that fills cell by cell as the job runs, each cell shaded by confidence depth. It is literally a picture of the CSV assembling itself. It replaces the generic progress bar with something that shows *what kind* of progress is happening — you can see at a glance that the `price_inr` column is a pale stripe and the `title` column is solid, which is exactly the thing the operator needs to know before they open the review table. Clicking a cell jumps to that row's field.

If you find something better, argue for it. But it should be one thing, it should be honest about the data, and it should not be an animated gradient.

### Motion

One orchestrated moment, not scattered effects. The fill grid populating is the obvious candidate. Everything else: fast, functional, under 200ms. Respect `prefers-reduced-motion` fully — with it on, the grid fills without transition and nothing else moves.

### Quality floor, built in without announcing it

Keyboard navigable end to end (the review table especially — arrow keys, tab, enter to edit, escape to cancel). Visible focus rings. AA contrast minimum. Works down to a 1024px laptop; degrades gracefully narrower, though this is a desktop tool and shouldn't pretend otherwise. No layout shift when rows stream in. Dark mode only if you can do it *well* — a bad dark mode is worse than none, and this palette may not want one.

### Copy

Written from the operator's side of the screen. Name things by what the person controls, never by how the system is built: "Product links," not "URL input array." "Needs a price," not "field null." Active voice, sentence case, plain verbs. The button that says **Start processing** produces a state that says **Processing** and a result that says **Processed** — same word through the whole flow. Let each element do one job.

---

## §10 — SECURITY (this tool holds API keys and fetches arbitrary URLs)

1. **Bind `127.0.0.1` by default.** `--host 0.0.0.0` must require `--token` and print a clear warning. No CORS wildcard.
2. **SSRF guard on the scraping fetcher.** Users paste arbitrary URLs. Resolve and reject private ranges, loopback, link-local, and cloud metadata endpoints (`169.254.169.254`) — re-check after every redirect hop, not just on the input, or the guard is decorative.
3. **Path traversal.** Download routes accept a job id matching `^j_[a-z0-9]{8}$` and an artifact from a fixed allowlist. Never a client-supplied path.
4. **Upload caps.** URL-list file ≤ 2 MB, ≤ 10,000 lines. Reject with a clear message, not a 500.
5. **Never return secrets.** `/api/config` reports *whether* a host is configured, never the key. Redact in logs and in error responses.
6. **One job at a time by default.** A queue, not unbounded parallel jobs — the rate limiter is per-domain and three concurrent jobs would quietly triple the load on someone's server.

---

## §11 — STRUCTURE

```
haat_lister/
├── … (existing core, unchanged)
├── api/
│   ├── app.py            # FastAPI, static mount, SSE
│   ├── routes/           # jobs, rows, downloads, config
│   ├── schemas.py        # request/response models, distinct from core models
│   ├── runner.py         # job queue, cancellation, progress → ledger
│   └── events.py         # SSE broadcaster
└── web/
    ├── src/
    │   ├── routes/       # Compose · Job · Review · History
    │   ├── components/   # FillGrid, RowStream, ConfidenceCell, StageChip, …
    │   ├── hooks/        # useJobEvents (SSE + reconnect + polling fallback)
    │   ├── styles/       # tokens.css — the §9 token system lives here, once
    │   └── api/          # typed client
    ├── public/fonts/
    └── dist/             # built, committed, served
```

`tokens.css` is the single source of colour and type. If a hex appears anywhere else in the codebase, that's a bug.

---

## §12 — PHASES

| Phase | Deliverable | Done when |
|---|---|---|
| 0 | Read existing code. Plan + **design plan with the two-pass critique** | I approve both |
| 1 | Part A: input-order writer, dedupe, incremental flush, accounting assertion, `runs/<job_id>/` layout, `failed.csv` | 200-URL CLI job produces correctly ordered files; ordering test passes |
| 2 | FastAPI skeleton, `serve` command, health/config routes, static serving | `haat-lister serve` opens a page that reports config status |
| 3 | Job create + preflight + runner + SSE + cancel/resume | a job runs from `curl`, events stream, cancel preserves rows |
| 4 | Design tokens, shell, Compose screen, settings, preflight | one URL runs end to end in the browser |
| 5 | Running screen, row stream, **fill grid**, tier badges | 50-URL job is watchable and honest |
| 6 | Complete screen, downloads, zip | listings.csv downloads and imports into haat |
| 7 | Review table, inline edit, bulk apply, re-export | 38 blank prices filled and re-exported without touching Excel |
| 8 | History, empty/error states, keyboard pass, reduced-motion, a11y audit | full keyboard run, no contrast failures |
| 9 | Security pass (§10), tests, README with screenshots | fresh clone → `serve` → working job in under 5 minutes |

Take screenshots as you build and critique your own work against the design plan. A picture is worth a thousand tokens.

---

## §13 — TESTS

Carry over every test from the original spec — `test_tier1_pass_prevents_download_and_upload` and `test_manifest_mode_never_calls_any_host` especially. The web layer must not weaken them. Add:

1. `test_output_order_matches_input_order` — jittered completion times, order preserved.
2. `test_every_input_url_accounted_for` — the §2.4 assertion, including on a cancelled job.
3. `test_duplicate_urls_collapsed_and_reported`.
4. `test_partial_download_mid_job_is_valid_csv`.
5. `test_cancel_preserves_completed_rows` / `test_resume_processes_only_remainder`.
6. `test_page_refresh_reconstructs_job_state` — kill and rebuild the client view from the ledger.
7. `test_sse_reconnect_no_duplicate_rows`.
8. `test_row_patch_validates_against_taxonomy`.
9. `test_gi_region_patch_rejected` — the API refuses it even if the UI is bypassed.
10. `test_reexport_preserves_original_extraction_values`.
11. `test_ssrf_blocks_metadata_endpoint_after_redirect`.
12. `test_download_path_traversal_rejected`.
13. `test_config_endpoint_never_returns_keys`.
14. Playwright E2E: paste 3 URLs → provenance → start → watch → download → verify the header is byte-identical to the template.
15. Playwright a11y: keyboard-only path through compose → run → review → export.

---

## §14 — DEFINITION OF DONE

- [ ] `haat-lister serve` → browser → paste 10 URLs → one click → watch → download a valid `listings.csv`. No terminal required after the first command.
- [ ] Header byte-identical to `haat-bulk-listings-template.csv`; rows in input order; every input URL in exactly one output file.
- [ ] The Tier-1-first gate is untouched and its ratio is visible live in the UI.
- [ ] `gi_region` is empty in every row and rejected by the API.
- [ ] The review table clears 38 flagged prices faster than Excel would.
- [ ] Refresh mid-job loses nothing. Cancel loses nothing.
- [ ] Full keyboard operation; AA contrast; reduced motion respected.
- [ ] The design plan's signature element is present and is the one memorable thing on screen.
- [ ] Nothing in the UI reads as a default: no cream-and-terracotta, no stock dashboard, no component-library smell.
- [ ] Works with the wifi off (except, obviously, the scraping itself).
