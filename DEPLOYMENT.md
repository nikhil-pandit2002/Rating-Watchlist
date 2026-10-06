# Deployment notes — restricted / corporate laptop

What this tool needs in order to run, and what it does **not** need. Written for
handing the project to someone on a managed machine where installs and outbound
traffic are controlled.

---

## 1. No third-party API is used

There is **no paid API, no vendor SDK, no API key, no account, and no login**
anywhere in this system. Nothing needs to be provisioned or expensed.

Every rating is read from the public pages the seven agencies already publish,
using the same endpoints a browser uses. A search of the source for
`api_key`, `secret`, `password`, `bearer`, `authorization` returns nothing.

The only token handled is Acuite's per-session CSRF token, which their own page
issues to any visitor and which the adapter reads from the landing page at the
start of each run. It is not a credential and is not stored.

Everything the tool has learned is kept in local SQLite files under `data/`.
No data leaves the machine.

---

## 2. Python

**Python 3.11** (developed and tested on 3.11.2). 3.10+ should work; 3.9 will
not, because the code uses `X | None` type syntax.

### Packages

| Package | Version used | Why it is needed |
|---|---|---|
| `requests` | 2.31.0 | all HTTP |
| `beautifulsoup4` | 4.12.3 | parsing agency HTML |
| `lxml` | 5.1.0 | the HTML parser BeautifulSoup uses |
| `pandas` | 2.2.2 | reading input sheets, writing output |
| `openpyxl` | 3.1.5 | the Excel engine pandas writes through |
| `pdfplumber` | 0.11.0 | table extraction from rating PDFs |
| `pymupdf` | 1.28.0 | faster PDF text/link extraction (imports as `fitz`) |
| `rapidfuzz` | 3.14.3 | company-name matching |
| `flask` | 3.1.2 | the web front end only |

```bash
pip install -r requirements.txt
```

**All nine are pure-PyPI, MIT/BSD/LGPL-style licences, and widely used.** None
require a compiler on Windows — every one ships a prebuilt wheel.

Two notes for a locked-down machine:

- **`pymupdf` is AGPL-licensed.** Most institutions are fine with this for
  internal use, but it is the one licence worth checking with your team. If it
  is refused, `pdfplumber` alone can do the job with some rework — say so and it
  can be removed.
- If PyPI is blocked, ask for an internal mirror (`pip install -i <mirror>`) or
  have the nine wheels downloaded once and installed offline with
  `pip install --no-index --find-links <folder> -r requirements.txt`.

Everything else the code imports is Python standard library: `sqlite3`, `re`,
`json`, `logging`, `threading`, `concurrent.futures`, `dataclasses`, `pathlib`,
`hashlib`, `datetime`, `urllib`, `io`, `os`, `sys`, `uuid`, `unicodedata`.

---

## 3. Network access required

Outbound **HTTPS (443)** to these hosts. This is the list to give whoever
manages the proxy or firewall — the counts are real fetches from a full run, so
the list is complete rather than guessed.

| Host | What it serves |
|---|---|
| `www.careratings.com` | CARE search + rating PDFs |
| `www.indiaratings.co.in` | India Ratings search API + press releases |
| `www.crisilratings.com` | CRISIL listing JSON + rationales |
| `www.crisil.com` | CRISIL documents reached by a pinned URL |
| `www.icra.in` | ICRA rationales |
| `infomericstorage.blob.core.windows.net` | **Infomerics rating PDFs** (Azure blob) |
| `cms.infomerics.com` | Infomerics company index |
| `connect.acuite.in` | Acuite search + rating details |
| `www.brickworkratings.com` | Brickwork press releases |

Two of these are easy to miss and will silently cost you ratings if blocked:

- **`infomericstorage.blob.core.windows.net`** — Infomerics hosts its actual PDFs
  on Azure blob storage, not on its own domain. Corporate proxies often block
  `*.blob.core.windows.net` wholesale.
- **`www.crisil.com`** is a different host from `www.crisilratings.com`. Both
  are needed.

**A proxy is fine** — `requests` honours the standard `HTTPS_PROXY` /
`HTTP_PROXY` environment variables, so no code change is needed. If the proxy
does TLS interception, set `REQUESTS_CA_BUNDLE` to the corporate root CA bundle
rather than disabling verification.

No inbound access is needed. The web app binds to `127.0.0.1:5000` — it is
reachable only from that machine and is not exposed on the network.

---

## 4. What is stored locally

| Path | What it is | Safe to copy? |
|---|---|---|
| `data/entities.db` | the watchlist (36 entities) | yes — copy it to keep the list |
| `data/resolutions.db` | confirmed entity pins + pinned document URLs | yes — copy it to keep confirmations |
| `data/cache/` | cached HTTP responses; several hundred MB | optional — deleting it only makes the next run slower |
| `data/runs/` | past Excel outputs | optional |
| `output/`, `input/` | working files | optional |

To move the tool with its memory intact, copy the source plus
`data/entities.db` and `data/resolutions.db`. Everything else rebuilds itself.

---

## 5. Running it

```bash
pip install -r requirements.txt

# web front end -> http://127.0.0.1:5000
python webapp/app.py

# or headless
python run_watchlist.py --discover --months 15 --output output/ratings.xlsx
```

Check the install without touching the network:

```bash
python tests/test_extreme_names.py
python tests/test_normalize.py
python tests/test_matching.py
```

All three run entirely offline. If they pass, the install is sound and any
remaining problem is network policy, not the code.

---

## 6. Politeness and load

Requests are throttled per host (default ~1.2s apart) and the seven agencies are
worked in parallel, one thread each — so no single site sees a burst. Responses
are cached on disk, so re-runs mostly hit the cache rather than the agency.
A full 36-entity run is a few thousand requests spread over several minutes,
which is ordinary browsing traffic.

Nothing here bypasses a paywall, a login, or a CAPTCHA, and no crawler
identifies itself as a search engine.
