# haat-lister

Turns product page URLs into a haat bulk-listing CSV — honestly.

Point it at your own catalogue, get back `listings.csv` in haat's exact 20-column format, the photos as files ready to upload, and a worklist of the rows that still need a human.

There is a command line and a local web console. They run the same code.

Two assumptions run through all of it. **A blank cell you can see is worth more than a plausible value you can't check** — nothing here guesses a price, weight, dimension, HS code or GI region. And **the tool never reports a check it did not run** — a page that never arrived doesn't get "bot check: no".

## What this tool is for

Moving *your own* catalogue onto haat.

haat requires products made in India by the seller and prohibits resold goods; photographs and copy belong to whoever made them. This tool will read any page you point it at, but a listing built from someone else's page won't survive haat's review however clean the CSV is. That's why `--provenance` is required and has no default: whether you made the thing is a fact only you know.

## Five minutes from clone to CSV

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows;  source .venv/bin/activate elsewhere
pip install -e .

cp .env.example .env            # set HAAT_CONTACT to an address you read
haat-lister serve
```

Opens `http://127.0.0.1:8000`. Paste links, say who made them, press one button, download the CSV.

**Compose** paste and run · **Sheet** everything every job produced, deduped · **Jobs** past runs · **Find photos** which products have usable photos, before committing to a run · **Import** a seller export or a saved page · **Why no photo?** at `/diagnose`.

Start with **Find photos**. Paste a catalogue or upload a CSV — it detects the link column and carries your SKU through. It writes nothing and contacts no image host, and what it learns is cached so the real run doesn't re-fetch.

## The two CSVs

haat's template ends with one `image_urls` cell — pipe-separated, with no room for which photo
passed which check or why the others did not. So every run produces both:

```
listings.csv               20 columns  →  upload this to haat
listings_with_images.csv   + every photo URL, size, method  →  for your records
```

Same rows, same order, and a test asserts they line up. They accumulate as `master.csv` and `master_with_images.csv`.

## The two rules

Everything else is detail.

**Rule 1 — images: try the free thing first.** Every row's own image URL is tested against nine predicates before anything is downloaded or uploaded. Survives → that URL is used and no bytes move. Only a URL *proven* to fail can reach a paid host. The gate is literal code:

```python
do_download = need_file or (need_url and not tier1_passed)
do_upload   = need_url and not tier1_passed
assert not (do_upload and tier1_passed)
```

Predicates run cheapest-first — syntax, reputation cache and signed-URL detection before any network call. The one that matters is the **hotlink test**: the image is fetched with no `Referer`, the way a buyer's browser loads it from a haat listing. A URL that works when you paste it and 403s for a buyer is the commonest cause of a broken listing photo.

**Rule 2 — provenance: the tool will not assume it.** Required, no default, on every route including imports — reading a page off disk tells you nothing about who owns the photographs in it.

| | |
|---|---|
| `own` / `authorised` | normal operation |
| `third-party` | every row forced to `needs_review`, images never re-hosted, descriptions rewritten |

## Every URL ends in exactly one of four states

```
written       came out clean
needs_human   came out, but a cell needs a decision
refused       the site declined, and stopping was correct
failed        something broke, and it might work next time
```

Decided in one function, disjoint, and they sum to the URLs processed — asserted at the end of every job, including after a cancel. `refused` is not a shade of `failed`: the retry button excludes refusals, because retrying `robots_disallowed` produces `robots_disallowed` forever.

## Sites that refuse us

The fetcher tries four ways of *asking*, never four identities:

```
A1  HTTP/2,   browser header set     the fast path
A2  HTTP/1.1, same headers           some CDNs reset h2 and serve h1 fine
A3  HTTP/1.1, fresh connection       transient resets, stale pools
B   a real browser                   when the site genuinely wants one
```

One deadline covers the whole row (`--url-timeout`, default 20s). Retries only for reasons a later attempt could change; `Retry-After` is an instruction, not a hint. After five consecutive whole-ladder failures on a host, later URLs on it fail immediately — any success clears the count.

When a site still says no, use **Import**: a seller export (`.csv`/`.xlsx`, columns auto-mapped and confirmed before use) or a page you saved with Ctrl+S (the photos come with it, so the shop yields its gallery with no request to it at all). Both go through the same extraction, the same nine predicates and the same provenance gate. A saved page and a live fetch produce byte-identical rows, and a test says so.

## What this tool will not do

Stated plainly so the next person doesn't add it.

- **No anti-bot evasion.** No CAPTCHA solving, paywall bypass, fingerprint spoofing, proxy rotation, stealth plugins, or cookie replay. A block is an answer; the row fails loudly.
- **It identifies itself honestly.** One real, contactable User-Agent with your email in it.
- **robots.txt respected by default**, one request per origin per run. A 401/403 on robots.txt reads as disallowed. `--ignore-robots` exists, defaults off, warns.
- **Polite by default.** One request at a time per host, ~2s apart. `--concurrency 5` is a budget across the batch, never five hits on one shop. A site's own `Crawl-delay` wins when longer.
- **Won't fetch from your own network.** Loopback, private ranges and the metadata endpoint refused, on every redirect hop. Not closed: DNS rebinding, since httpx re-resolves when it opens the socket. Doesn't matter on loopback; `serve --host 0.0.0.0` says so before it starts.
- **`gi_region` is always blank.** A GI tag is a government certification and haat makes it a seller declaration. Four independent barriers, so removing one doesn't open it.
- **Money and customs fields are never guessed.** `price_inr` blank by default; HS codes are labelled suggestions.

## The command line

```bash
haat-lister config-check                    # costs nothing; run it first
haat-lister single URL --provenance own --dry-run
haat-lister batch urls.txt --provenance own [--resume]
haat-lister import catalogue.csv --provenance own
haat-lister preflight urls.txt              # what we already know, before the run
haat-lister diagnose URL                    # why this page gave the photo it did
haat-lister find urls.txt                   # photos for every product, writes nothing
haat-lister master | profiles | review | rehost-failed
haat-lister serve
```

Ctrl-C commits everything finished; `--resume` picks up without re-fetching.

Worth knowing: `--images manifest|url_columns|both` (default `manifest` — files, which is what haat's uploader takes) · `--render/--no-render` · `--url-timeout` · `--concurrency` · `--llm` · `--dry-run/--json` · `-v/-vv` · `--log-file`.

## What a job leaves behind

```
runs/j_7fk2m9qa/
├── listings.csv              the import file, in the order you pasted
├── listings_with_images.csv  same rows plus every photo link
├── review.csv                every row that needs a human, and which cells
├── image_manifest.csv        which photo belongs to which row; first is the hero
├── failed.csv                class · reason · rungs_tried · time_spent
├── images/<row_key>/         normalised, ready to upload
└── job.json                  the exact settings this used
```

Filter `failed.csv` on `class` before pasting URLs into a new job. The goal you should be able to meet: clear `review.csv` without reopening a source page — except for price, which nobody can scrape.

The ledger (`store/ledger.db`) is the source of truth; the CSV is a projection tagged with the position you pasted at. So the file on disk is always a correctly ordered prefix — never a hole, never a row out of place. That's what makes "download what's done so far" honest and a mid-job refresh free.

## Configuration

`config.yaml` is commented throughout; CLI flags override it for one run. Lines marked `# OPERATOR:` need your attention, and `config-check` lists every one still unset.

The three you're most likely to touch: **`taxonomy.yaml`** (entries marked `derived: true` were inferred from haat's slug convention, not read from haat — verify with one test import) · **`price.strategy`** (blank by default; haat wants the maker's INR price, not another shop's retail price) · **`hs_codes`** (ships nearly empty on purpose).

## Development

```bash
pip install -e ".[dev]"
pytest                       # ~900 tests
ruff check . && mypy haat_lister
cd web && npm install && npm run build
python web/audit.py http://127.0.0.1:8131    # axe + keyboard + reduced motion
python web/e2e.py   http://127.0.0.1:8131    # paste → run → download → verify bytes
```

The tests that matter most assert things *didn't* happen: `test_image_pipeline_gate.py` counts host calls · `test_fetch_stages.py` asserts no browser launched · `test_job_output.py` asserts no URL went missing · `test_accounting.py` the four states are disjoint · `test_diagnose_honesty.py` no check reported that didn't run · `test_encoding.py` we never advertise a codec we can't decode · `test_security.py` sweeps the security clauses.

To try it without touching a real shop: `python demo/shop.py` (port 8799) serves three deliberately awkward pages. Add `127.0.0.1` to `fetch.allow_private_hosts` first — the SSRF guard refuses loopback by default — and take it out afterwards.

## Still open

None of these block a first run.

1. **A test import into haat.** The CSV matches the template byte-for-byte, but "the importer accepts it" is the one claim nobody here has verified. One row settles it — and the fourteen `derived` slugs with it.
2. **The made-to-order availability value.** Unset, so those rows go out blank and flagged. Export one made-to-order listing and read the cell.
3. **An authoritative HS-code list.** Two entries ship; everything else gets a labelled suggestion, so most rows route to review until it grows.
