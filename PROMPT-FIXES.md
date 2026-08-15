# MASTER PROMPT — `haat-lister` v3
## Fix pass: flexible URL input · one accumulating sheet · image extraction that reports why

> **How to use:** save as `PROMPT-FIXES.md` in the repo and send Claude Code:
> *"Read PROMPT-FIXES.md. Start with Phase 0 — build `diagnose` and show me its output on the Amazon URL in §4.1 before changing anything else."*
>
> Extends `PROMPT.md` and `PROMPT-WEB.md`. Both still govern. In particular §5 below lists what must not move.

---

## §0 — WHERE THE BUILD IS

v0.1.0 runs. `haat-lister serve` binds 127.0.0.1:8000, the Compose screen accepts links, jobs run, and `listings.csv` downloads. Observed on a real run:

```
j_wyu7imi9 · authorised · manifest
1 written · 1 need a human · 0 failed
7s · direct 0 · local 0 · hosted 0 · no photo 1 · 0 image-host calls
row 1: "Mivi DuoPods Marathon Earbuds Wireless | Fast Charge | 70H Playtime |
        BT v5.3 | 13mm Drivers | Noise Cancella…"   image: none   flags: 4
```

Three defects and one gap, in the order they should be fixed.

---

## §1 — WHAT'S WRONG

**Defect 1 — the failure is silent.** `no photo 1` and `image: none` tell the operator nothing actionable. Were there zero candidates? Did five candidates each fail predicate 6? Was the page a captcha wall? Right now the only way to know is to read the code. **This is the first fix, because every other diagnosis depends on it.**

**Defect 2 — the image never resolved on a page that certainly has one.** Amazon product pages carry images; the extractor found none it would accept. Causes are enumerated in §4.

**Defect 3 — input parsing is too narrow.** The Compose box takes one URL per line. Operators have links in a spreadsheet cell, a WhatsApp message, or a comma-separated export. Splitting on newlines alone means a comma-separated paste is read as one malformed line.

**Gap — output is per-job, not cumulative.** Each job writes its own `listings.csv`. Running ten jobs over a week leaves ten files to merge by hand. The operator wants *one sheet that fills up*.

Also fix, while in the area:
- Titles keep marketplace SEO tails: `Product Name | Feature | Feature | Feature`. Collapse to the real name.
- Copy bug in the results banner: "1 row need a human" → count-aware ("1 row needs" / "38 rows need").

---

## §2 — FIX 1: ACCEPT LINKS IN WHATEVER SHAPE THEY ARRIVE

### 2.1 Find URLs, don't split on delimiters

Splitting on commas is the obvious approach and it is wrong: commas are legal in URLs and appear in real query strings and path segments. Splitting first and validating after will silently cut those links in half.

**Extract instead of split.** Scan the whole blob for URL-shaped tokens:

1. Normalise whitespace and unicode; convert non-breaking spaces; strip zero-width characters (spreadsheet pastes are full of these).
2. Find every `https?://…` run, terminating at whitespace, a delimiter that cannot appear mid-URL in this context, or end of input.
3. Strip wrapping and trailing punctuation that is almost never part of a URL: matched `<>`, `""`, `''`, `()`, `[]`, and trailing `,` `;` `.` — but only when unmatched, so `…/dp/B0FT?a=(1)` survives.
4. Unwrap markdown links `[text](url)` and HTML `href="…"` — people paste from docs and emails.
5. Accept bare-domain lines (`amazon.in/dp/B0FT…`) by prepending `https://`, and flag them as assumed.
6. Everything left over that isn't a URL goes to `unparsed[]` and is shown back to the operator verbatim — never dropped silently.

Delimiters that must all work, including mixed in one paste: newline, comma, semicolon, tab, pipe, and runs of spaces.

Put this in `utils/urls.py::extract_urls(blob) -> (urls, unparsed)` as a pure function with the §8 test table. It is used identically by the CLI file reader, the API, and the UI preview — one implementation, three callers.

### 2.2 Canonicalise before deduping

Existing rules stay (strip `utm_*`, `gclid`, `fbclid`, `ref`, fragments; lowercase host; sort remaining params). Add **per-domain canonical forms** in the plugin system, because tracking-laden marketplace URLs are the common case:

- Amazon: `…/dp/{ASIN}` or `…/gp/product/{ASIN}` → `https://{host}/dp/{ASIN}`. The pasted example carries `_encoding`, `pd_rd_w`, `content-id`, `pf_rd_p`, `pf_rd_r`, `pd_rd_wg`, `pd_rd_r`, `ref_`, `th` — every one is disposable, and two links to the same ASIN with different tracking must dedupe to one row.
- Flipkart: keep `pid`, drop `lid`/`marketplace`/`srno`/`otracker`/`fm`/`iid`/`ppt`/`ssid`.
- Etsy: `/listing/{id}/…` → `/listing/{id}`.
- Shopify: drop `?variant=` only when `--merge-variants` is set (variants are often genuinely separate listings — do not assume).

Canonicalisation is per-domain config, not a hardcoded chain. Record both `source_url` (canonical) and `original_url` (as pasted) in the ledger; the review file shows the original so the operator recognises their own link.

### 2.3 UI: show the parse before the run

The Compose counter panel (`pasted / unique / duplicate / not a link`) is good. Extend it:

- Below the textarea, a collapsed **"Show the 48 links I found"** list — canonical form, with the original on hover, duplicates grouped and struck through. Operators need to see that a comma paste split the way they expected *before* spending ten minutes.
- `not a link` becomes clickable, revealing the unparsed fragments so a typo is fixable in place.
- Bare domains show an "assumed https" marker.
- Live parsing stays debounced and off the main thread for large pastes; 5,000 links must not freeze the box.

---

## §3 — FIX 2: ONE SHEET THAT FILLS UP

### 3.1 Within a job — confirm, don't rebuild

One job already produces one `listings.csv` with one row per product. That is correct and stays. Verify with a 25-URL run that input order holds and every URL lands in exactly one output file (the `PROMPT-WEB.md` §2 assertion). If that already passes, this section costs you a test run, not a refactor.

### 3.2 Across jobs — the master sheet

New, and the thing actually being asked for.

```
runs/master.csv          ← accumulates every job. The operator's real working file.
runs/j_xxxx/listings.csv ← per-job, unchanged, still the audit trail
```

Rules:

- **Append on job completion** when `--master` is on (default: **on** for the web console, off for CLI unless passed — the web operator's mental model is a filling sheet; a scripted CLI user usually wants isolation).
- **Same 19-column header, byte-identical.** Master is a valid haat import file at all times, not a superset with extra bookkeeping columns.
- **Dedupe on canonical `source_url` across the whole file.** A URL already in master does not append twice. Default on a repeat: skip and report. `--on-duplicate {skip,replace,append}` covers the other intents; `replace` updates the existing row in place and preserves its position.
- **Order is append order**, not per-job order, so the sheet reads as a history.
- **Never rewrite master from a partial job.** Append only on `completed` or explicit "add these rows to master" from a cancelled job's review screen.
- Atomic: write `master.csv.tmp`, `os.replace`. If master is open in Excel and locked on Windows, catch it and say so — "master.csv is open in another program, close it and press retry" — not a stack trace.
- `haat-lister master --stats` prints row count, date range, jobs merged, duplicates skipped.

### 3.3 UI

- **Jobs** nav gets a sibling: **Sheet**. It shows master.csv — row count, last updated, a table preview, Download, and "Open the folder."
- The Complete screen's primary action stays **Download listings.csv**, with a secondary line: "Also added 24 rows to your sheet — now 312 rows." Say what happened; don't make them go looking.
- If a job's rows were skipped as duplicates, say so plainly there: "6 rows were already in your sheet."

### 3.4 Optional, only if you want it

A `--sheets` export writing master to a Google Sheet via a service account, credentials in `.env`, off by default and cleanly absent when unconfigured. Do this last, and only after §4 is finished — it's convenience, not a fix.

---

## §4 — FIX 3: IMAGES — DIAGNOSE FIRST, THEN REPAIR

### 4.1 Build `diagnose` before touching the extractor

```bash
haat-lister diagnose "https://www.amazon.in/Mivi-Marathon-Playtime-Wireless-Bluetooth/dp/B0FTFMNYBV/"
```

Prints, with no CSV written and no host contacted:

```
FETCH
  stage A   200  text/html  412 KB  1.2s
  robots    allowed
  page      looks like a product page? yes | captcha wall? no | login wall? no
  stage B   not attempted (stage A sufficient)

TITLE        "Mivi DuoPods Marathon Earbuds Wireless | Fast Charge | 70H …"  [og:title]
             cleaned → "Mivi DuoPods Marathon Wireless Earbuds"

IMAGE CANDIDATES  (found 0)
  jsonld Product.image        ✗  no Product node in JSON-LD
  og:image                    ✗  absent
  twitter:image               ✗  absent
  srcset                      ✗  0 gallery <img> with srcset
  lazy attributes             ✗  0
  background-image            ✗  0
  ⚠  page contains 14 <img> under m.media-amazon.com not matched by any rule

RESULT  image: none  reason: no_candidates_extracted
```

And when candidates exist, one line per candidate per predicate, stopping where it stopped:

```
  [1] https://m.media-amazon.com/images/I/61abc._AC_SX679_.jpg
      1 syntax ok · 2 reachable 200 · 3 redirects 0 · 4 image/jpeg
      5 size 48 KB ok · 6 dimensions 679×679 ✗ FAIL below_min_dimensions (need 800×800)
```

That output turns "why no photo" from an investigation into a glance. Expose it in the UI too: every row with `image: none` gets a **"Why no photo?"** link opening this report. Add `GET /api/diagnose?url=…`.

**Run it on the Amazon URL and show me the output before writing any extractor changes.** The fixes below are the likely causes; the report tells you which are real.

### 4.2 Likely cause A — Amazon hides images from generic extraction

Amazon does not put the gallery in plain `<img src>`. Add `extract/plugins/amazon.py` (the plugin system already exists — this is its first real customer) reading, in order:

1. `#landingImage[data-a-dynamic-image]` — a JSON map of `{url: [width, height]}`. Parse it, take the largest by area. This is the single highest-yield rule.
2. `#landingImage[data-old-hires]` — often the full-resolution original.
3. The inline `ImageBlockATF` script: `colorImages.initial[]`, each entry carrying `hiRes`, `large`, `thumb`, and a `variant` label. Take `hiRes` where present, `large` otherwise, in listed order — this is how you get the *whole gallery* rather than the hero alone.
4. `#imgTagWrapperId img`, `#altImages img` as a last resort.

**Then normalise the Amazon size modifier**, which matters more than it looks:

```
https://m.media-amazon.com/images/I/61abcDEF._AC_SX679_.jpg
                                            └── strip this segment ──┘
→ https://m.media-amazon.com/images/I/61abcDEF.jpg          (original)
→ https://m.media-amazon.com/images/I/61abcDEF._SL1600_.jpg (explicit large)
```

Add the stripped original as a **new higher-ranked candidate while keeping the modified one** — never replace blindly, per the existing rule. A candidate rejected at 679×679 usually passes comfortably once the modifier is gone. There's a strong chance this alone fixes the reported symptom.

### 4.3 Likely cause B — a 200 that isn't the product page

Amazon serves "Robot Check" / "Enter the characters you see below" pages with a 200 status. A parser sees success and no images. Add a **page-shape check** to `fetch/` that runs on every response, not just Amazon's:

- captcha markers: `/errors/validateCaptcha`, "Enter the characters you see below", "Robot Check", Cloudflare challenge markers, `cf-browser-verification`
- login/consent walls: a sign-in form dominating the body, `/ap/signin` redirects, cookie-consent interstitials with no product content
- soft 404s: 200 with "not found" / "no longer available" / "currently unavailable" and no price node

Each maps to a distinct row reason — `blocked_by_source`, `login_required`, `product_unavailable` — surfaced in `failed.csv`, in `review.csv`, and in the UI. **A blocked page must never present as "extracted successfully, no photo."** That conflation is what made this bug invisible.

Correct behaviour on a block: fail the row loudly with a plain-language explanation. Do not escalate — no captcha solving, no proxy rotation, no fingerprint spoofing. The remedy offered to the operator is to supply their own export or use their seller account's data, not to fight the site.

### 4.4 Likely cause C — Stage B isn't triggering when it should

Confirm the Stage A → Stage B condition includes **zero image candidates**, not only missing title or description. A page with a title and description but no images is exactly the case that needs the browser. From the screenshot's 7-second runtime, Stage B likely never ran.

Also: when Stage B does run, wait for the gallery specifically (a selector wait, not just `networkidle`), and scroll once to trigger lazy loading before parsing.

### 4.5 Likely cause D — the 800×800 floor is dropping usable photos

The floor is right for a premium marketplace, but "reject everything, ship nothing" is the wrong endgame. Change the failure mode, not the standard:

- If candidates fail **only** on dimensions, take the largest available, mark `image_method="direct_low_res"` (or `local_low_res`), set `status=needs_review`, and flag `image_below_standard` with the actual dimensions in `review.csv`.
- Keep the hard floor at a genuinely unusable size (default 400×400) below which the answer is still none.
- Make both thresholds config, both surfaced in `diagnose` output.

An operator with a 679×679 photo and a flag can decide; an operator with nothing cannot.

### 4.6 Never-silent rule

Every row ending at `image: none` carries a specific reason from a closed enum — `no_candidates_extracted`, `blocked_by_source`, `all_candidates_failed_validation`, `download_failed`, `host_upload_failed`, `robots_disallowed` — and that reason appears in `review.csv` and on the row in the UI. `none` with an empty reason is a bug, and there should be a test asserting the reason is non-empty whenever the method is `none`.

### 4.7 Title cleaning

The observed title kept the full pipe-delimited SEO tail. Add to `extract/title.py`:

- If the title splits on ` | ` or ` - ` into 3+ segments and the first is ≥ 3 words, keep the first segment and treat the rest as attributes.
- Drop trailing marketing tails: "Free Shipping", "Best Price", "Buy Online", "(Pack of N)" only when a separate quantity field captured it, brand-store suffixes.
- Mine the discarded segments for real attributes (`70H Playtime`, `BT v5.3`, `13mm Drivers`) and offer them to the description rewriter rather than throwing them away.
- Always keep the original in the review file so a bad clean is visible.

---

## §5 — WHAT MUST NOT MOVE

This is a fix pass on the edges of a pipeline whose core is correct. Do not:

- Change the Tier 1 → Tier 2 gate, or the predicate order, or make Tier 2 reachable in `manifest` mode. The screenshot's `0 image-host calls` is the gate working; keep it working. `test_tier1_pass_prevents_download_and_upload` and `test_manifest_mode_never_calls_any_host` must stay green throughout.
- Weaken the 19-column header lock, in `listings.csv` or `master.csv`.
- Make `gi_region` writable, by extractor, plugin, API, or UI.
- Add a default to `price_inr`. The screenshot's "need a human" row is correct behaviour, not a defect.
- Add anti-bot evasion of any kind (§4.3).
- Introduce a second implementation of URL parsing, canonicalisation, or CSV writing. One each, shared by CLI and API.

---

## §6 — ONE THING TO SETTLE BEFORE PHASE 3

The test URL is a third-party Amazon listing for a mass-market electronics product. haat's seller rules prohibit resold and dropshipped goods and require products made in India by the seller, and the product photos on that page belong to whoever shot them. If Mivi is your own brand and this is your own listing, the run is a straightforward migration and `authorised` is the right provenance. If it's someone else's listing, the tool is working correctly but the resulting listing wouldn't survive haat's own review.

Nothing to build here — the provenance gate already handles it. But add one line to the README under a "What this tool is for" heading, so the next operator understands the constraint before they paste 500 links, rather than after.

---

## §7 — PHASES

| Phase | Deliverable | Done when |
|---|---|---|
| 0 | `diagnose` command + `/api/diagnose` | run on the §4.1 Amazon URL; **show me the output before changing the extractor** |
| 1 | `utils/urls.py::extract_urls` + per-domain canonicalisation + the §8 table | comma, newline, semicolon, tab, mixed, and markdown pastes all parse |
| 2 | Compose UI: parsed-link preview, clickable `not a link`, assumed-https marker | a comma-separated paste of 12 links shows 12 before the run |
| 3 | Page-shape check (§4.3) + reason enum (§4.6) + UI surfacing + "Why no photo?" | a blocked page reports `blocked_by_source`, never "no photo" |
| 4 | Amazon plugin (§4.2) + size-modifier normalisation | the §4.1 URL yields a validated image; `diagnose` shows which rule won |
| 5 | Stage B trigger fix + gallery wait + scroll (§4.4) | a JS-only gallery resolves; Stage B % appears in the summary |
| 6 | Low-res tier (§4.5) + title cleaning (§4.7) + banner copy fix | 679×679 ships flagged rather than vanishing |
| 7 | `master.csv`: append, dedupe, `--on-duplicate`, atomic write, file-lock message | three jobs accumulate into one 19-column sheet with no duplicates |
| 8 | Sheet screen + Complete-screen line + `master --stats` | operator sees the sheet fill without leaving the browser |
| 9 | Optional Google Sheets export | absent and silent when unconfigured |

---

## §8 — TESTS

`extract_urls` table — one test each, all must pass:

| Input | Expect |
|---|---|
| `a.com/x\nb.com/y` | 2 |
| `https://a.com/x, https://b.com/y` | 2 |
| `https://a.com/x,https://b.com/y` | 2 (no space) |
| `https://a.com/x; https://b.com/y` | 2 |
| tab-separated pair | 2 |
| `https://a.com/p?ids=1,2,3` | **1** — comma inside the URL survives |
| `https://a.com/x, https://b.com/p?ids=4,5` | **2** — both behaviours in one blob |
| `<https://a.com/x>` / `"https://a.com/x"` | 1, wrappers stripped |
| `[Product](https://a.com/x)` | 1 |
| `Check this out: https://a.com/x.` | 1, trailing period dropped |
| `amazon.in/dp/B0FT` | 1, assumed-https flag set |
| `hello world` | 0 urls, 1 unparsed |
| 5,000 mixed lines | parses under 200 ms |

Plus:

1. `test_amazon_canonicalises_to_asin` — the full tracking URL from §2.2 → `https://www.amazon.in/dp/B0FTFMNYBV`.
2. `test_two_tracking_urls_same_asin_dedupe_to_one_row`.
3. `test_amazon_dynamic_image_json_parsed` — fixture HTML, largest-by-area chosen.
4. `test_amazon_size_modifier_stripped_and_ranked_above_original`.
5. `test_captcha_page_reports_blocked_not_no_photo` — 200 + Robot Check fixture.
6. `test_image_none_always_has_reason` — property test across all failure paths.
7. `test_stage_b_triggers_on_zero_image_candidates`.
8. `test_low_res_image_flagged_not_dropped` — 679×679 → shipped, `needs_review`, dimensions in review.csv.
9. `test_title_seo_tail_stripped_original_retained`.
10. `test_master_append_dedupes_across_jobs`.
11. `test_master_header_byte_identical_to_template`.
12. `test_master_not_written_from_incomplete_job`.
13. `test_master_locked_file_reports_clearly` — simulate the Windows lock.
14. `test_on_duplicate_replace_preserves_row_position`.
15. E2E: paste 6 comma-separated links → run → listings.csv has 6 ordered rows → master grows by 6 → re-running the same paste grows master by 0.

Every existing test stays green. If a change to the pipeline requires editing an image-gate test, stop and ask first.

---

## §9 — DEFINITION OF DONE

- [x] `diagnose` explains any image outcome in one screen, and the UI links to it from every `none` row
- [x] The §4.1 Amazon URL produces a validated image, or an explicit `blocked_by_source` with a plain-language explanation — never a silent `none`
- [x] A comma-separated paste of 12 links produces 12 rows in one CSV, in order
- [x] Commas inside URLs survive parsing
- [x] Three jobs accumulate into one `master.csv`, deduped, 19 columns, byte-identical header
- [x] The results banner counts correctly ("1 row needs" / "38 rows need")
- [x] Titles lose the SEO tail and keep the original in review.csv
- [x] `0 image-host calls` still holds in `manifest` mode; both gate tests green
- [x] `gi_region` still empty and still rejected by the API
- [x] README states what the tool is for, in one short paragraph


---

## STATUS — all ten phases done

Verified against the demo shop (including a new `/blocked/<n>` bot-check page
built for §4.3) and a purpose-built Amazon-shaped fixture. 682 tests, ruff and
mypy clean, tsc clean, axe clean on every screen.

Bugs the tests and live runs caught, worth recording because each was silent:

| Where | What |
|---|---|
| `canonical.py` | `drop_query: ["*"]` fired on Amazon SEARCH pages, deleting the search terms |
| `urls.py` | 5,000-link paste took 341ms against a 200ms budget |
| `urls.py` | `extract_urls` collapsed exact repeats, deleting the evidence before anything could count them |
| `urls.py` | canonicalisation percent-encoded the very commas §8 exists to preserve — including in image URLs we then requested |
| `amazon.py` | the size-modifier regex matched **nothing**; `_` was not in its character class |
| `amazon.py` | `/G/01/` was matched against a lowercased URL, so Amazon sprite sheets passed as product photos |
| `pipeline.py` | a blocked row carried Stage A's gap notes — "no weight found" on a captcha wall |
| `diagnose.py` | reported `direct` on a bot check, naming the captcha image as the product photo |

Still open, all needing haat rather than code:

- 14 `derived: true` subcategory slugs — one test import settles them
- `fields.availability_made_to_order_value` is unset
- the HS-code map has two entries
