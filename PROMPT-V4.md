# MASTER PROMPT — `haat-lister` v4
## Fix pass: page_fetch_failed · bulk photo finder · exports that carry image links

> Extends `PROMPT.md`, `PROMPT-WEB.md`, `PROMPT-FIXES.md`. All still govern. §8 lists what must not move.

---

## §0 — EVIDENCE FROM THE LAST RUN

Job `j_pzegz95i`, four links across four shops:

```
1  myntra.com/dresses/marks+%26+spencer/…       → page_fetch_failed   ⚑10
2  nykaafashion.com/twenty-dresses-by-…          → page_fetch_failed   ⚑10
3  amazon.in/…/dp/B0CKQ13ZYR                     → direct              ⚑5
4  flipkart.com/apple-earpods-usb-c-wired/…      → robots_disallowed   ⚑10

listings.csv 1 row · failed.csv 3 rows · image_manifest.csv 6 rows
```

Diagnose on #1:

```
robots.txt              allowed
stage A                 http_error — <StreamReset stream_id:3, error_code:2, remote_reset:True>
looks like a product page   no — nothing on it says product
bot check               no
sign-in wall            no
stage B                 off
```

Diagnose on #3 (working, but with a contradiction):

```
stage A                 200 · text/html · 2.3 MB · 2011ms
looks like a product page   a purchase control ('add to cart'), something priced
bot check               no
sign-in wall            no
stage B                 not attempted (stage A was enough)
body: 'currently unavailable'          ← flagged, on a page that has a live buy box
→ direct  https://m.media-amazon.com/images/I/51jeYk-gFbL.jpg
```

---

## §1 — THE ORDER TO FIX THINGS

1. **`page_fetch_failed` on Myntra and Nykaa** — §2.
2. **The false "currently unavailable" flag** — §3.
3. **Flipkart / robots_disallowed** — §4.
4. **Bulk photo finder** — §5.
5. **Exports that actually contain image links** — §6.

---

## §2 — FIX: `page_fetch_failed` IS A TRANSPORT FAILURE, NOT A PARSE FAILURE

### 2.1 What actually happened

`StreamReset error_code:2` is HTTP/2 `INTERNAL_ERROR`, sent by the server after the connection was established. No HTML ever arrived, which is why "looks like a product page: no" and "bot check: no" both reported negative: **there was nothing to inspect.** Those two lines are currently misleading, because absence of evidence is being printed as evidence of absence.

Two independent defects made this fatal:

- **No retry ladder.** One attempt, HTTP/2, one header set, then give up.
- **`stage B: off`.** A transport error is the single strongest signal that the browser is needed, and it is precisely the case where the fallback didn't fire.

### 2.2 The fetch ladder

| Rung | Attempt | Exists because |
|---|---|---|
| A1 | HTTP/2, full browser header set | fastest path, works on most sites |
| A2 | **HTTP/1.1**, same headers | the direct answer to `StreamReset` |
| A3 | HTTP/1.1, fresh connection, `Connection: close`, one retry after 2s jittered backoff | transient resets and pooled-connection staleness |
| B | **Playwright**, real browser, wait for the gallery selector, scroll once | the answer when the site wants a browser and means it |
| ✗ | fail with a *specific* reason | never a generic `page_fetch_failed` again |

Rung A2 alone may fix both URLs. Try it manually before writing the rest.

### 2.3 Header set

```
User-Agent          a current real browser UA, single value in config, contactable in README
Accept              text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8
Accept-Language     en-IN,en;q=0.9
Accept-Encoding     gzip, deflate, br
Sec-Fetch-Dest      document
Sec-Fetch-Mode      navigate
Sec-Fetch-Site      none
Sec-Fetch-User      ?1
Upgrade-Insecure-Requests  1
```

This is a browser being honest about being a browser, not a disguise. No fingerprint spoofing, no TLS-signature mimicry, no proxy rotation, no captcha solving.

### 2.4 Escalation rules

- **Any transport error escalates to Rung B.**
- HTTP 403/429 with no body escalates to Rung B once, then fails as `blocked_by_source`.
- Zero image candidates escalates to Rung B.
- A soft 404 or a genuine 404 does **not** escalate.
- Rung B is skipped when Playwright isn't installed; the reason says so and tells the operator to run `playwright install chromium`.

### 2.5 Reason enum — replace `page_fetch_failed`

```
transport_reset          stream reset / connection reset
tls_error                handshake or certificate failure
dns_error                host does not resolve
timeout_connect / timeout_read
http_4xx / http_5xx      with the status recorded
blocked_by_source        captcha, challenge, or a hard block after all rungs
robots_disallowed        we chose not to fetch
browser_unavailable      Rung B needed but Playwright is not installed
```

Every reason carries the rungs attempted. `failed.csv` gets a `rungs_tried` column.

### 2.6 Per-domain fetch profile, learned and persisted

When a domain succeeds on a rung other than A1, persist that in the ledger and start there next time. Visible and clearable: `haat-lister profiles --list` / `--clear <domain>`.

### 2.7 Diagnose honesty

When no HTML was retrieved, the page-shape lines must read `not evaluated — no page content` rather than `no`.

---

## §3 — FIX: THE FALSE "currently unavailable" FLAG

- **Scope the soft-404 check to the main product node**, not `body`.
- **Make it conditional, not additive:** if a price *and* a purchase control were found, an availability marker elsewhere is ignored entirely.
- If the marker is inside the buy box, that's a real signal — set `availability` accordingly, don't fail the row.
- Diagnose should print *where* a marker matched (`buy-box` / `elsewhere on page`).
- Add an internal consistency assertion that logs a warning when two page-shape findings disagree.

---

## §4 — FLIPKART: `robots_disallowed` IS CORRECT — MAKE THE UI USEFUL

> This shop's robots.txt asks crawlers to stay off product pages, so nothing was fetched. If these are your own products, the usual routes are your seller-panel export, the shop's official API or affiliate feed, or entering these few by hand.

Do **not** add a one-click robots override to the UI.

---

## §5 — NEW: BULK PHOTO FINDER

### 5.1 What it is

A screen at `/find` — rename the nav item from **Why no photo?** to **Find photos**, keeping the single-URL diagnose as its detail view. It does not write listings, does not touch `master.csv`, and **never contacts an image host**.

### 5.2 Input — same parser, more shapes

Reuse `utils/urls.py::extract_urls`. No second implementation.

- Textarea taking comma, newline, semicolon, tab, or mixed pastes.
- **File upload:** `.csv`, `.txt`, `.tsv`. Auto-detect the URL column, show which was chosen, let the operator pick another. Carry the other columns through to the output.
- Same live counter and parsed-link preview as Compose.
- Cap consistent with Compose (10,000 lines / 2 MB).

### 5.3 Output — a table, one row per product

| Column | Notes |
|---|---|
| # | input order |
| Product | title, cleaned; original on hover |
| Images | thumbnail strip, count, hero marked |
| Primary image URL | monospace, one-click copy |
| All image URLs | expandable, pipe-separated on copy |
| Resolution | of the hero |
| Method | `direct` · `direct_low_res` · `local` · `none` |
| Reason | the §2.5 enum when not `direct` |
| Price found | source amount + currency, unconverted |
| Details | description snippet, category guess, dimensions/weight |
| Why | link to single-URL diagnose for that row |

Rows stream in as they resolve. Filter chips: `has photo` / `no photo` / `low res` / `failed`.

### 5.4 It shares the pipeline

Identical code paths. No CSV write, no downloads unless `--with-files`, no host calls ever, results cached in the ledger so a subsequent real job reuses them.

---

## §6 — NEW: EXPORTS THAT CARRY IMAGE LINKS

### 6.1 Why the import file can't

haat's template is 19 columns and none of them is an image. The image-bearing file is a **companion**, not a replacement.

### 6.2 Artifacts

```
runs/j_xxxx/
├── listings.csv                 19 columns, header-locked.   → imports into haat
├── listings_with_images.csv     19 columns + image columns.
├── image_links.csv              from Find photos
└── review.csv / failed.csv / image_manifest.csv / images/ / everything.zip
runs/master.csv                  accumulating, 19 columns
runs/master_with_images.csv      accumulating, with image columns
```

`listings_with_images.csv` = the 19 columns in exact order, then appended:

```
source_url, image_url, image_1 … image_N, image_count, image_method,
image_reason, image_width, image_height, local_image_path
```

`N` from `--max-images` (default 6). Empty trailing columns stay present so the header is stable across runs.

### 6.3 Rules

- Both files written from the same ledger rows in the same order; a test asserts row-for-row correspondence on `source_url`.
- Download panel labels them for what they do:
  - **listings.csv** — upload this to haat
  - **listings_with_images.csv** — same rows plus every photo link, for your own records
- CSV-injection guard, UTF-8, atomic write. `--excel-bom` applies to both.
- Master gets the same pair, deduped identically.

---

## §7 — HOUSEKEEPING (the three startup warnings)

1. **14 subcategory slugs marked `derived`.** One test import of a 14-row CSV settles all of them.
2. **Made-to-order availability value unknown.** Get the real enum from the seller dashboard.
3. **HS code map has 2 entries.** Grow from the actual catalogue mix; keep confidence capped at medium.

Surface all three in `/api/health` and on the Sheet screen.

---

## §8 — WHAT MUST NOT MOVE

- The Tier 1 → Tier 2 gate, predicate order, and `0 image-host calls` in `manifest` mode. Find photos must never make a host call.
- The 19-column header lock on `listings.csv` and `master.csv`.
- `gi_region` unwritable by extractor, plugin, API, and UI.
- `price_inr` blank by default.
- robots.txt respected by default; no UI override; no evasion beyond honest browser headers and a real browser (§2.3).
- One implementation each of URL parsing, canonicalisation, fetching, and CSV writing.

---

## §9 — PHASES

| Phase | Deliverable | Done when |
|---|---|---|
| 0 | Manual check: `curl --http1.1` against the two failing URLs | you know whether A2 alone fixes it — report before building |
| 1 | Fetch ladder A1→A2→A3→B, header set, escalation rules, reason enum | Myntra and Nykaa resolve; diagnose names the winning rung |
| 2 | Diagnose honesty (§2.7) + per-domain fetch profiles | a second Myntra URL starts at A2 |
| 3 | Scoped page-shape checks + consistency assertion (§3) | the Amazon page no longer flags `currently unavailable` |
| 4 | Flipkart copy + failed.csv guidance (§4) | the row explains the alternatives |
| 5 | `listings_with_images.csv` + `master_with_images.csv` (§6) | a run produces both, row-for-row identical on source_url |
| 6 | Find photos: parser, upload, column detection, table, streaming | 20 mixed links produce a full table |
| 7 | `image_links.csv` download + ledger cache reuse | a job after a find re-fetches nothing |
| 8 | Housekeeping surfacing (§7) + README update | `/api/health` shows all three warnings |

---

## §10 — TESTS

1. `test_http2_stream_reset_falls_back_to_http1`
2. `test_transport_error_escalates_to_browser`
3. `test_browser_unavailable_reports_clearly`
4. `test_no_generic_page_fetch_failed_remains`
5. `test_page_shape_not_evaluated_without_html`
6. `test_fetch_profile_persisted_and_reused`
7. `test_soft404_ignored_when_buybox_present`
8. `test_soft404_honoured_inside_buybox`
9. `test_page_shape_contradiction_logs_warning`
10. `test_find_photos_makes_zero_host_calls`
11. `test_csv_upload_column_autodetect`
12. `test_find_photos_accepts_comma_and_newline_and_tab_mixed`
13. `test_listings_and_with_images_row_correspondence`
14. `test_with_images_header_stable_across_runs`
15. `test_listings_csv_still_19_columns`
16. `test_find_cache_reused_by_subsequent_job`
17. Existing gate tests stay green.

---

## §11 — DEFINITION OF DONE

- [~] The four URLs from §0 re-run: **Myntra and Nykaa do not resolve and cannot without evasion** (see PHASE 0 RESULT); they now fail with a specific transport cause and the full climb. Amazon still resolves; Flipkart declines with a useful explanation that distinguishes "their rules say no" from "their bot wall would not show us the rules".
- [x] No result anywhere says `page_fetch_failed`; every failure names a specific transport or policy cause and the rungs it tried
- [x] Diagnose never reports a check it didn't run
- [x] The Amazon page stops flagging `currently unavailable`
- [x] Find photos takes 20 comma-separated links or an uploaded CSV and returns every image URL plus details, with zero host calls
- [x] `listings_with_images.csv` downloads with all photo links; `listings.csv` is still exactly 19 columns
- [x] `master_with_images.csv` accumulates alongside `master.csv`, deduped identically
- [x] Both gate tests still green; `gi_region` still empty; robots still respected by default

---

## PHASE 0 RESULT — measured 2026-08-07, before any code was written

**§2.2's premise is wrong for these two hosts, and §11's first line cannot be met without evasion.** Full findings and their consequences are in the response that accompanied this run; the short version:

| Configuration | Myntra | Nykaa |
|---|---|---|
| h2 + current headers (today) | StreamReset 0.7s | StreamReset 1.0s |
| **h1.1 + current headers (rung A2)** | **ReadTimeout 21s** | **ReadTimeout 21s** |
| h2 + §2.3 browser headers | StreamReset 1.2s | StreamReset 0.9s |
| h1.1 + §2.3 browser headers | ReadTimeout 21s | ReadTimeout 21s |
| **real headless Chromium (rung B)** | **ERR_HTTP2_PROTOCOL_ERROR** | **ERR_HTTP2_PROTOCOL_ERROR** |

TLS 1.3 completes on both; DNS resolves; both sit behind Akamai. The refusal is
post-handshake and applies to a real browser too, so it is a bot-management
decision about this client, not a protocol or header problem. Rung A2 makes it
*worse* (21s black hole rather than a 0.7s reset), and Rung B does not fix it.

Getting past it would require TLS-fingerprint mimicry or proxy rotation, which
§8 forbids. The honest outcome for these two hosts is `blocked_by_source` with
the seller-export remedy — the same answer Flipkart gets.

Everything else in v4 is unaffected and worth building: the ladder still helps
other sites, and the reason enum, diagnose honesty, profiles, scoped page-shape
checks, the `_with_images` companions and Find photos all stand.
