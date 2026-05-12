# Correspondence Filer — Handoff

Tool that pulls Gmail threads under a given label, renders each as a PDF, asks
Claude for a 2–4 word topic descriptor, and uploads to a Google Drive folder
named `YYYY-MM-DD - Topic.pdf`. One Drive folder per "entity" (Bar Phoebe,
Sundown, FWPM, Tipsy, ...). Drive layout under each target folder:
`Thread PDFs/`, `Attachments/`, `Links/`.

## Status

- **Step 1** — `filer.py` CLI + library. **Done.**
- **Step 2** — Flask web app (`app.py`, `templates/`, `static/`) with label
  dropdown, Google Picker, SSE progress. **Done.**
- **Step 3** — Saved entities (`entities.json`) and a dropdown that
  auto-fills label + folder. **Done.**

## Files

```
filer.py                 CLI + library
app.py                   Flask app
templates/index.html     UI
static/app.js            client logic
static/app.css           styling
requirements.txt
.env.example
.gitignore               ignores secrets, manifest.jsonl, entities.json,
                         .flask_session/
HANDOFF.md
```

## Setup

### Google Cloud Console

Same GCP project for both OAuth clients.

1. Enable APIs: Gmail API, Google Drive API, **Google Picker API**.
2. **Web OAuth 2.0 client**:
   - Authorized JavaScript origins: `http://localhost:5000` (plus any
     forwarded Codespace / hosted URL).
   - Authorized redirect URIs: `http://localhost:5000/oauth/callback`
     (plus equivalents).
   - Download to repo root as `client_secret_web.json` (gitignored).
3. **API key** (Credentials → Create credentials → API key):
   - Restrict: API restrictions → "Google Picker API" only.
   - HTTP referrer restrictions: `http://localhost:5000/*` (plus equivalents).
4. OAuth consent screen: add your Gmail as a test user. Scopes already
   needed: `gmail.readonly`, `drive`.

### Local

```sh
cp .env.example .env       # fill in ANTHROPIC_API_KEY, FLASK_SECRET_KEY, GOOGLE_API_KEY
pip install -r requirements.txt
python app.py              # http://localhost:5000
```

Optional override: `GOOGLE_APP_ID=<gcp project number>` if the project
number can't be auto-extracted from `client_secret_web.json`.

## CLI

```
python filer.py \
  --label "13 - Bar Phoebe" \
  --drive-folder-id 1OwWA0... \
  [--before 2026/01/01] [--after 2025/01/01] \
  [--limit 10] [--dry-run]
```

## Library entry points

```python
from filer import run_filing_job, list_labels, SCOPES

for event in run_filing_job(
    label_id=..., drive_folder_id=...,
    before=None, after=None, limit=None,
    dry_run=False, creds=None, anthropic_api_key=None,
):
    ...
```

### Event schema yielded by `run_filing_job`

```
{"type": "start",    "label": str, "thread_count": int}
{"type": "thread",   "status": "processing", "thread_id": str}
{"type": "thread",   "status": "planned",   "thread_id": str, "filename": str, "subject": str}
{"type": "thread",   "status": "uploaded",  "thread_id": str, "filename": str, "subject": str, "drive_file_id": str}
{"type": "thread",   "status": "skipped",   "thread_id": str, "reason": str}
{"type": "thread",   "status": "error",     "thread_id": str, "message": str}
{"type": "complete", "uploaded": int, "skipped": int, "failed": int}
{"type": "error",    "message": str}   # fatal
```

## Flask routes

| Method/Path | Purpose |
|---|---|
| `GET /` | Single-page UI |
| `GET /oauth/login` | Start Google OAuth |
| `GET /oauth/callback` | Finish OAuth, store creds in filesystem session |
| `GET /oauth/logout` | Clear session |
| `GET /api/labels` | List Gmail labels |
| `GET /api/picker-config` | API key + access token + GCP project number for the Picker |
| `POST /api/run` | Kick off job; returns `{job_id}` |
| `GET /api/jobs/<id>/stream` | SSE stream of `run_filing_job` events |
| `GET /api/entities` | List saved entities |
| `POST /api/entities` | Save a new entity |
| `DELETE /api/entities/<id>` | Delete a saved entity |

In-memory `JOBS: dict[job_id, queue.Queue]` for SSE fan-out. Sessions live
on disk in `.flask_session/`. Entities live in `entities.json` (atomic
write via tempfile + `os.replace`, threading lock around writes).

## Storage files (all gitignored)

| File | Purpose |
|---|---|
| `client_secret_web.json` | Web OAuth client config |
| `credentials.json` | Desktop OAuth client (CLI only) |
| `token.json` | CLI-side cached token |
| `.flask_session/` | Server-side Flask sessions (web OAuth creds) |
| `manifest.jsonl` | One JSON record per uploaded thread — used to skip re-filing |
| `entities.json` | `[{id, name, label_id, label_name, folder_id, folder_name}, ...]` |

## PDF rendering

`xhtml2pdf` — pure Python, no native deps. One PDF per thread, messages
concatenated with `<hr/>` separators.

## Topic model

`claude-haiku-4-5-20251001` — Haiku is plenty for a 2–4 word topic from a
subject + body excerpt and keeps cost low.

## Deployment notes

- Develop in Codespace; deploy to Render or Fly.io free tier.
- Secrets (`client_secret_web.json`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`,
  `FLASK_SECRET_KEY`) live in env vars; never check them in.
- The dev server's threaded SSE is fine for single-user local. For a
  production deploy use gunicorn with a gevent/eventlet worker so SSE
  streams don't tie up a sync worker per client.
