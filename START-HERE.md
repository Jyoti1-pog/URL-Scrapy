# haat-lister — start here

You have a ZIP. This page takes you from that to a `listings.csv` you can upload
to haat. Ten minutes, most of it waiting for one install.

If you read nothing else, read [Before your first real run](#6-before-your-first-real-run).
Three things there are business decisions, and the tool deliberately will not
make them for you.

---

## 1. What you need

| | |
|---|---|
| **Python 3.11 or newer** | `python --version`. If that fails, install from python.org and tick *"Add Python to PATH"* |
| **Windows, macOS or Linux** | all three work |
| **An internet connection** | it reads product pages |

You do **not** need Node.js. The web console ships already built.

---

## 2. Install

Unzip it somewhere you can find again — the tool writes its results next to
itself, so `Documents\haat-lister` is a better home than `Downloads`.

Open a terminal **in that folder**:

```bash
python -m venv .venv

.venv\Scripts\activate            # Windows
source .venv/bin/activate         # macOS / Linux

pip install -e "."
```

That last line takes two or three minutes. The `"."` at the end is part of the
command — it is what tells pip to install *this folder*. Leave it off and pip
answers `-e option requires 1 argument`.

**Optional**, and only if you want the browser fallback for shops that build
their pages with JavaScript:

```bash
pip install -e ".[render]"
playwright install chromium
```

Skip it to begin with. Without it those shops fail with a clear reason rather
than silently, and you can add it later.

---

## 3. Tell it who to say it is

```bash
copy .env.example .env            # Windows
cp .env.example .env              # macOS / Linux
```

Open `.env` and set **`HAAT_CONTACT`** to an email address you actually read.

This is not paperwork. Every page this tool requests carries that address in its
User-Agent, so a shop owner who wants it to stop can reach a human. Leave the
placeholder and `config-check` will tell you off.

---

## 4. Check before you spend a request

```bash
haat-lister config-check
```

Costs nothing, touches no network. You want:

```
Ready. N warning(s), 0 blocking problems.
```

**Blocking problems must be fixed before anything runs.** Warnings are things to
read once and decide about — section 6.

---

## 5. Your first run

```bash
haat-lister serve
```

Your browser opens at `http://127.0.0.1:8000`.

1. **Find photos** — paste ten product links and press the button. Nothing is
   written, no listing is created, no image host is contacted. It just tells you
   which products have usable photographs. **Always start here.** It is the
   cheapest way to learn whether a shop will work at all.
2. **Compose** — paste the same links, say who made the content, press
   *Start processing*.
3. On the job page, press **Download listings.csv**.

That file is what you upload to haat. Ctrl-C in the terminal stops the server.

### Prefer the command line?

```bash
haat-lister find urls.txt                        # look first, writes nothing
haat-lister batch urls.txt --provenance own      # then run
```

`urls.txt` is one link per line. Ctrl-C stops a batch cleanly and keeps
everything already finished; `--resume` picks up without re-fetching a page.

---

## 6. Before your first real run

Three things the tool will not decide for you, in the order they will bite.

### `--provenance` — required, no default

| you choose | what happens |
|---|---|
| `own` | you made or own this catalogue. Normal operation |
| `authorised` | you have the rights holder's permission. Normal operation |
| `third-party` | every row marked *needs a human*, photographs never re-hosted, descriptions rewritten |

haat requires products made in India by the seller and prohibits resold goods.
Photographs and copy belong to whoever made them. A listing built from someone
else's page will not survive haat's review however clean the CSV is — and
whether you made the thing is a fact only you know.

### HS codes are suggestions, not facts

An HS code is a **customs declaration**. A wrong one is a legal and financial
problem for you, not a cosmetic defect.

The tool fills it from the product category and labels every one a suggestion.
`config.yaml` → `hs_codes` has each line marked `VERIFY`. Two forks worth a word
with your customs broker before a large run:

- a **stitched** saree is `6211`; an unstitched one is `5007`
- **bed linen** is `6302`; general furnishings are `6304`

### Category slugs are inferred

`taxonomy.yaml` marks fourteen subcategory slugs `derived: true` — worked out
from haat's naming convention rather than read from haat itself. A wrong slug
either rejects the import or files your listing where nobody looks.

**One test import of a single row settles all fourteen.** Do that before you run
two hundred.

---

## 7. What you get

```
runs/j_7fk2m9qa/
├── listings.csv              ← upload this to haat
├── listings_with_images.csv  same rows plus every photo link, for your records
├── review.csv                every row needing a decision, and which cell
├── image_manifest.csv        which photograph belongs to which row
├── failed.csv                URLs that produced nothing, and why
├── images/<row_key>/         the photographs, ready to upload
└── job.json                  the exact settings that run used
```

**Every link you paste ends up in exactly one of those files.** That is checked
at the end of every job, including after you cancel one. A job that cannot say
where a link went refuses to call itself finished.

`listings.csv` is haat's twenty columns exactly, in haat's order, including the
`image_urls` column at the end.

### Cells that come back blank

Blank is a deliberate answer, not a failure:

| blank | why |
|---|---|
| `gi_region` | **always blank.** A GI tag is a government certification and haat makes it a seller declaration. Not ours to assert |
| `length_cm` / `width_cm` / `height_cm` | most product pages do not state them, and a wrong shipping dimension is a wrong freight cost |
| `weight_g` | filled when the page states it, blank when it does not |
| `price_inr` | filled from the page. To set prices yourself instead: `config.yaml` → `price.strategy: blank` |

Everything blank and needed is listed in `review.csv` with its reason, so you
fill it once rather than hunt for it.

---

## 8. When a shop refuses

Some shops refuse any automated client. That is an answer, and this tool takes
it as one rather than disguising itself. Those rows read `refused` or
`timeout_read`.

**Use the Import screen.** Two routes, both producing exactly the row a fetch
would have:

- **A seller export** — `.csv` or `.xlsx` from your own seller panel. Columns
  are auto-mapped, shown to you with sample values, and confirmed before a
  single row is built. Save the mapping once and it reapplies next time.
- **A page you saved** — open the product page, `Ctrl+S`, choose
  *"Webpage, complete"*, drop the `.html` on the Import screen. The photographs
  come with it, so the shop yields its gallery with no request to that shop.

If neither works, `image_urls` is the last column of `listings.csv`. Put the
links in by hand, separated by ` | `. The tool leaves that cell empty rather
than absent for exactly this reason.

---

## 9. When something looks wrong

```bash
haat-lister diagnose "https://the-url-that-went-wrong"
```

It runs the real path and reports every step: which connection attempt answered,
whether the page looked like a product, every photograph it checked and which of
the nine checks each one failed.

It says `— not reached` for checks it did not get to, rather than reporting a
result it does not have. If the page never arrived, it will not claim the shop
has no photographs.

Same thing in the console under **Why no photo?**

---

## 10. Getting help

Send these three and most questions answer themselves:

1. the output of `haat-lister config-check`
2. the `failed.csv` from the run — its `reason` and `time_spent` columns
3. the output of `haat-lister diagnose <one failing url>`

`README.md` in this folder is the full reference: how the image checks work,
what the tool refuses to do and why, how to write a plugin for a shop it gets
wrong.

---

## Known limits, stated plainly

- **Shops that block automated clients cannot be fetched.** By design — no
  CAPTCHA solving, no proxies, no pretending to be a browser we are not. Import
  is the route for those.
- **Dimensions are usually blank**, because pages rarely state them.
- **HS codes and fourteen category slugs need one verification pass** from you.
  Both are flagged; neither is silent.
- **It runs on your machine.** Built to be run locally by one operator. It is
  not a hosted service, and `serve` listens only on `127.0.0.1` unless you
  explicitly tell it otherwise.
