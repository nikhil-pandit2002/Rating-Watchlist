"""Gradio front end, for the Hugging Face Space.

A second interface over the same engine, not a second implementation. Hugging
Face charges for Docker Spaces, which is what a Flask app would need, so the
free tier is reached through Gradio instead. Nothing under src/rating_scraper
imports a web framework, so both front ends call the identical pipeline and
neither knows about the other.

The Flask app in webapp/ remains the one that runs on a laptop; this one exists
so the project can be opened and tried from a link.
"""
from __future__ import annotations

import sys
import tempfile
from datetime import date
from pathlib import Path

import gradio as gr
import pandas as pd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from rating_scraper.agencies import ADAPTERS  # noqa: E402
from rating_scraper.entities import EntityStore  # noqa: E402
from rating_scraper.excel_export import write_workbook  # noqa: E402
from rating_scraper.http_client import Fetcher  # noqa: E402
from rating_scraper.models import Company  # noqa: E402
from rating_scraper.pipeline import Pipeline, months_before  # noqa: E402
from rating_scraper.resolutions import ResolutionStore  # noqa: E402

entities = EntityStore(ROOT / "data" / "entities.db")
CUTOFF = months_before(date.today(), 15)

# A Space is shared and its disk is wiped on restart, so a run here is a
# demonstration rather than the recurring collection the laptop build does.
# Capping it keeps one visitor from holding every agency connection for several
# minutes while another waits.
MAX_LIVE = 6


def entity_choices() -> list[str]:
    return [e.name for e in entities.list(active_only=True)]


def watchlist_table() -> pd.DataFrame:
    rows = []
    for e in entities.list(active_only=True):
        rows.append({"Entity": e.name, "Type": e.entity_type})
    return pd.DataFrame(rows)


def sample_output() -> pd.DataFrame:
    """The committed run, so the page shows real results before anything runs."""
    path = ROOT / "output" / "watchlist_final.xlsx"
    if not path.exists():
        return pd.DataFrame({"note": ["no sample run committed"]})
    df = pd.read_excel(path, sheet_name="Ratings").fillna("")
    keep = ["Borrower Name", "Rating Date", "Rating Agency Name",
            "Instrument Category", "Rating", "Outlook", "Amount",
            "NaBFID Name", "Loan Amount", "Online url"]
    return df[[c for c in keep if c in df.columns]]


def collect(names: list[str], progress=gr.Progress()):
    """Run the real pipeline over the selected entities."""
    if not names:
        return (pd.DataFrame({"note": ["Select at least one entity."]}),
                None, "Nothing selected.")
    if len(names) > MAX_LIVE:
        return (pd.DataFrame({"note": [f"Select at most {MAX_LIVE} entities here."]}),
                None, f"This demo runs at most {MAX_LIVE} entities at a time.")

    fetch = Fetcher(cache_dir=ROOT / "data" / "cache", delay=1.2)
    adapters = [ADAPTERS[k](fetch) for k in ADAPTERS]
    resolutions = ResolutionStore(ROOT / "data" / "resolutions.db")

    total = len(names) * len(adapters)
    progress(0, desc=f"0 / {total} lookups")

    def tick(done: int, _total: int, label: str) -> None:
        progress(done / max(total, 1), desc=f"{done} / {total} · {label}")

    result = Pipeline(adapters, resolutions=resolutions,
                      progress=tick, max_age_months=15).run(
        [Company(borrower_name=n) for n in names])

    out = Path(tempfile.gettempdir()) / "rating_watchlist_run.xlsx"
    write_workbook(result, out)

    if not result.records:
        reasons = {q.status for q in result.qa}
        return (pd.DataFrame({"note": ["No current rating found."],
                              "why": [", ".join(sorted(reasons)) or "unknown"]}),
                str(out),
                f"0 of {len(names)} entities returned a rating. "
                f"The QA sheet in the download explains each one.")

    df = pd.DataFrame([{
        "Borrower": r.borrower_name,
        "Date": r.rating_date,
        "Agency": r.agency,
        "Category": r.instrument_category,
        "Rating": r.rating,
        "Outlook": r.outlook,
        "Amount": r.amount,
        "NaBFID": "Yes" if r.nabfid_name else "No",
        "NaBFID loan": r.loan_amount,
        "Source": r.url,
    } for r in result.records])

    found = len({r.borrower_name for r in result.records})
    nab = len({r.borrower_name for r in result.records if r.nabfid_name})
    return df, str(out), (
        f"{found} of {len(names)} entities returned a rating · "
        f"{len(result.records)} rows · {nab} name NaBFID as a lender · "
        f"ratings older than {CUTOFF} are reported blank.")


with gr.Blocks(title="Rating Watchlist", theme=gr.themes.Soft()) as demo:
    gr.Markdown(
        "# Rating Watchlist\n"
        "Collects the **latest credit rating** for infrastructure borrowers from "
        "**seven SEBI-registered rating agencies** — CARE, CRISIL, ICRA, India "
        "Ratings, Brickwork, Infomerics and Acuite — and flags where **NaBFID** "
        "appears as a lender.\n\n"
        "Built for trusts, InvITs, municipal corporations and foundations: the "
        "segment commercial rating vendors do not cover, which was previously "
        "looked up by hand across seven websites."
    )

    with gr.Tab("Run a collection"):
        gr.Markdown(
            f"Pick up to **{MAX_LIVE}** entities and press Collect. Each one is "
            "searched across all seven agencies live, so allow a minute or two.\n\n"
            "Where two similarly-named entities match equally well, the tool "
            "**reports nothing rather than guessing** — the reason is recorded in "
            "the QA sheet of the download."
        )
        picker = gr.CheckboxGroup(
            choices=entity_choices(),
            value=entity_choices()[:2],
            label="Entities",
        )
        go = gr.Button("Collect ratings", variant="primary")
        summary = gr.Markdown()
        results = gr.Dataframe(label="Ratings", wrap=True)
        download = gr.File(label="Excel (Ratings · QA · Summary)")
        go.click(collect, inputs=picker, outputs=[results, download, summary])

    with gr.Tab("Sample output"):
        gr.Markdown(
            "A completed run over the full 36-entity watchlist, committed to the "
            "repository. Every row links to the agency document it came from."
        )
        gr.Dataframe(value=sample_output(), wrap=True)

    with gr.Tab("The watchlist"):
        gr.Markdown(f"{len(entity_choices())} entities, by inferred type.")
        gr.Dataframe(value=watchlist_table(), wrap=True)

    gr.Markdown(
        "---\n"
        "Ratings are read from each agency's own public disclosure documents. "
        "A rating older than 15 months is reported blank rather than stale.\n\n"
        "Developed by Nikhil Pandit · "
        "[source](https://github.com/nikhil-pandit2002/Rating-Watchlist)"
    )

if __name__ == "__main__":
    demo.queue().launch()
