# haat-lister

Turns product page URLs into a haat bulk-listing CSV — honestly.

Point it at your own catalogue, get back `listings.csv` in haat's exact 19-column
format, the product photos as files ready to upload, and a worklist telling you
which rows still need a human and why.

There is a command line and a local web console. They run the same code.

The design assumption throughout is that **a blank cell you can see is worth more
than a plausible value you can't check.** Nothing here guesses a price, a weight,
a dimension, an HS code or a GI region.

A second assumption, learned the hard way and now everywhere in the code: **the
tool never reports a check it did not run.** A page that never arrived does not
get "bot check: no". A count nobody could take is not zero. A photograph counted
twice at two resolutions is not two photographs.

---

## What this tool is for

Moving **your own** catalogue onto haat. A seller with two hundred products on
their own storefront, or on a marketplace they already sell through, who does
not want to retype them.

That constraint is not a formality. haat's seller rules require products made in
India by the seller and prohibit resold or dropshipped goods; product
photographs and marketing copy belong to whoever created them. So this tool will
happily read any product page you point it at — and a listing built from someone
else's page will not survive haat's own review, however clean the CSV is. That
is why `--provenance` is required and has no default: whether you made the thing
is a fact only you know, and it is the one question the tool refuses to answer
for you.

If you are pointing it at a page you do not own to see what it does, that is
fine. Just know which of those two things you are doing before you paste five
hundred links.

---

## Five minutes from clone to CSV

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows;  source .venv/bin/activate elsewhere
pip install -e .

cp .env.example .env            # then set HAAT_CONTACT to an address you read
haat-lister serve
```

That opens `http://127.0.0.1:8000`. Paste product links, say who made them, press
one button, download the CSV. No terminal needed after that first command.

Five screens:

| | |
|---|---|
| **Compose** | paste links, choose provenance, run |
| **Sheet** | `runs/master.csv` — everything every finished job produced, deduped |
| **Jobs** | what you have run, and its files |
| **Find photos** | every photo for every product, *before* committing to a run |
| **Import** | a seller export or a page you saved — for shops that refuse a fetch |

Plus **Why no photo?** at `/diagnose`, reachable from any row that came back
empty.

**Find photos** is the one worth knowing about first. Paste a catalogue or upload
a CSV — it detects which column holds the links and carries your SKU column
through — and it tells you which products have usable photos, which are too
small, and which shops refuse us. It writes nothing, publishes nothing, and never
contacts an image host, so it costs you a look and nothing else. What it learns
is cached, so the real run afterwards does not re-fetch the same shops.

**Import** is the one worth knowing about second, and the reason is in
[Sites that refuse us](#sites-that-refuse-us).

### The two CSVs

haat's import template is nineteen columns and none of them is an image, so
every run produces two files:

```
listings.csv               19 columns  →  upload this to haat
listings_with_images.csv   + every photo URL, size, and method  →  for your records
```

They are written from the same rows in the same order, and a test asserts they
line up. The same pair accumulates as `master.csv` and `master_with_images.csv`.

The console ships **built**, so a machine with no Node still runs it.
`config-check` runs on the page and tells you what is missing before you spend a
request.

---

## The console

### Paste links

![Compose](docs/screenshots/compose.png)

Duplicates and malformed lines are marked **in place**, with line numbers, so
they can be fixed without leaving the screen. `plain/3?utm_source=x` is
recognised as the same product as `plain/3` and fetched once.

**Provenance has no default.** The run cannot start until you say who made the
content, with a sentence explaining why it is being asked. Settings collapse to
one line — `manifest mode · price left blank · descriptions as written`.

If the agent is not reachable the counters read `--` rather than `0`, and the
button says so. A count nobody could take is not zero, and rendering it as one
blames your links for a server that is not running.

### Before it starts

Preflight reads `robots.txt` once per host — not once per URL — and consults
`domains.yaml`, the record of refusals previous runs observed. Both arrive at
second zero rather than four minutes in.

`domains.yaml` **never prevents a run.** It is a record of observations, and
observations go stale: a site that rate-limited you on a Monday afternoon is not
a site you may never speak to again. Entries expire after thirty days, the
wording says "it may well have changed its mind", and there is deliberately no
field in the preflight response that a screen could refuse to submit on.

### Watch it work

![Running](docs/screenshots/running.png)

The rail is the **fill grid**: 19 columns wide, one per CSV column, filling cell
by cell as rows land. Each cell is shaded by how deeply that field is dyed —
full depth for a value read from JSON-LD, mid-vat for one inferred, pale for one
nobody could find. `gi_region` is hatched, because it is locked rather than
empty.

Read it and you already know what you will have to fix: `price_inr` is a pale
stripe, `title` is solid.

The stages are real transitions, not a timer. The tier counts are Rule 1's
economics live: `direct 2 · local 0 · hosted 0`.

### Take the file

![Complete](docs/screenshots/complete.png)

One primary action. Everything else quiet beneath it, sized in rows rather than
kilobytes. If anything needs a human it says so in brass and offers to fix it
here.

### Fix what needs fixing

![Review](docs/screenshots/review.png)

Arrows move, Enter edits, Enter commits and drops one row in the same column —
because filling a column is what actually happens. Space selects; a selection
can be bulk-set in one request. An edited cell sits on white and remembers what
the page said.

`gi_region` is hatched and refuses focus, so the keyboard skips it rather than
stopping on a cell that can never be filled.

**Edits never overwrite the extraction.** They are stored beside it and applied
on the way out, so removing one restores what the page said, and six weeks later
"did we scrape this or did I type it?" has an answer.

---

## Every URL ends in exactly one of four states

```
written       came out clean
needs_human   came out, but a cell needs a decision
refused       the site declined, and stopping was correct
failed        something broke, and it might work next time
```

They are decided in **one function** (`jobs.terminal_state`), so the ledger, the
counts, the files and the retry button read one answer rather than each deriving
their own. They are disjoint, and they sum to the number of URLs processed.

`refused` is not a shade of `failed`, and the difference is the whole point:
**the retry button excludes refusals**, because retrying `robots_disallowed`
produces `robots_disallowed`, forever. A button that cannot change its own
outcome should not exist.

The one deliberate overlap: a `needs_human` row is written to `listings.csv`
*and* pointed at by `review.csv`. Nothing else is ever in two files, and the job
header reports "how many rows are in listings.csv" as its own number rather than
folding it into `written`.

### One name per outcome

`haat_lister/images/reasons.py` is a closed vocabulary. Every member carries a
class (`refused` or `failed`), whether it is worth retrying, and **a next action
in a sentence** — not a description of the problem, a route out of it.

The job page, `diagnose`, Find photos and `failed.csv` all read that one table,
so they cannot print different words about the same request. An unrecognised
string reads as `failed`, never `refused`: calling something a refusal is a
claim about the site, and it is only made when a refusal was actually seen.

---

## The command line

```bash
haat-lister config-check                       # costs nothing; run it first
haat-lister single URL --provenance own --dry-run
haat-lister batch urls.txt --provenance own
haat-lister batch urls.txt --provenance own --job j_7fk2m9qa --resume
haat-lister import catalogue.csv --provenance own    # a seller export
haat-lister import page.html --provenance own        # a page you saved
haat-lister preflight urls.txt                 # what we already know, before the run
haat-lister diagnose URL                       # why this page gave the photo it did
haat-lister validate-only urls.txt             # Tier 1 sweep, zero downloads
haat-lister master                             # inspect runs/master.csv
haat-lister profiles                           # which rung each host answers on
haat-lister review                             # re-emit review.csv from the ledger
haat-lister rehost-failed                      # retry Tier 2 for stored rows
haat-lister serve                              # the console
```

Ctrl-C stops a batch cleanly and commits everything finished; `--resume` picks up
without re-fetching a page.

### Options worth knowing

```
--provenance own|authorised|third-party    required, no default, on every route
--images manifest|url_columns|both         default manifest (files, not URLs)
--render / --no-render                     Stage B browser fallback
--url-timeout SECONDS                      one clock for ONE url, default 20
--llm                                      model-assisted rewrite + category choice
--concurrency N                            rows in flight across the batch
--resume                                   skip what this job already completed
--dry-run / --json                         report only / the full record
-v / -vv                                   info / debug
--log-file PATH                            one JSON line per URL, credentials redacted
```

On `import`:

```
--source-url URL       for a saved page that does not record where it came from
--save-profile NAME    remember this export's column mapping for next time
--dry-run              show the column mapping and stop; writes nothing
```

---

## What a job leaves behind

```
runs/j_7fk2m9qa/
├── listings.csv              the import file. 19 columns, rows in the order you pasted
├── listings_with_images.csv  the same rows plus every photo link. Not for uploading
├── review.csv                every row that needs a human, and which cells
├── image_manifest.csv        which photo belongs to which row. The first is the hero
├── failed.csv                URLs that produced nothing
├── images/<row_key>/         the photos, normalised and ready to upload
└── job.json                  the exact settings this used
```

**Every URL you paste ends up in exactly one of those files.** Not usually — it
is asserted at the end of every job, including after a cancel, and a job that
cannot say where a URL went refuses to call itself done.

`failed.csv` carries the columns you need to decide what to do next:

| column | what it is for |
|---|---|
| `class` | `refused` or `failed`. Filter on this before pasting URLs into a new job |
| `reason` | one word from the closed vocabulary |
| `rungs_tried` | `a1_http2:transport_reset:507ms \| a2_http11:timeout_read:4700ms` |
| `time_spent` | `6s - fetch 5.3s, parse 0.0s, idle 1.1s` |

The goal you should be able to meet: clear `review.csv` **without reopening a
single source page** — except for price, which is a business decision nobody can
scrape.

---

## The two rules

Everything else in this codebase is detail. These two are the design.

### Rule 1 — images: try the free thing first, always

Every row's own image URL is tested against nine predicates before anything is
downloaded or uploaded. If it survives, that URL is used and **no bytes move**.
Only a URL *proven* to fail can reach a paid image host.

```
Tier 1   validate the source's direct URL          every row, always
Tier 2a  download                                  only if Tier 1 failed, or files are wanted
Tier 2b  normalise (resize, strip EXIF, re-encode)
Tier 2c  upload to a host                          only if Tier 1 failed AND a URL is needed
Tier 2d  re-validate what the host gave back       through the same nine predicates
```

The gate is literal code, not a convention:

```python
do_download = need_file or (need_url and not tier1_passed)
do_upload   = need_url and not tier1_passed
assert not (do_upload and tier1_passed)
```

The predicates run cheapest-first — syntax, reputation cache and signed-URL
detection all **before any network call**, so a rejection from a CDN you already
know blocks hotlinking costs nothing:

1. syntax · 8. signed/expiring URL · 9. host reputation · 2. reachable ·
3. redirect sanity · 4. content-type · 5. size floor · 6. decodable + dimensions ·
7. **hotlink test** — fetched without a `Referer`, the way a buyer's browser
   would load it from a haat listing.

Predicate 7 is the one that matters. A URL that works when you paste it in your
browser and 403s for a buyer is the commonest way a marketplace listing ends up
with a broken photo.

Predicate 5 has one deliberate soft spot. Some CDNs answer `HEAD` with a stub —
`Content-Length: 20` for a 1500×1500 photograph — and believing them rejected
every photo on the site. No image in any format can be encoded in fewer than 26
bytes, so below that the header stops being evidence and predicate 6 reads the
real bytes. A genuine 43-byte tracking pixel still dies at predicate 5 for free.

In the default `manifest` mode there is no image-host object at all, so `--images
manifest` doesn't mean "don't call a host" — there is nothing to call. That is
the right mode for haat, whose uploader takes files.

### Rule 2 — provenance: the tool will not assume it

Required, with no default, on the command line, in the console, and **on every
ingestion route including imports**. Reading a page off local disk tells you
nothing about who owns the photographs in it.

| Value | Effect |
|---|---|
| `own` | You made or own this content. Normal operation. |
| `authorised` | You have the rights holder's permission. Normal operation. |
| `third-party` | Neither. Every row is forced to `needs_review`, images are **never** re-hosted, and descriptions are forced through a rewrite. |

Product photographs and marketing copy belong to whoever created them. Which of
those applies is a fact only you know — and re-uploading photographs you don't
own would be this tool making a copy of someone else's work on your behalf.

---

## Sites that refuse us

Some shops refuse a correctly-identified client on every attempt. That is an
answer, and this tool takes it as one.

The fetcher tries four ways of *asking*, never four identities:

```
A1  HTTP/2,   browser header set            the fast path, most sites
A2  HTTP/1.1, same headers                  some CDNs reset h2 and serve h1 fine
A3  HTTP/1.1, fresh connection              transient resets, stale pools
B   a real browser                          when the site genuinely wants one
```

Each rung has its own short budget, and the whole row has **one deadline**
(`--url-timeout`, default 20s) covering every rung, the browser, and any retry.
Without that the limits nest and nothing holds the total: a single URL could
occupy most of a minute while every individual limit was respected.

Retries are for reasons a later attempt could change — `timeout_connect`,
`timeout_read`, `http_error_5xx`, `blocked_429`. Never a refusal, never
`dns_failure`, never a 404. `Retry-After` is treated as an instruction and not a
hint; that header is the entire difference between a retry budget and pressing.

After five consecutive whole-ladder failures on one host, later URLs on it fail
immediately rather than re-climbing. The count is about a run, not a reputation:
any success clears it.

**When a site still says no, the answer is a door you already have a key to.**

### Import

`haat-lister import`, or the **Import** screen:

- **A seller export** (`.csv`, `.tsv`, `.xlsx`). Columns are auto-mapped, shown
  to you with sample values, and confirmed before a single row is built.
  Unmapped columns are listed by name, never discarded silently. Save the
  mapping once and it reapplies to the next export with the same headers.
- **A page you saved** (`Ctrl+S` → "Webpage, complete", or `.mhtml`). The
  photographs in the `_files` folder come with it, so a shop that refuses the
  page still yields its gallery — with no request to that shop at all.

Both go through the *same* extraction, the same nine image predicates and the
same provenance gate as a fetched URL. An import that built its own record would
skip that gate by omission rather than by decision, so it doesn't build one.

A saved page and a live fetch of the same product produce byte-identical
19-column rows, and there is a test that says so.

---

## What this tool will not do

Stated plainly so you know what you're getting, and so the next person to work on
this doesn't add it:

- **No anti-bot evasion.** No CAPTCHA solving, no paywall bypass, no fingerprint
  spoofing, no proxy rotation, no stealth plugins, no `navigator.webdriver`
  patching, no cookie replay lifted from a browser session. If a site blocks us,
  that is an answer: the row fails loudly and says which URL and why.
- **It identifies itself honestly.** One real, contactable User-Agent with your
  email in it, used by both httpx and Chromium. The ladder tries other
  *protocols*, never other identities.
- **It respects `robots.txt` by default,** one request per origin per run. A
  401/403 on `robots.txt` is read as *disallowed* — a site that won't show its
  rules hasn't granted anything. `--ignore-robots` exists, defaults off, warns,
  and does not change the terminal state shown to you.
- **It is polite by default.** One request at a time per host, ~2s apart with
  jitter. `--concurrency 5` is a budget across the batch, never five simultaneous
  hits on one shop. A site's own `Crawl-delay` wins when longer; a shorter one
  never speeds us up.
- **It will not fetch from your own network.** Loopback, private ranges,
  link-local and the cloud metadata endpoint are refused — **on every redirect
  hop**, because a guard that only checks the URL you typed is decorative. The
  escape hatch is a list of hostnames in config, never a switch, and never
  settable from a request.
- **`gi_region` is always blank.** A GI tag is an Indian government certification
  and haat makes it a seller declaration. The internal record has no such field,
  the writer emits a constant, the API refuses to set it, and no column mapping
  or saved profile can reach it. Four barriers, so removing one doesn't open it.
- **Money and customs fields are never silently guessed.** `price_inr` is blank
  by policy by default. HS codes are clearly-labelled suggestions, never facts.

### The one thing it does not stop

**DNS rebinding.** The SSRF guard resolves a hostname and checks the addresses;
httpx resolves again when it opens the socket, and a hostile resolver can answer
differently. Closing that needs a custom transport. It doesn't matter on
loopback, and `serve --host 0.0.0.0` says so before it starts.

---

## How a page becomes a row

```
preflight (robots per host, domains.yaml)
      ↓
robots.txt  →  fetch ladder A1→A2→A3  →  [rung B: browser]     one deadline for all of it
      ↓
extract  →  [plugin]  →  page-shape verdict
      ↓ only if something's missing a browser could supply
Stage B (Chromium, ~3s) →  extract  →  [plugin]  →  merge
      ↓
enrich (category, HS suggestion, FX, policy screen)
      ↓
[--llm]  →  images (Rule 1)  →  policy defaults  →  provenance gate  →  ledger
      ↓
listings.csv, in the order you pasted
```

**Stage B runs only for incomplete records** — missing title, description or
image candidates. Deliberately not for missing price, weight or dimensions:
those are absent from most source pages full stop, so paying three seconds a row
to confirm that would be the most expensive possible way to learn nothing.

Where the two stages disagree, a rendered value wins only if the static one was
empty or less trustworthy. **On a tie the static value stands** — a rendered DOM
is also where recommendation carousels live.

A captcha wall, a sign-in interstitial and a "no longer available" notice all
arrive as a 200 and extract into a tidy record with a title and no photos. The
**page-shape verdict** fails the row there, before Stage B and before any image
work, and retracts whatever the extractors said about it: "no weight found" on a
captcha wall sends you to fill in a weight for a page nobody saw.

### Which images it picks

Shops name their gallery files after the product and do not name their menu
icons after it. Candidates whose filename shares words with the page's own URL
sort first, which lifts `rani-pink-dola-silk-printed-saree-1.jpg` above
`saree-menu.jpg` on any shop without knowing anything about that shop. It sorts
on a count, so a site whose gallery shares no words with its URL is left in its
existing order — the rule can promote, never demote.

Candidates *tested* (`images.max_candidates`, 40) and photographs *kept*
(`images.max_images_per_product`, 10) are separate numbers. They used to be one,
and a page whose chrome outranked its gallery could never return more than the
handful of real photos that fit in the same budget.

Photographs are counted by identity, not by URL. `71rOScyvhRL.jpg` and
`71rOScyvhRL._SL1500_.jpg` are one photograph at two sizes; the largest wins.

---

## The ledger is the source of truth

`store/ledger.db` holds every row, tagged with the position you pasted it at.
The CSV is a **projection** of it.

That one decision resolves a conflict that otherwise has no answer — rows must
come out in input order, and the file must be written incrementally so a job
that dies at #480 doesn't lose 480 rows. Rows commit to SQLite the instant they
finish; a watermark writer appends in index order, buffering only rendered lines.

The consequence worth noticing: **the file on disk is always a correctly ordered
prefix.** Never a hole, never a row out of place. That is what makes "download
what's done so far" an honest offer, what makes a mid-job page refresh free, and
what makes a re-export after edits the same operation rather than a second one.

Because the ledger outlives any one release, a record written before a
vocabulary changed still loads: retired names read as unknown rather than
raising. A projection that cannot be rebuilt has quietly become the source of
truth.

---

## Why no photo?

`haat-lister diagnose URL`, or `/diagnose` in the console. It runs the real
path — the same fetcher, the same extraction, the same nine predicates in the
same order — and reports every step instead of only the verdict.

```
FETCH
  stage A   timeout_read
            fail  HTTP/2                      transport_reset       0.5s
            fail  HTTP/1.1                    timeout_read          8.4s
  robots    allowed
  page      looks like a product page?  -- not reached
            captcha wall?               -- not reached
            no page arrived, so none of these were asked
  stage B   disabled (--no-browser)
```

Three things it will not do:

- **It does not report a check it did not run.** Every check renders `yes`, `no`
  or `— not reached`. "bot check: no" about a page that never arrived is a true
  statement about a variable and a false one about the shop, and it sends you
  into the extractor to debug a transport problem.
- **It does not say "off".** Stage B says *why*: `disabled (--no-browser)`,
  `not needed (stage A complete)`, `not attempted — stage A returned no
  response`, or `a browser would get the same answer`. Those are four different
  next actions.
- **It does not try harder than the pipeline.** If a page blocks us, the report
  says so and stops; the remedy offered is your own export, never a way around.

---

## Plugins

For shops the generic path gets wrong. A plugin runs **last and its values win**
— that's the point of writing one. The safeguard isn't restraint, it's
accountability: every field it supplies is stamped `source=plugin`, so
`review.csv` names exactly which cells came from where.

```yaml
extraction:
  plugins_dir: "my_plugins"     # your Python; only read when non-empty
```

```python
from haat_lister.extract.plugins import PluginContext, PluginResult, register
from haat_lister.models import Confidence, FieldSource, FieldValue

class MyShop:
    name = "my_shop"

    def matches(self, url: str, html: str) -> bool:
        return "myshop.example" in url      # keep cheap: runs on every page

    def extract(self, ctx: PluginContext) -> PluginResult:
        result = PluginResult()
        if node := ctx.dom.css_first(".product-weight"):
            result.fields["weight_g"] = FieldValue.found(
                int(node.text(strip=True).removesuffix("g")),
                FieldSource.PLUGIN, Confidence.HIGH,
            )
        return result

register(MyShop())
```

`haat_lister/extract/plugins/example_shopify.py` is a full worked example: it
recovers Shopify's real variant price from the theme's JavaScript and the full
gallery from a slider that renders one `<img>` — both without a browser.
`amazon.py` is the other.

A plugin **cannot** write `gi_region`, claim a value came from JSON-LD, clear a
row's status, or exempt an image from Tier 1.

---

## The `--llm` layer

Optional, opt-in per run, deliberately narrow. It **rewrites a description** in
the seller's own words, and **chooses a category from `taxonomy.yaml`** when
keyword matching fell through — a choice from a closed list, never an invention.

It cannot write a price, weight, dimension, HS code, GI region, availability or
stock count — not because the prompt says so (though it does) but because
`RewriteResult` has no field to put one in. A model returning `{"price_inr":
2499}` isn't rejected; there is nowhere for it to land.

Rewrites are re-screened against the policy vocabulary, so a GI claim the source
never made is flagged. Responses are cached in the ledger by prompt hash.

---

## Try it without touching a real shop

```bash
python demo/shop.py                                  # terminal 1, port 8799
# config.yaml → fetch.allow_private_hosts: ["127.0.0.1"]
haat-lister serve                                    # terminal 2
```

`demo/shop.py` serves three deliberately awkward kinds of page: clean JSON-LD, a
React shell with nothing in the HTML, and one whose images 403 anyone without a
`Referer`. `demo/urls.txt` covers all three plus a duplicate and a malformed
line.

Step two is needed because the SSRF guard refuses loopback by default — this
tool fetches URLs typed by whoever can reach it, so `127.0.0.1` is exactly the
address it should not follow without being told to. **Put it back afterwards.**

---

## Configuration

`config.yaml` is commented throughout and is the durable default; CLI flags
override it for one run. Lines marked `# OPERATOR:` need your attention, and
`config-check` lists every one still unset.

The three you're most likely to touch:

- **`taxonomy.yaml`** — haat's category and subcategory slugs. Entries marked
  `derived: true` were inferred from haat's slug convention rather than read from
  real haat data. **Verify them with one test import before a large run**; a
  wrong slug either rejects the import or files your listing where nobody finds it.
- **`price.strategy`** — `blank` (default), `convert`, or `markup`. Blank because
  haat wants the maker's INR price, which is a business decision, not the scraped
  retail price of some other shop.
- **`hs_codes`** — ships nearly empty on purpose. An HS code is a customs
  declaration; until you have an authoritative list, rows carry a labelled
  suggestion rather than a fact.

Worth knowing about, rarely worth changing:

```yaml
fetch:
  url_timeout_s: 20              # one clock for ONE url, all attempts
  rung_timeout_s: 8              # per rung, clipped by whatever the row has left
  refusals_before_fast_fail: 5   # consecutive whole-ladder failures per host
  max_url_retries: 2             # only for retryable reasons, honouring Retry-After

images:
  max_candidates: 40             # how many we TEST
  max_images_per_product: 10     # how many we KEEP
```

### A note on compression

`brotli` and `zstandard` are **hard dependencies**, not extras, and the reason is
a bug worth not repeating. The header set advertised Brotli unconditionally while
the codec was optional, so on a normal install we asked every site for Brotli,
got it, and handed the raw compressed bytes to the parser. A 200 came back, the
body was unreadable binary, and the row reported "no title, no images" — blaming
the shop for something entirely ours. Only sites that happened to serve gzip
worked.

The `Accept-Encoding` header is now derived from what httpx can actually decode,
so it cannot drift from the truth, and `config-check` reports a missing codec
rather than leaving it to be discovered as a shop with no photographs.

---

## Development

```bash
pip install -e ".[dev]"
pytest                       # ~900 tests
ruff check . && mypy haat_lister

cd web && npm install && npm run build     # the console
python web/audit.py  http://127.0.0.1:8131 # axe + keyboard + reduced motion
python web/e2e.py    http://127.0.0.1:8131 # paste → run → download → verify bytes
```

The tests that matter most assert things *didn't* happen:

| file | what it refuses to let you break |
|---|---|
| `test_image_pipeline_gate.py` | counts host calls and downloads |
| `test_fetch_stages.py` | asserts no browser process was launched |
| `test_job_output.py` | asserts no URL went missing |
| `test_accounting.py` | the four states are disjoint and sum |
| `test_diagnose_honesty.py` | no check is reported that did not run |
| `test_encoding.py` | we never advertise a codec we cannot decode |
| `test_security.py` | sweeps the security clauses one by one |

`test_diagnose_honesty.py` asserts on **rendered output**, not the model. The
whole life of that bug was a right answer that never reached a human.

`web/audit.py` and `web/e2e.py` are runnable, not aspirational: axe-core comes
from `node_modules`, and the E2E checks the downloaded header byte-for-byte
against `haat-bulk-listings-template.csv`. `tests/test_e2e_import.py` drives a
real Chromium through the import screen and skips — rather than fails — when the
browser or the built console is absent.

---

## Still open

Three things need you, and none of them block a first run:

- **A test import into haat.** The CSV matches the template byte-for-byte, but
  "the importer accepts it" is the one claim nobody here has verified. One row
  would settle it — and would settle the fourteen derived slugs at the same time.
- **The made-to-order availability value.** `fields.availability_made_to_order_value`
  is unset, so those rows go out blank and flagged. Export one made-to-order
  listing from your panel and read the cell.
- **An authoritative HS-code list.** Two entries ship — `apparel` and
  `jewellery`, both evidenced by haat's own template. Everything else gets a
  labelled suggestion, which means most rows route to review until the list
  grows.
