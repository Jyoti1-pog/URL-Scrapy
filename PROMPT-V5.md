# MASTER PROMPT — `haat-lister` v5
## Fix pass: honest failure taxonomy · row accounting · the routes that actually work

> Extends `PROMPT.md`, `PROMPT-WEB.md`, `PROMPT-FIXES.md` and `PROMPT-V4.md`. All still govern. §7 lists what must not move.

---

## §0 — WHERE THE BUILD IS

Observed on run `j_f0x6hw7v`:

```
j_f0x6hw7v · authorised · manifest · 19 columns · 3/3
0 written · 3 need a human · 3 failed
21s · direct 0 · local 0 · hosted 0 · 0 image-host calls
listings.csv 0 rows · review.csv 3 rows · failed.csv 3 rows

1  nykaafashion.com/…  page_fetch_failed  ⚑10
2  ajio.com/…          robots_disallowed  ⚑10
3  myntra.com/…        page_fetch_failed  ⚑10
```

**Read the run correctly before fixing it.** Rows 1 and 3 never returned a response. Row 2 was refused by robots.txt and the tool stopped, which is the tool working. Zero of these three is a parser defect. What the run exposes is that the product **reports** three very different outcomes as one undifferentiated "failed," double-counts every row, and offers a retry that cannot succeed.

---

## §1 — PHASE 0: ROW ACCOUNTING

Three input URLs produced **six** output rows.

### 1.1 The rule

| State | Meaning | File | Counts as |
|---|---|---|---|
| `written` | Row extracted, all required fields present | `listings.csv` | written |
| `needs_human` | Row extracted, some field needs a decision | `listings.csv` **and** `review.csv` | needs a human |
| `refused` | The site declined access before any content arrived | `failed.csv` | refused |
| `failed` | We reached the site but could not extract a usable row | `failed.csv` | failed |

`needs_human` is the one deliberate exception. `refused` and `failed` never appear in `review.csv`.

### 1.2 What that makes the run above

```
0 written · 0 need a human · 1 refused · 2 failed
```

Assert `written + needs_human + refused + failed == len(input_urls)` at job completion.

### 1.3 Kill the phantom review rows

When `review_rows == 0`, the banner does not render.

### 1.4 Kill the phantom flags

A row that never produced a record has no field-level flags — it has one reason. Suppress the flag count when `extracted_fields == 0`.

---

## §2 — FIX 2: ONE NAME PER OUTCOME

### 2.1 The closed vocabulary

**Refused** (the site declined; the tool behaved correctly):
`robots_disallowed` · `blocked_403` · `blocked_429` · `bot_challenge` · `sign_in_required`

**Failed** (we should have got content and did not):
`timeout_connect` · `timeout_read` · `dns_failure` · `http_error_5xx` · `not_a_product_page` · `no_extractable_content` · `no_image_candidates` · `all_candidates_rejected`

`page_fetch_failed` is deleted, not aliased.

### 2.2 Refused is not failed

- Job header: `0 written · 0 need a human · 1 refused · 2 failed`
- Retry excludes refused rows.
- `failed.csv` gets a `class` column (`refused` | `failed`).
- Find photos tabs: `all` · `has photo` · `low res` · `no photo` · `refused` · `failed`.

### 2.3 Give every reason a fix line

Each enum member carries a one-sentence `what_to_do` — the next action, not a description.

---

## §3 — FIX 3: `diagnose` MUST NOT REPORT CHECKS IT DID NOT RUN

### 3.1 Three states, not two

Every check renders `yes` / `no` / `— not reached`.

### 3.2 Show the attempt ledger

One line per attempt: transport, status or exception, elapsed.

### 3.3 Stage B says `off` — say why

`not attempted — stage A returned no response` / `disabled (--no-browser)` / `not needed (stage A complete)`.

---

## §4 — FIX 4: BUILD THE ROUTES THAT WORK

### 4.1 Import from a seller export (build first — highest yield)

`.csv` / `.xlsx` / `.tsv`. Column-mapper UI, fuzzy auto-map, saved `profiles/<name>.yaml` keyed by header signature. Unmapped columns shown, never silently discarded. Image URLs go through the identical Tier-1 chain.

### 4.2 Import from a saved page

`Ctrl+S` → "Webpage, complete" → drop the `.html`/folder/`.mhtml` here. Existing extractor, resolve relative images against `_files/`, zero network calls in manifest mode. `--from-file` on the CLI. `fetch_stage="saved_page"`.

### 4.3 Paste a page

The one-off version of 4.2.

### 4.4 Preflight must warn *before* the run

robots per URL at preflight; `domains.yaml` of previously-observed refusals — observed history, never a blocklist, never prevents a run.

---

## §5 — FIX 5: RETRY AND TIMEOUT BUDGET

- `--url-timeout` (default 20s) covering all attempts for one URL.
- Retry only `timeout_connect`, `timeout_read`, `http_error_5xx`, `blocked_429`. Honour `Retry-After`. Never retry a refused reason, `dns_failure`, or `not_a_product_page`.
- Per-domain circuit breaker after N consecutive refusals/timeouts (default 5).
- Report the budget spent: `21s · fetch 19.8s · parse 0.2s · idle 1.0s`.

---

## §6 — WHAT THIS PROMPT DELIBERATELY DOES NOT ASK FOR

**Do not build anything whose purpose is to make the tool harder to identify as a tool.** No UA rotation to impersonate a consumer browser, no proxy pools, no CAPTCHA solving, no fingerprint spoofing, no cookie replay, no timing tuned for evasion.

**`robots_disallowed` remains a hard stop.** `--ignore-robots` stays CLI-only and undocumented in the UI.

A retry budget is not evasion — `Retry-After` compliance is the difference.

---

## §7 — WHAT MUST NOT MOVE

- The Tier-1-first image gate and its two tests. Seller-export and saved-page rows use the identical chain.
- `--provenance` required with no default, on every ingestion route including imports.
- `gi_region` empty and API-rejected.
- The 19-column header byte-identical, in `listings.csv` and `master.csv`.
- Output order matches input order.
- Master dedupe on canonical `source_url`, surviving the export's URL format.
- The design language. New screens are siblings of Compose.

---

## §8 — TESTS

1. `test_every_url_in_exactly_one_terminal_state`
2. `test_counts_sum_to_input_length`
3. `test_refused_rows_absent_from_review_csv`
4. `test_no_field_flags_on_empty_record`
5. `test_reason_strings_are_enum_members`
6. `test_retry_excludes_refused_class`
7. `test_diagnose_marks_downstream_checks_not_reached`
8. `test_url_timeout_budget_enforced`
9. `test_circuit_breaker_opens_after_n_refusals`
10. `test_retry_after_header_respected`
11. `test_seller_export_maps_to_19_columns`
12. `test_saved_page_extraction_makes_zero_network_calls`
13. `test_saved_page_and_live_fetch_produce_identical_row`
14. `test_import_requires_provenance`
15. `test_master_dedupes_scraped_and_exported_same_product`
16. `test_domains_yaml_never_blocks_a_run`
17. Playwright E2E: import a seller export → map → save profile → run → download; re-import and assert the profile auto-applies.

---

## §9 — DEFINITION OF DONE

- [ ] `j_f0x6hw7v` re-run reads `0 written · 0 need a human · 1 refused · 2 failed`, counts sum to 3.
- [ ] `review.csv` empty for that run; banner does not render.
- [ ] `page_fetch_failed` appears nowhere in the codebase.
- [ ] Job row, `diagnose`, and Find photos use the same reason string for the same event.
- [ ] `diagnose` on myntra shows `— not reached` for all three post-fetch checks, and prints the attempt ledger.
- [ ] "Retry the N that failed" excludes the ajio row.
- [ ] Every reason renders a `what_to_do` that links to a route the operator can take.
- [ ] Preflight flags a known-refusing domain before the run, with the import button inline.
- [ ] A seller export produces a valid `listings.csv` with no product page fetched.
- [ ] A saved `.html` produces the same row as a live fetch, zero network calls in manifest mode.
- [ ] Nothing added whose purpose is evading a site's refusal (§6).
