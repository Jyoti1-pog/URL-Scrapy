# haat-lister — Phase 0 Plan (approval required before any code)

Status: **plan only**. No implementation files have been written.
Working from the `PROMPT.md` content supplied in-chat (the repo is empty — see §F1).

---

## A. File tree

Largely as specified in §3.2, with additions marked `(+)` and rationale given.

```
haat-lister/
├── pyproject.toml                 (+) packaging, deps, pytest/ruff config, console_script
├── README.md                      (+) §13 requirement
├── .env.example                   (+) every key documented, no values
├── .gitignore                     (+) .env, downloads/, images/, store/, *.tmp
├── config.yaml                       thresholds, timeouts, enums, host chain, FX
├── taxonomy.yaml                     seeded + marked incomplete (§4.2)
├── haat_lister/
│   ├── __init__.py
│   ├── cli.py                        Typer entrypoint. Arg parsing + wiring only.
│   ├── config.py                     pydantic-settings; config.yaml + .env merge
│   ├── models.py                     FieldValue[T], ProductRecord, ImageResult, enums
│   ├── pipeline.py                (+) per-URL orchestration: fetch→extract→enrich→
│   │                                  image→policy→write. Shared by single & batch.
│   ├── fetch/
│   │   ├── static.py                 Stage A (httpx)
│   │   └── rendered.py               Stage B (Playwright, lazy import)
│   ├── extract/
│   │   ├── structured.py             extruct: JSON-LD → microdata → RDFa → OG
│   │   ├── title.py
│   │   ├── description.py
│   │   ├── images.py                 candidate collection, normalise, rank
│   │   ├── price.py
│   │   ├── dimensions.py
│   │   ├── variants.py
│   │   └── plugins/
│   │       ├── __init__.py           registry: match(url) -> Plugin | None
│   │       └── example_shopify.py
│   ├── enrich/
│   │   ├── category.py
│   │   ├── hs_code.py
│   │   ├── fx.py
│   │   └── rewrite.py                --llm only
│   ├── images/
│   │   ├── validator.py              ★ the 9 predicates. Pure, sync-testable.
│   │   ├── downloader.py             Tier 2a
│   │   ├── optimiser.py              Tier 2b
│   │   ├── pipeline.py               ★ the Rule 1 gate. Only orchestrator.
│   │   └── hosts/
│   │       ├── base.py               ImageHost protocol, HostedImage
│   │       ├── cloudinary.py
│   │       ├── imgbb.py
│   │       └── imgur.py
│   ├── policy/
│   │   ├── screen.py
│   │   ├── keywords.yaml          (+) §2.5 says "configurable keyword set" — the
│   │   │                              antiquities/wildlife/weapons lists belong in
│   │   │                              config, not in source. brands.txt stays a
│   │   │                              flat list because operators will paste into it.
│   │   └── brands.txt
│   ├── output/
│   │   ├── csv_writer.py             ONLY module that knows column names (§2.7)
│   │   ├── review_writer.py
│   │   └── manifest_writer.py
│   ├── store/
│   │   └── ledger.py                 SQLite: rows, uploads, bad-host cache, fx cache
│   └── utils/
│       ├── robots.py
│       ├── ratelimit.py           (+) per-domain semaphore + jittered delay
│       ├── urls.py                (+) canonicalisation, row_key derivation
│       ├── units.py               (+) lb/oz/kg→g, in/mm→cm  (§9 test 11 targets this)
│       ├── atomic.py              (+) .tmp + os.replace writer used by all 3 outputs
│       └── logging.py                structured JSON lines, credential redaction
└── tests/
    ├── conftest.py                   respx fixtures, fake hosts, sample HTML corpus
    ├── fixtures/                     saved HTML: shopify, woo, etsy-like, bare, JS-only
    ├── test_validator_predicates.py  §9.3 — one test per predicate, both directions
    ├── test_image_pipeline_gate.py   §9.1, 9.2, 9.4, 9.5, 9.6, 9.19  ← headline
    ├── test_csv_writer.py            §9.12, 9.13, 9.14
    ├── test_fields.py                §9.7, 9.8, 9.9, 9.10, 9.11
    ├── test_policy_provenance.py     §9.17, 9.18
    ├── test_fetch_stages.py          §9.16
    └── test_batch_resume.py          §9.15, 9.20
```

Two structural notes:

- **`haat_lister/pipeline.py` vs `haat_lister/images/pipeline.py`.** §3.2 only names the
  image one, but something has to sequence fetch→extract→enrich→write per URL, and putting
  it in `cli.py` violates "No logic" in §3.2. Batch mode then becomes `asyncio.gather` over
  the same per-URL coroutine, which is what makes `single` and `batch` provably identical.
- **`images/validator.py` is pure and synchronous-testable.** It takes an injected HTTP
  client, so every predicate test is a `respx` route with no event-loop ceremony. This is
  the module §12 says gets read and tweaked most; it should be the most boring file here.

---

## B. Dependencies

Runtime, from §3.1:

| Package | Use |
|---|---|
| `httpx[http2]` | async fetch, per-domain limits, HEAD/Range probes |
| `selectolax` | fast HTML parse (hot path) |
| `beautifulsoup4` + `lxml` | fallback where selectolax ergonomics hurt (§3.1 allows) |
| `extruct` | JSON-LD / microdata / RDFa / OG |
| `pillow` | decode, validate, convert, resize, strip EXIF |
| `pydantic` (v2), `pydantic-settings` | the data contract |
| `typer`, `rich` | CLI + summary tables |
| `tenacity` | retry/backoff with jitter |
| `python-slugify` | row keys, image folder names |
| `pyyaml` | config.yaml / taxonomy.yaml / keywords.yaml |
| `protego` | robots.txt (correct wildcard + `Crawl-delay` handling; stdlib `urllib.robotparser` gets modern robots files wrong) |

Optional extras (never imported at startup):

| Extra | Package | Gate |
|---|---|---|
| `[render]` | `playwright` | lazily imported inside `fetch/rendered.py` |
| `[llm]` | `anthropic` | only when `--llm` |
| `[hosts]` | `cloudinary` | only in `url_columns`/`both`; ImgBB and Imgur are plain httpx multipart, no SDK |

Dev: `pytest`, `pytest-asyncio`, `respx`, `ruff`, `mypy`.

**Interpreter.** §0 says 3.11+; this machine has 3.13.5. Fine, with one risk (§F5).

---

## C. The §6.1 predicate list (as I will implement it)

Signature: `validate_direct_url(url, *, client, cfg) -> ValidationResult(ok: bool, reason: str, meta: dict)`.
Evaluated **in order, short-circuiting on first failure.** `meta` carries content-type,
bytes seen, and decoded dimensions for the review file.

| # | Predicate | Passes when | Fail reason | Implementation note |
|---|---|---|---|---|
| 1 | Syntax | absolute `http`/`https`, parseable, non-empty host | `bad_syntax` | Pure string work. I do **not** do a separate DNS lookup here — see §F3. |
| 2 | Reachable | `HEAD` → 200; on 405/403/501 retry `GET` with `Range: bytes=0-2047` | `http_{code}` / `timeout` / `dns_error` | Connect/resolution failures surface here as `dns_error`. |
| 3 | Redirect sanity | ≤5 hops; final response is not HTML and not a login/consent interstitial | `redirect_to_html` | Manual redirect following so hop count and each `Location` are inspectable. |
| 4 | Content-Type | starts with `image/` | `wrong_content_type` | `text/html` here = block page. |
| 5 | Size floor | `Content-Length` ≥ `min_bytes` (10 KB) | `too_small` | **Missing/chunked `Content-Length` does not fail** — it defers to predicate 6, which counts real bytes. See §F4. |
| 6 | Decodable + big enough | magic bytes valid, Pillow opens, `width ≥ 800 and height ≥ 800` | `undecodable` / `below_min_dimensions` | Streams progressively into a `BytesIO`, stopping as soon as Pillow can read the header — typically a few KB, not the whole file. |
| 7 | Hotlink test | re-request with **no `Referer`**, neutral UA, brand-new client, no cookie jar → still 200 + `image/*` | `hotlink_blocked` | Second network call. Reaching it only after 1–6 pass is exactly why the order short-circuits — one hotlink probe per row, not one per candidate. `--no-hotlink-test` skips it and marks the row in review. |
| 8 | Not signed/expiring | URL contains none of `expires`, `Expires`, `X-Amz-Expires`, `X-Amz-Signature`, `Signature`, `Key-Pair-Id`, `token`, `sig=`, `st=`, `se=`, `/tmp/`, `/session/`, `/preview/` | `signed_or_expiring_url` | Token list lives in `config.yaml`. Matched against the full URL, case-sensitively for the mixed-case entries and case-insensitively for the rest, as written. |
| 9 | Host reputation cache | host not in persisted `known_bad_hotlink_hosts` | `host_known_to_block` | Cheapest check but placed **last as specified** so its cache is populated by real evidence rather than short-circuiting the evidence-gathering. It fires before any network call on subsequent rows only because... see §F7, where I argue it should move to position 1.5 and ask for a ruling. |

Cache population rule (§6.1 predicate 9, "after N confirmed failures"): only
`hotlink_blocked` and hard `http_403` outcomes increment a host's counter. Timeouts,
`too_small`, and `below_min_dimensions` never do — those are properties of one image,
not of a host, and letting them poison the cache would silently push whole CDNs onto
the expensive path.

**First candidate to pass all nine wins**; validation stops there.

---

## D. Field-confidence table (my reading of §4)

`FieldValue[T] = {value, confidence: high|medium|low|none, source: jsonld|og|microdata|
meta|h1|title|spec_table|variants|inferred|fx_converted|llm|policy_default|operator}`.
CSV writer emits `.value` only; review writer reads confidence + source.

| Column | Tier | Best case source | Confidence cap | Blank when |
|---|---|---|---|---|
| `title` | auto | `Product.name` → `og:title` → `<h1>` → `<title>`−suffix | high | never (missing title = row `failed`) |
| `description` | auto | `Product.description` → `og:description` → meta → main block | high (`raw`) / medium (`rewrite`, source=`llm`) | acceptable, flagged |
| `category_slug` | suggest+flag | keyword/embedding map → taxonomy.yaml | **medium** | never — falls back to `more-crafts` |
| `subcategory_slug` | suggest+flag | scoped to chosen parent | **medium** | unknown → blank + flag |
| `custom_category` | auto | free text | high | unless parent == `more-crafts` |
| `price_inr` | **never silent** | `offers.price`+`priceCurrency` → `og:price:*` → visible+symbol | **none by default** | **default `blank`** — source amount/currency/rate go to review |
| `hs_code` | suggest + **always** flag | category+material map | **medium, hard cap** | unmapped → blank + flag; appears in review even when populated |
| `weight_g` | auto if found | `Product.weight` / spec table | high (product) / **low** (shipping weight) | not found → blank + flag |
| `length_cm` `width_cm` `height_cm` | auto if found | spec table, L×W×H order | high (labelled) / **low** (order normalised) | not found → blank + flag; never inferred from category |
| `availability` | enum, config-driven | `offers.availability` → visible text | high (mapped) | unknown/out-of-stock → blank + flag, **never defaults to `stock`** |
| `stock_qty` | auto if found | `inventoryLevel` / visible count | high | vague copy → blank + flag; never defaults to 1 or 10 |
| `sizes` | auto if found | `hasVariant` / variant selectors | high | non-apparel → blank |
| `gi_region` | **never infer** | — | `none`, always | **always blank.** Not writable by the extractor at the type level (§F6). |
| `rfq_enabled` | policy default | `--rfq-default` | high | default blank; literal `yes`, never `true` |
| `rfq_min_qty` | policy default | `--rfq-min-qty` | high | blank unless `rfq_enabled == yes` |
| `bulk_only` | policy default | `--bulk-only` | high | default blank; literal `yes` |
| `seller_note` | auto, opt-in | `--seller-note` / `--seller-note-from-source` | high / medium | default blank |

Reading I want confirmed: **a blank cell is never a row failure.** The only hard row
failures are (a) an unrecognised category/subcategory slug (§4.2), (b) no extractable
title, (c) no extractable content at all (§5.1 Stage C). Everything else degrades to
`needs_review` with a populated review row.

---

## E. Phase-by-phase execution (§11, unchanged order)

I plan to follow §11 exactly, including Phase 3 before 7–8. One note: Phase 3's
`validate-only` needs predicate 9's ledger table, so a minimal `store/ledger.py` (schema
+ bad-host set only) lands in Phase 3 rather than Phase 9. The rest of the ledger —
resume state, upload dedupe — stays in Phase 9 as written.

---

## F. Assumptions and open questions

Numbered so you can answer by number. **F1, F2, F8 and F9 are the ones that change what
I build**; the rest I have a default for and will proceed on unless you say otherwise.

### F1. Template CSV — RESOLVED, and it contradicts §7 on quoting
The template is now at the repo root. Measured: 19 columns in exactly the §4 order, no
image column, **unquoted header**, **minimally quoted** data rows (only `description` and
`sizes` carry quotes, because they contain commas), CRLF throughout, trailing newline,
no BOM.

**The conflict:** §7 says write `QUOTE_ALL`. Doing so emits `"title","description",…`,
which is not byte-identical to the template's unquoted header — and §13 requires exactly
that byte-identity. The two instructions cannot both be satisfied.

**Resolved in favour of the template**, since §12 says the CSV template is the best
evidence about the importer. `config.yaml -> csv.quote_all` now defaults to `false`, the
header is written unquoted unconditionally, and `quote_all: true` remains available with
a comment saying it breaks header byte-identity. No safety is lost: CSV injection is
handled by the apostrophe guard, which quoting never addressed anyway.

Also confirmed by the samples: `gi_region` empty in both rows; `rfq_enabled` is the
literal `yes` with `rfq_min_qty` `50` alongside a blank `bulk_only`; `availability` is
`stock`; `sizes` is `S,M,L,XL` with no spaces; `apparel/womens-fashion` and
`jewellery/earrings` are real slug pairs.

Original note follows.

### F1-original. `haat-bulk-listings-template.csv` is not in the repo — I need the real file
§13 requires the output header to be **byte-identical** to the template, and §9.12 tests
exactly that. §4 gives me the 19 names in order, which is enough to write code, but not
enough to guarantee byte-identity: I can't see whether the template quotes its header,
whether it has a UTF-8 BOM, whether it ends `\r\n` or `\n`, or whether it uses
`QUOTE_ALL` on data rows (§7 tells me to write `QUOTE_ALL`, but if the template's own
sample rows are minimally quoted, matching it is the safer bet for the importer).

**What I need:** drop the file at the repo root. Until then I'll build against the §4
column list with `QUOTE_ALL`/`\r\n`/no-BOM, and Phase 4 will diff against the real file
the moment it exists.

Also: `PROMPT.md` itself isn't on disk. Please save your original file to the repo root
rather than having me retype it — a spec this load-bearing shouldn't be reproduced from
a transcription.

### F15. What the listing-creator screenshots settled (Phase 3)
The seller dashboard screenshots answered more than the taxonomy question:

- **haat ingests image FILES, not URLs.** The Photos panel reads "Up to 10… JPEG, PNG,
  WebP · up to 10 photos · 8 MB each" with a drop zone. This confirms §6.0's suspicion and
  makes `manifest` the correct default permanently; `url_columns` is almost certainly the
  wrong mode for haat. Config now carries `max_images_per_product: 10`, `max_file_mb: 8`,
  `accepted_formats: [jpeg, png, webp]`, and `keep_webp: true` — haat accepts WebP, so
  re-encoding it to JPEG would be a lossy step for nothing.
- **`hs_code`, `weight_g`, `length_cm`, `width_cm`, `height_cm` and `price_inr` are all
  marked required (\*).** §4 says a blank cell is legitimate, and for *our CSV* it still
  is — but a blank there blocks the seller from publishing. Recorded as
  `fields.required_by_haat` so the review writer treats them as must-fill rather than
  nice-to-have. This is the single biggest thing `review.csv` will be used for.
- **Availability is exactly two states**, radio buttons: "Ready stock" (with a required
  quantity) and "Made to order" (no stock limit). See F2.
- **GI is a seller-ticked checkbox** — "This product carries a GI tag". Reinforces the
  never-infer rule: it is a declaration, not an observation.
- **`seller_note` is the "Private note (only you see this)" field**, never shown to buyers.
- "Other — my craft isn't listed" prompts the seller to name their category and says
  "we'll group it under More crafts", confirming `more-crafts` + `custom_category`.

### F7. Predicate 9 ordering — RESOLVED: moved to the front
Operator chose to optimise. The cache check now runs third, before any network call.

**I applied the same reasoning to predicate 8** (signed/expiring URL), which is also a
pure string test and was also specified after the network. Evaluation order is now:

    1 syntax → 8 signed → 9 host cache → 2 reachable → 3 redirects → 4 content-type
    → 5 size floor → 6 decode+dimensions → 7 hotlink

Predicate *numbers* are unchanged, so `reason` strings and `ValidationResult.predicate`
still map back to the spec. `tier1_attempted` is still True on every row.

### F2. `availability` — narrowed, one value still needed
The screenshots show exactly two states, so the vocabulary is no longer open-ended. The
wire value for "Ready stock" is confirmed as `stock` by the template. The wire value for
"Made to order" is not visible in the UI, and a guessed enum would simply be rejected at
import — so made-to-order products get a blank `availability` plus a review flag until
someone reads the real value off an existing made-to-order listing. One line in
`config.yaml` closes this.

### F3. Predicate 1's "host resolvable"
Doing a real DNS lookup in predicate 1 means resolving every candidate twice (once to
check, once when httpx connects) and turns a pure function into a network call. **My
plan:** predicate 1 is pure syntax; resolution failure surfaces at predicate 2 as
`dns_error`, which is the reason string §6.1 already assigns to predicate 2. Same
observable behaviour, half the lookups, and `validator.py` stays testable without a
resolver stub. Flagging because it's a literal deviation from the table's wording.

### F4. Predicate 5 when `Content-Length` is absent
Chunked responses and many CDNs omit it. Failing `too_small` there would reject good
images; skipping to predicate 6 costs a partial GET we're about to do anyway. **My
plan:** absent header → predicate 5 passes with `meta.content_length=None`, predicate 6
enforces the floor on real bytes read and can still return `too_small`. Recorded in the
review file so it's visible.

### F5. Python 3.13 + `extruct` — RESOLVED in Phase 1
`extruct` 0.18.0 installed cleanly on 3.13.5, `pyrdfa3` 3.6.5 and all, and imports fine.
No fallback parser needed, and RDFa stays available as §5.2's fourth preference. The
original concern is left below for the record.


`extruct` pulls `pyrdfa3`/`rdflib` for RDFa, which is the least maintained part of that
dependency tree and the most likely to fight 3.13. **Mitigation:** call
`extruct.extract(html, syntaxes=["json-ld", "microdata", "opengraph"])` — RDFa is fourth
in §5.2's preference order and contributes almost nothing on real product pages. If the
install is clean I'll add `"rdfa"` back. If `extruct` won't install at all, the fallback
is a ~120-line in-house JSON-LD + OG parser, which covers the overwhelming majority of
what §5.2 actually depends on. I'll confirm which path in Phase 2.

### F6. Making `gi_region` structurally unwritable
§4 says "hard-code this: `gi_region` is not writable by the extractor." **My plan:**
`gi_region` isn't a field on the extractor's output model at all. `csv_writer` emits the
column as a literal `""` constant, and §9.7 asserts it. A GI mention found in source
text goes to `review.gi_mention_found` as a question. This means no code path exists
that *could* populate it, which is stronger than a runtime check.

### F7. Predicate 9 is ordered last, and I think it should be ~first — requesting a ruling
As specified, the reputation cache runs *after* the hotlink test. So for a host we
already know blocks hotlinking, every future row still pays predicates 2–7 — including
the extra hotlink round-trip — before the cache says "we knew that." On a 5,000-URL
batch against one CDN that's thousands of avoidable requests, which reads against the
cheap-path-first philosophy the rest of the document is built on.

Moving it to position 2 (after pure syntax, before any network call) preserves every
stated guarantee: `tier1_attempted` stays `True`, the reason stays `host_known_to_block`,
and §6.1's "resolves instantly" line arguably describes this behaviour already.

**I have not assumed this.** Tell me which you want; I'll implement the ordering you
choose and the predicate tests will pin it either way.

### F8. `hs_code` — I will not invent a table
§12 lists HS codes as one of the four things to stop and ask about. I have exactly two
data points from §4: cotton apparel → 6206, silver jewellery → 7113. **My plan:**
`config.yaml` seeds only those two, every other category maps to blank + flag, and
`config-check` reports how thin the map is. Give me an authoritative list (or a customs
broker's, or haat's own suggester output) and I'll load it. I'd rather ship 2 correct
mappings and 400 flagged blanks than a plausible table.

### F9. FX rate source
§4 says "a rate from config or a fetched-and-cached rate" without naming a provider.
**My default: config-only, no network.** `config.yaml` carries a rates table with an
explicit `as_of` date, `config-check` warns when it's over 30 days old, and every
converted price records rate + timestamp per §9.9. Adding a live provider means a new
outbound dependency and a new failure mode on a tool whose default price strategy is
`blank` anyway. Say the word if you want a fetcher and name the provider.

### F10. `third-party` provenance in `url_columns` mode
Rule 2.2 forbids `image_method=hosted` for third-party, but `url_columns` mode needs a
URL. **My plan:** the run is allowed; Tier 1 may still pass (linking to an image the
operator doesn't own is the operator's call, and it's recorded); if Tier 1 fails, Tier 2c
is *unreachable* and the row lands at `image_method="none"`, `status="needs_review"` per
§6.6. `config-check` and the run banner both say so. The alternative — refusing the
combination outright — is defensible; tell me if you'd prefer it.

### F11. `row_key` and URL canonicalisation
**My plan:** `row_key = slugify(host + path)[:60] + "-" + sha1(canonical_url)[:8]`.
Canonicalisation lowercases scheme and host, drops the fragment, strips a configurable
tracking-param list (`utm_*`, `gclid`, `fbclid`, `ref`, `_ga`), and **preserves remaining
param order** — sorting them is tempting for dedupe but breaks sites where query order is
semantic. Dedupe (§7.1) keys on the canonical URL.

### F12. SQLite under async
`aiosqlite` adds a dependency for a workload that's tiny relative to network time. **My
plan:** stdlib `sqlite3` in WAL mode, all access funnelled through one `Ledger` object,
writes wrapped in `asyncio.to_thread` with short transactions. Resume state is committed
per completed row so Ctrl-C mid-batch loses at most one row.

### F13. Section cross-references in the prompt are off by one
Not a design question, just so you know I'm not confused: §2.1 and §2.6 say "the §7
predicate list" and "every branch in §7", but the predicates are §6.1 and §7 is Outputs.
§2.2 says "phase order from §12", but phases are §11 and §12 is working style. I'm
reading these as §6.1 and §11 respectively. Worth fixing in your copy since future
sessions will read it cold.

### F14. Smaller assumptions I'm proceeding on without asking
- `--images manifest` is the default, so **the default configuration cannot make a
  single image-host call.** §9.2 proves it.
- CSV injection guard prefixes `'` to fields starting with `= + - @ TAB CR`. Our numeric
  columns are non-negative integers so this only ever touches text fields.
- `max_images_per_product` default 6, hero first; Tier 1 validates candidates in rank
  order and stops at the first pass, so the other five cost nothing when candidate 1 is good.
- Per-domain concurrency is hard-capped at 1 regardless of `--concurrency`, with a
  jittered 2 s delay (§2.5, §8). `--concurrency` scales across *distinct* domains only.
- Robots is checked once per domain and cached for the run.
- A `.env` with no host credentials is fine in `manifest` mode and warns **at startup**,
  not mid-run (§6.4).
- User-Agent is a real contactable string built from a config value; `config-check`
  fails if it's still the placeholder.
- No CAPTCHA solving, no proxy rotation, no fingerprint spoofing, no paywall bypass —
  a blocking site is a loud row failure (§2.5). I won't add these later either.
```
