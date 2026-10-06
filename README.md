---
title: Rating Watchlist
emoji: "📊"
colorFrom: green
colorTo: gray
sdk: gradio
app_file: app.py
pinned: false
license: mit
short_description: Credit ratings from 7 agencies, NaBFID flagged
---

> **Running here as a live demo.** The instructions below describe installing it
> on a Windows machine; on this Space it is already running — open the app tab
> and press **Get Ratings**. A full 36-entity run makes 252 requests to seven
> external websites and takes several minutes, so filtering to one or two
> entities first gives a quicker result.

# Rating Watchlist

Collects the **latest credit rating** for a fixed list of entities from **seven
SEBI-registered rating agencies**, and flags where **NaBFID** appears as a lender.

Built for the entities a rating vendor does not cover — trusts, InvITs, municipal
corporations, foundations and project SPVs — which until now were looked up by
hand. It arrives with **36 entities already loaded**, and more can be added at
any time from the web page.

| | |
|---|---|
| Agencies | CARE, CRISIL, ICRA, India Ratings, Brickwork, Infomerics, Acuite |
| Output | Excel — one row per rated instrument, plus a QA sheet |
| Age rule | ratings older than **15 months** are reported blank, not stale |
| Cost | **none** — no paid API, no key, no account, no login |

---

# QUICK START — the two-click version

Built for **Python 3.13 on 64-bit Windows**, which is what is installed on the
target machine. Everything needed is already in this folder; no internet is
required to install.

1. Copy this folder somewhere local, e.g. `C:\Users\<you>\Documents\`.
   Avoid running it from inside a zip, a network drive, or a OneDrive-synced
   folder — file locking there can interrupt a run.
2. Double-click **`INSTALL.bat`** — once only. It finds Python, installs the
   packages from `wheels\` without touching the internet, and runs the built-in
   checks. Takes a couple of minutes.
3. Double-click **`START.bat`** — your browser opens at
   <http://127.0.0.1:5000>. Keep the black window open while you use it;
   closing it stops the application.

If `INSTALL.bat` reports that Python was not found, install **Python 3.13
(64-bit)** from <https://www.python.org/downloads/windows/>, **tick "Add
python.exe to PATH"** on the first screen, and run it again.

Everything below is the manual equivalent, plus how to use and troubleshoot it.


# PART 1 — Installing it manually

Allow about 20 minutes. Steps 1–5 are the install; step 6 proves it works.

## Step 1 — Check whether Python is already there

Open **Command Prompt** (press `Win`, type `cmd`, press Enter) and run:

```bat
python --version
```

**You need 3.10 or newer.** The bundled packages are built for **3.13**.

| What you see | What to do |
|---|---|
| `Python 3.11.x` or `3.10.x`/`3.12.x`/`3.13.x` | Good — go to Step 3 |
| `Python 3.9.x` or older | Too old. Do Step 2 |
| `'python' is not recognized...` | Not installed. Do Step 2 |
| The Microsoft Store opens | It is a placeholder, not Python. Do Step 2 |

## Step 2 — Install Python (only if Step 1 said you need it)

Download **Python 3.11** or newer from <https://www.python.org/downloads/windows/>.

> On a managed laptop the installer may be blocked. If so, raise a request with
> IT for "Python 3.11 (64-bit)" — it is a standard, freely distributable
> developer tool.

While installing, **tick "Add python.exe to PATH"** on the first screen. This is
the single most common cause of the tool not starting later.

Close Command Prompt, open a new one, and repeat Step 1.

## Step 3 — Put the folder somewhere sensible

Copy the whole `final_rating_scrapper` folder to a normal working location, for
example:

```
C:\Users\<your-name>\Documents\final_rating_scrapper
```

Avoid running it straight from a zip file, a network drive, or a folder that
syncs to OneDrive/SharePoint — file locking on synced folders can interrupt a
run midway.

## Step 4 — Open Command Prompt inside that folder

In File Explorer, open the folder, click the **address bar**, type `cmd` and
press Enter. A Command Prompt opens already pointed at the folder.

Check you are in the right place:

```bat
dir
```

You should see `README.md`, `requirements.txt`, `run_watchlist.py`, and the
`src`, `webapp`, `data` and `tests` folders.

## Step 5 — Install the packages it needs

Try the normal way first:

```bat
pip install -r requirements.txt
```

**If that works, go to Step 6.** If it fails — in particular with:

```
ERROR: Could not find a version that satisfies the requirement requests>=2.31
ERROR: No matching distribution found for requests>=2.31
```

then use **Step 5b** instead. Despite how it reads, that message almost never
means the package is missing. It is what pip says when it **cannot reach the
internet at all** — a blocked or proxied corporate network. Nothing is wrong
with the tool or your Python.

## Step 5b — Offline install (no internet needed)

Everything required is already in the `wheels\` folder, so this works on a
laptop with no access to PyPI:

```bat
pip install --no-index --find-links wheels -r requirements-lock.txt
```

That is the whole fix. It installs 33 packages — the nine listed below plus
their dependencies — from files already on disk.

Two details worth knowing:

- **`requirements-lock.txt`, not `requirements.txt`.** The lock file pins the
  exact versions this build was tested against. Left unpinned, pip resolves
  `pandas>=2.2` to pandas 3.0, a major version ahead of what was verified.
- **The bundle is built for Python 3.11, 64-bit Windows.** If `python --version`
  showed something else, the install will complain about unsupported wheels. In
  that case either install Python 3.11, or ask whoever sent you the folder to
  rebuild the bundle with:
  `pip download -r requirements-lock.txt -d wheels --only-binary=:all: --platform win_amd64 --python-version 312`

> If your organisation runs an internal PyPI mirror, that also works and keeps
> you on current versions:
> `pip install --index-url https://<internal-mirror>/simple -r requirements.txt`

**No compiler is needed** either way — every package ships a ready-built Windows
binary.

## Step 6 — Prove the install works, without touching the internet

```bat
python tests\test_extreme_names.py
python tests\test_normalize.py
python tests\test_matching.py
```

Expect to see:

```
FAILURES: 0
OK - 45 assertions passed
OK - 38 checks
```

These run **entirely offline**. If they pass, your installation is sound. That
matters: from here on, anything that fails is a network or firewall issue, not
a broken install — and those are fixed by completely different people.

## Step 7 — Start the application

```bat
python webapp\app.py
```

You will see:

```
watchlist: 36 active entities
 * Running on http://127.0.0.1:5000
```

Open a browser at **<http://127.0.0.1:5000>**.

**Leave the Command Prompt window open** — closing it stops the application.
To stop it deliberately, click that window and press `Ctrl+C`.

---

# PART 2 — Using it

## The web page

**Add an entity** — type the name in the box and press **Add**. The name is the
only thing required; the type (InvIT, Trust, Municipal Body…) is worked out for
you and shown as a badge.

**Upload a list** — a spreadsheet with a column of names. A header of
`Entity Name`, `Company Name`, `Borrower Name`, `Name`, `Company` or `Entity`
is recognised, and a single-column sheet works even with no header at all.
There is a **Download template** link on the page.

**Get Ratings** — collects for every ticked entity across all seven agencies.
The button shows how many are selected. A progress bar reports which agency and
entity is in flight; when it finishes, a **Download Excel** button appears.

**The list is permanent.** An entity added today is included in every future run
without re-uploading anything.

> A full 36-entity run takes several minutes and contacts the agencies live.
> To try the flow quickly, type something in the filter box, tick one or two
> entities, and run those.

## Without the web page

```bat
python run_watchlist.py --discover --months 15 --output output\ratings.xlsx
```

Occasionally an agency publishes a rating but leaves the entity out of its own
search. When you have the document's web address, record it once:

```bat
python run_watchlist.py --pin-url "Nxt-Infra Trust" "CRISIL Ratings" "https://www.crisil.com/...html"
python run_watchlist.py --list-pins
```

The agency name must be exactly one of: `CARE Ratings`, `CRISIL Ratings`,
`ICRA`, `India Ratings`, `Brickwork Ratings`, `Infomerics`, `Acuite Ratings`.

---

# PART 3 — Reading the output

One row per rated instrument, 15 columns:

`Borrower Name · CIN · Open Charges · Rating Date · Rating Agency Name ·
Instrument Category · Instrument Details · Amount · Rating · Development ·
Outlook · Online url · NaBFID Name · Loan Amount · Unit`

- **NaBFID Name** — `Yes` / `No`. Taken from the agency's lender annexure, not
  from body text, so a passing mention of NaBFID cannot produce a false `Yes`.
- **Loan Amount** — NaBFID's exposure, **summed across every facility** it lends
  on, not just the first line.
- **Online url** — the exact document the row came from. Start here whenever a
  number looks wrong.
- **A blank is never silent.** Every entity with no rating gets a line in the
  **QA sheet** explaining why: `not_rated`, `outside_age_window` (with the date
  that was rejected), `needs_confirmation`, `parse_failed`, and so on.

`output\watchlist_final.xlsx` is a completed run, included as a reference.

---

# PART 4 — Installation problems

Full detail in [TROUBLESHOOTING.md](TROUBLESHOOTING.md). The common ones:

**`'python' is not recognized`**
Python is missing, or "Add python.exe to PATH" was not ticked. Reinstall with
that box ticked, or use `py` instead of `python` in every command.

**`SyntaxError` mentioning `|`**
Python is older than 3.10. Check with `python --version`.

**`ModuleNotFoundError: No module named 'rating_scraper'`**
You are in the wrong folder. `cd` into `final_rating_scrapper` and try again.

**`ModuleNotFoundError: No module named 'fitz'`**
`fitz` is the import name of the `pymupdf` package — that is normal, not a typo.
Re-run `pip install -r requirements.txt`.

**`Could not find a version that satisfies the requirement requests>=2.31`**
The commonest error on a corporate laptop, and the most misleading. pip cannot
reach PyPI; the package is not missing. Use the offline install in **Step 5b**:
`pip install --no-index --find-links wheels -r requirements-lock.txt`

**`... is not a supported wheel on this platform`** (during the offline install)
The bundle in `wheels\` is built for Python 3.11 on 64-bit Windows and your
Python is a different version. Check with `python --version`, then either
install 3.11 or have the bundle rebuilt for your version — the command is in
Step 5b.

**Everything returns "not_rated"**
The offline tests pass but nothing is found — outbound HTTPS is blocked. Give
your IT team the host list in [DEPLOYMENT.md](DEPLOYMENT.md). Two are easy to
miss: `infomericstorage.blob.core.windows.net` (where Infomerics keeps its PDFs)
and `www.crisil.com` (a different host from `www.crisilratings.com`).

**`Address already in use` on starting the web page**
Something else is on port 5000. Either close it, or edit the last line of
`webapp\app.py` and change `port=5000` to `port=5050`.

---

# PART 5 — Back it up

Two small files hold everything the tool has learned:

| File | Size | What is lost without it |
|---|---|---|
| `data\entities.db` | 16 KB | the watchlist — every entity must be re-added |
| `data\resolutions.db` | 135 KB | every confirmed match and pinned document URL |

Copy those two anywhere safe. `data\cache\` appears on first use and can be
deleted freely — it only makes the next run slower.

---

# PART 6 — Project structure

```
final_rating_scrapper/
├── INSTALL.bat                double-click once to set up
├── START.bat                  double-click to run
├── README.md                  this file
├── DEPLOYMENT.md              for IT: hosts to allow, packages, licences
├── TROUBLESHOOTING.md         what breaks, how to tell, how to fix
├── requirements.txt           the nine packages (online install)
├── requirements-lock.txt      exact tested versions (offline install)
├── wheels/                    all 33 packages, for a laptop with no PyPI access
├── run_watchlist.py           command-line runs + URL pinning
│
├── data/                      the tool's memory - BACK THIS UP
│   ├── entities.db            the watchlist (36 entities)
│   ├── resolutions.db         confirmed matches + pinned document URLs
│   ├── cache/                 cached downloads (created on first run)
│   └── runs/                  Excel files produced by the web page
│
├── src/rating_scraper/
│   ├── models.py              core data structures + output columns
│   ├── normalize.py           name matching, rating/amount/date parsing
│   ├── entities.py            the watchlist store
│   ├── resolutions.py         confirmed pins + document URL pins
│   ├── pipeline.py            orchestration, 15-month rule, strict mode
│   ├── http_client.py         throttling, retries, on-disk cache
│   ├── pdf_utils.py           PDF text and table extraction
│   ├── excel_export.py        the workbook writer
│   └── agencies/              one adapter per agency + base.py
│
├── webapp/                    app.py, jobs.py, static/
├── tests/                     run these first - all work offline
└── tools/                     diagnostics (see TROUBLESHOOTING.md)
```

---

# PART 7 — How it decides two names are the same company

This is where the risk lives, so it is worth understanding.

Agencies spell the same entity many ways, and different entities alike. The
matcher handles, with a test for each:

- **closed-up compounds** — CRISIL writes `Oriental Infratrust` for
  `Oriental Infra Trust`
- **separators** — `Nxt_InfraTrust`, `Pimpri-Chinchwad`
- **suffix forms** — `Pvt. Ltd.` = `Private Limited`
- **acronym tails** — `(SMC)` is dropped, but `(Bawana)` is kept, because there
  the bracket *is* the identity
- **the agencies' own typos** — India Ratings indexes `CORPORTION`
- **plurals** — `Blooming Blossoms` for `Blooming Blossom`

And refuses to merge things that only look alike:

```
MSIDC Nashik Region      vs  MSIDC Nashik Region II      85  → not accepted
Pune Ring Road Eastern   vs  Pune Ring Road Western      66  → not accepted
Clean Solar (Baniyana)   vs  Clean Solar (Bhadla)        59  → not accepted
Huoban Energy 1          vs  Huoban Energy 11            50  → not accepted
```

**Where it cannot be sure, it produces no row rather than a guess** — the entity
appears in the QA sheet as `needs_confirmation`, listing the candidates.

---

# PART 8 — Known limits

- **Coverage is 35 of the 36 entities.** The last, *Vidya Vikas Education Trust*,
  is rated — but every rating is older than 15 months, so the rule blanks it.
- **Some ratings are absent from an agency's own search.** CRISIL hosts
  documents for entities its index does not list. Use `--pin-url`.
- **Location and Open Charges are not collected.** Agencies do not publish them;
  they come from MCA registry data.
- **Most of these entities have no CIN.** Trusts, InvITs and civic bodies are not
  registered under the Companies Act, so a confirmed name is the only identity
  anchor available for them.
- **Only CRISIL has been back-tested in depth.** Every bug found so far came from
  comparing output against real documents. The other six agencies have not had
  the same scrutiny — spot-checking amounts against the linked source documents
  is the most useful thing a new user can do.
