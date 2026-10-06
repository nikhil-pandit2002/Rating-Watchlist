"""Flask front end for the rating collector.

    python webapp/app.py          ->  http://127.0.0.1:5000

The page is built around one persistent watchlist rather than ad-hoc searches:
entities are added once and every later run picks them up automatically.

    GET    /                        the page
    GET    /api/entities            the watchlist, with pin status per agency
    POST   /api/entities            add one (name is the only required field)
    POST   /api/entities/upload     add many from a spreadsheet
    PATCH  /api/entities/<id>       rename / retype / activate / deactivate
    DELETE /api/entities/<id>       remove
    GET    /api/template            a one-column starter sheet
    GET    /api/agencies            which agencies are wired up
    POST   /api/jobs                collect ratings for the chosen entities
    GET    /api/jobs/<id>           status / progress / QA
    GET    /api/jobs/<id>/result    download the Excel output
"""
from __future__ import annotations

import io
import logging
import os
import sys
from pathlib import Path

import pandas as pd
from flask import Flask, jsonify, request, send_file, send_from_directory

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from rating_scraper.agencies import ADAPTERS  # noqa: E402
from rating_scraper.entities import ENTITY_TYPES, EntityStore, guess_entity_type  # noqa: E402
from rating_scraper.models import Company  # noqa: E402

from jobs import JobManager  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("webapp")

STATIC = Path(__file__).resolve().parent / "static"
app = Flask(__name__, static_folder=str(STATIC), static_url_path="/static")
# Flask caches /static for 12 hours by default. Because the page always lives at
# the same address, a browser that has visited an earlier build serves the old
# app.js from cache against fresh HTML - the entity list silently never renders,
# with no error anywhere to explain it. There is one user on localhost, so
# caching buys nothing and costs a support call.
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0


@app.after_request
def _no_store(response):
    if request.path.startswith("/static") or request.path == "/":
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response

entities = EntityStore(ROOT / "data" / "entities.db")
jobs = JobManager(ROOT / "data")
# The same store the runner uses, so a pin confirmed in either place is seen
# by both.
resolutions = jobs.resolutions

# Column headings people actually use for the one field that matters.
NAME_COLUMNS = ("entity name", "company name", "borrower name", "name",
                "borrower", "company", "entity")


@app.get("/")
def index():
    return send_from_directory(STATIC, "index.html")


@app.get("/api/agencies")
def agencies():
    return jsonify([{"key": k, "name": c.name} for k, c in ADAPTERS.items()])


# -- watchlist ----------------------------------------------------------
def _entity_json(e) -> dict:
    pins = resolutions.get("", e.name)
    confirmed = resolutions.confirmed("", e.name)
    return {
        "id": e.id,
        "name": e.name,
        "type": e.entity_type,
        "active": e.active,
        "notes": e.notes,
        "agencies_pinned": sorted(pins),
        "agencies_confirmed": sorted(confirmed),
    }


@app.get("/api/entities")
def list_entities():
    rows = [_entity_json(e) for e in entities.list()]
    return jsonify({
        "entities": rows,
        "types": list(ENTITY_TYPES),
        "active": sum(1 for r in rows if r["active"]),
    })


@app.post("/api/entities")
def add_entity():
    body = request.get_json(silent=True) or {}
    name = str(body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "a name is required"}), 400
    e, status = entities.add(name, str(body.get("type") or ""))
    if status == "invalid":
        return jsonify({"error": f"{name!r} is not a usable entity name"}), 400
    return jsonify({"entity": _entity_json(e), "status": status})


@app.post("/api/entities/upload")
def upload_entities():
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "no file uploaded"}), 400
    try:
        if f.filename.lower().endswith((".csv", ".txt")):
            df = pd.read_csv(f, dtype=str)
        else:
            df = pd.read_excel(f, dtype=str)
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"could not read that file: {e}"}), 400

    lookup = {str(c).strip().lower(): c for c in df.columns}
    col = next((lookup[k] for k in NAME_COLUMNS if k in lookup), None)
    if col is None:
        # A single-column sheet needs no header at all - take what is there.
        col = df.columns[0] if len(df.columns) == 1 else None
    if col is None:
        return jsonify({
            "error": "no name column found - expected one of: "
                     + ", ".join(NAME_COLUMNS)
        }), 400

    names = [str(v).strip() for v in df[col] if str(v).strip()
             and str(v).strip().lower() not in ("nan", "none")]
    result = entities.add_many(names)
    return jsonify({
        "added": len(result["added"]),
        "already_present": len(result["exists"]),
        "reactivated": len(result["reactivated"]),
        "invalid": len(result["invalid"]),
        "names_added": result["added"][:50],
    })


@app.patch("/api/entities/<int:entity_id>")
def patch_entity(entity_id: int):
    body = request.get_json(silent=True) or {}
    fields = {k: v for k, v in body.items() if k in ("name", "entity_type", "notes", "active")}
    e = entities.update(entity_id, **fields)
    if not e:
        return jsonify({"error": "no such entity"}), 404
    return jsonify({"entity": _entity_json(e)})


@app.delete("/api/entities/<int:entity_id>")
def delete_entity(entity_id: int):
    return jsonify({"deleted": entities.delete(entity_id)})


@app.get("/api/template")
def template():
    buf = io.BytesIO()
    pd.DataFrame({"Entity Name": [
        "Cube Highways Trust",
        "Greater Chennai Corporation",
        "Maple Infrastructure Trust",
    ]}).to_excel(buf, index=False)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name="entity_template.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# -- runs ---------------------------------------------------------------
@app.post("/api/jobs")
def create_job():
    body = request.get_json(silent=True) or {}
    ids = body.get("entity_ids")
    if ids:
        chosen = [e for e in (entities.get(int(i)) for i in ids) if e]
    else:
        chosen = entities.list(active_only=True)
    if not chosen:
        return jsonify({"error": "no entities selected"}), 400

    wanted = body.get("agencies") or list(ADAPTERS)
    unknown = [a for a in wanted if a not in ADAPTERS]
    if unknown:
        return jsonify({"error": f"unknown agencies: {unknown}"}), 400

    companies = [Company(borrower_name=e.name) for e in chosen]
    job = jobs.submit(companies, wanted, strict=bool(body.get("strict")))
    return jsonify(job.to_dict()), 202


@app.get("/api/jobs/<job_id>")
def job_status(job_id: str):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "no such job"}), 404
    return jsonify(job.to_dict())


@app.get("/api/jobs/<job_id>/result")
def job_result(job_id: str):
    job = jobs.get(job_id)
    if not job or not job.output_path or not Path(job.output_path).exists():
        return jsonify({"error": "no result for that job"}), 404
    return send_file(job.output_path, as_attachment=True,
                     download_name=f"ratings_{job_id[:8]}.xlsx")


def _quiet_console() -> None:
    """Print one clear banner instead of Flask's developer output.

    Started normally, Flask prints a red 'This is a development server' warning
    and Werkzeug logs every request. Both are correct for a public deployment
    and misleading here: this serves one person on 127.0.0.1, and the red text
    reads as a failure to someone who has just installed the tool - it has
    already been reported as an error once.

    Routine retry notices are quietened for the same reason. An agency
    answering 502 once and succeeding on retry is normal, but 'WARNING ... 502'
    on the console looks like something broke. Progress and results are in the
    web page, which is where a user is actually looking.
    """
    import flask.cli

    flask.cli.show_server_banner = lambda *a, **k: None
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    logging.getLogger("rating_scraper").setLevel(logging.ERROR)
    logging.getLogger("webapp").setLevel(logging.ERROR)


if __name__ == "__main__":
    # A cloud host assigns the port and expects the process to listen on all
    # interfaces; a laptop wants neither. Both read from the environment, so the
    # same file serves locally and when deployed without a second code path.
    port = int(os.environ.get("PORT", "5000"))
    host = os.environ.get("HOST", "127.0.0.1")

    _quiet_console()
    count = len(entities.list(active_only=True))
    print()
    print("  Rating Watchlist")
    print(f"  {count} entities loaded")
    print()
    print(f"  Open your browser at:   http://{'127.0.0.1' if host == '0.0.0.0' else host}:{port}")
    print()
    print("  Keep this window open while you use it.")
    print("  Press Ctrl+C here to stop.")
    print()
    app.run(host=host, port=port, debug=False, threaded=True)
