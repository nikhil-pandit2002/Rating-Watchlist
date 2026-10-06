# Troubleshooting

Ordered by how often it happens. **Start with step 0** — it separates a broken
install from a blocked network, which look identical from the outside.

---

## 0. Is it the code or the network?

```bash
python tests/test_extreme_names.py
python tests/test_normalize.py
python tests/test_matching.py
```

These run **entirely offline**.

- **All pass** → the install is fine. Every remaining problem is network,
  proxy, or an agency changing its site. Go to section 2.
- **Any fail** → the install or the Python version is wrong. Go to section 1.

---

## 1. Install problems

### `SyntaxError` mentioning `|` or `X | None`

Python is too old. This needs **3.11** (3.10 works, 3.9 does not).

```bash
python --version
```

If several Pythons are installed, be explicit: `py -3.11 -m pip install -r requirements.txt`
then `py -3.11 webapp/app.py`.

### `ModuleNotFoundError: No module named 'rating_scraper'`

Run scripts from the **project root**, not from inside a folder:

```bash
cd final_rating_scrapper
python run_watchlist.py --discover        # right
python tools/check_entity_names.py        # right - it adjusts its own path
```

### `ModuleNotFoundError: No module named 'fitz'` / `flask` / `rapidfuzz`

Dependencies are missing or went to a different Python.

```bash
pip install -r requirements.txt
python -c "import requests, bs4, lxml, pandas, openpyxl, pdfplumber, fitz, rapidfuzz, flask; print('all present')"
```

`fitz` is the import name of **pymupdf** — that is normal, not a mistake.

### pip cannot reach PyPI

Ask for an internal mirror, or install offline from pre-downloaded wheels:

```bash
pip install --no-index --find-links <folder> -r requirements.txt
```

### `pymupdf` is refused on licence grounds (AGPL)

The only package with a licence worth checking. It can be removed with some
rework — `pdfplumber` covers the same ground more slowly. Raise it rather than
working around it.

---

## 2. Network and proxy

### Everything returns "not_rated", or the run is very slow and finds nothing

Almost always the proxy. Test one agency directly:

```bash
python -c "import requests; r=requests.get('https://www.careratings.com', timeout=30); print(r.status_code)"
```

- **Times out / connection refused** → outbound HTTPS is blocked. Give the nine
  hosts in [DEPLOYMENT.md](DEPLOYMENT.md) to whoever manages the proxy.
- **`SSLError` / certificate verify failed** → the proxy inspects TLS. Point
  Python at the corporate root CA:

  ```bash
  set REQUESTS_CA_BUNDLE=C:\path\to\corporate-ca.pem
  ```

  Do **not** disable verification.

- **407 Proxy Authentication Required** → set the proxy variables:

  ```bash
  set HTTPS_PROXY=http://user:pass@proxy.corp:8080
  ```

### Only Infomerics returns nothing

Its PDFs are on **`infomericstorage.blob.core.windows.net`**, not on its own
domain. Corporate proxies often block `*.blob.core.windows.net` wholesale. This
one host has to be allowed separately.

### Only CRISIL misses some entities

`www.crisil.com` is a **different host** from `www.crisilratings.com`. Both are
needed; pinned document URLs use the former.

---

## 3. A rating is missing that you can see on the agency's website

Work through these in order — the QA sheet usually tells you which it is.

### The QA sheet says `outside_age_window`

Working as intended. The rating exists but is older than 15 months, and the rule
blanks it. The row records the exact date rejected. To widen the window:

```bash
python run_watchlist.py --discover --months 24
```

### The QA sheet says `needs_confirmation`

The name matches more than one of the agency's entities, or matches one only
partially, and it will not guess. The candidates are listed on the row. Confirm
once and it is remembered permanently.

### The QA sheet says `not_rated`, but you can see the document

Two known causes:

**(a) The agency does not list the entity in its own search.** Confirmed for
CRISIL on four entities — the document is hosted, but no spelling of the name
returns it from their index or their results endpoint. The document id cannot be
derived, so pin the URL:

```bash
python run_watchlist.py --pin-url "Nxt-Infra Trust" "CRISIL Ratings" "https://www.crisil.com/mnt/…/Nxt_InfraTrust_….html"
```

The agency name must match exactly one of:
`CARE Ratings`, `CRISIL Ratings`, `ICRA`, `India Ratings`, `Brickwork Ratings`,
`Infomerics`, `Acuite Ratings`.

**(b) The agency spells the name differently.** Ask each agency what it calls the
entity:

```bash
python tools/check_entity_names.py
```

It reports, per entity, the closest name in each of the seven indexes. If the
agency's spelling differs, rename the entity on the web page to match — the
match is by name, and the agency's own spelling always wins.

### The QA sheet says `parse_failed`

The agency changed its page or PDF layout. Note the URL on the QA row; the
adapter for that agency needs a look. Other agencies are unaffected.

---

## 4. The numbers look wrong

### Amounts are small integers like 1, 2, 3, 4, 5

A table has been read with its **serial-number column mistaken for the amount**.
This happened once with CARE and is fixed, but it is the signature to watch for.
Compare against the source PDF linked in `Online url`.

### `Instrument Category` is `Other`

Treat it as a **warning, not a category**. In normal operation there are none.
Rows landing in `Other` mean a table was parsed wrongly — the one occurrence
turned ₹12,950 crore into rows of 1 through 5. Do not filter these out; look at
the document.

### NaBFID loan amount looks too low

It should be the **sum of every facility** NaBFID lends on, not the first. If a
document shows three NaBFID lines and the output shows only the largest, that is
a bug worth reporting.

### A rating is attached to the wrong company

The most serious failure. Check `Online url` — it points at the exact document
used. If the document is for a different entity, the match was wrong: record it,
and run in **strict mode** so nothing is accepted without confirmation.

---

## 5. The web page

### It will not start — `Address already in use`

Port 5000 is taken. Change the last line of `webapp/app.py` to another port.

### The page loads but the list is empty

`data/entities.db` is missing or was not copied. It carries the 36 entities.
Check it exists and is around 16 KB.

### The Get Ratings button does nothing

Open the browser console (F12). If requests to `/api/…` are failing, the Flask
process has probably crashed — look at the terminal running `python webapp/app.py`.

### Progress reaches 100% but no file appears

The Excel is written to `data/runs/`. If the folder is missing or not writable,
the run fails at the last step. Check the terminal output.

---

## 6. Keeping its memory

Two files hold everything the tool has learned:

| File | What is lost if it goes missing |
|---|---|
| `data/entities.db` | the watchlist — every entity must be re-added |
| `data/resolutions.db` | every confirmed match and pinned URL — all confirmations must be redone |

**Back these two up.** They are 16 KB and 160 KB. `data/cache/` can be deleted
freely — it only makes the next run slower.
