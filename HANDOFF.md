# Correspondence Filer — Handoff

Tool that pulls Gmail threads under a given label, renders each as a PDF, asks
Claude for a 2–4 word topic descriptor, and uploads to a Google Drive folder
named `YYYY-MM-DD - Topic.pdf`. One Drive folder per "entity" (Bar Phoebe,
Sundown, FWPM, Tipsy, ...). Drive layout under each target folder:
`Thread PDFs/`, `Attachments/`, `Links/`.

## Plan

1. **filer.py** — CLI + importable library (`run_filing_job`). DONE.
2. **app.py** — Flask web app with label dropdown, Google Picker for the
   target folder, SSE progress stream. NEXT.
3. **entities.json** — saved `{name, label, folder_id}` per venue so we don't
   re-pick each time. AFTER 2.

## Step 1 — what exists

- `filer.py` — CLI and library.
- `requirements.txt` — `anthropic`, `google-api-python-client`,
  `google-auth-oauthlib`, `python-dotenv`, `xhtml2pdf`.
- `.env.example` — Anthropic key template.
- `.gitignore` — excludes `.env`, `credentials.json`, `token.json`,
  `manifest.jsonl`, `.venv/`, `__pycache__/`.

### CLI

```
python filer.py \
  --label "13 - Bar Phoebe" \
  --drive-folder-id 1OwWA0... \
  [--before 2026/01/01] [--after 2025/01/01] \
  [--limit 10] [--dry-run]
```

### Library

```python
from filer import run_filing_job, list_labels

for event in run_filing_job(
    label_id=...,
    drive_folder_id=...,
    before=None, after=None, limit=None,
    dry_run=False,
    creds=None,                # uses get_credentials() if None
    anthropic_api_key=None,    # falls back to ANTHROPIC_API_KEY env
):
    ...
```

### Event schema (yielded by `run_filing_job`)

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

### Auth + scopes

- OAuth scopes: `gmail.readonly` + `drive` (full Drive, chosen so we can
  `find_or_create_child` on existing user folders).
- Current `get_credentials()` uses `InstalledAppFlow.run_local_server(port=0)`
  (Desktop OAuth client) — replace with a Web OAuth flow for step 2.

### PDF rendering

`xhtml2pdf` (pure Python, no native deps). One PDF per thread; all messages
concatenated with `<hr/>` separators.

### Manifest

`manifest.jsonl` (gitignored). One JSON record per uploaded thread:
`{thread_id, subject, filename, drive_file_id, filed_at}`. Used to skip
already-filed threads on re-run.

## Step 2 — Flask web app (next)

Endpoints:

| Method/Path | Purpose |
|---|---|
| `GET /` | Single-page UI |
| `GET /api/labels` | List Gmail labels (`filer.list_labels`) |
| `GET /api/picker-config` | Google API key + OAuth token for Picker JS |
| `POST /api/run` | Kick off job in background thread, return `job_id` |
| `GET /api/jobs/<id>/stream` | SSE stream forwarding `run_filing_job` events |

In-memory `{job_id: queue.Queue}` for SSE fan-out — fine for single-user
local. No DB.

### OAuth changes required for step 2

- Add a **Web** OAuth client in the same GCP project (Desktop won't work for
  Picker).
- Add an OAuth callback handler in `app.py`; store creds in a server-side
  session.
- Google Picker needs both an OAuth token (with `drive` scope) and a Google
  API key (created in the same GCP project, restricted to the Picker API).

## Step 3 — multi-entity UX (after step 2)

`entities.json` next to the script:

```json
[
  {"name": "Bar Phoebe", "label": "13 - Bar Phoebe", "folder_id": "1OwWA0..."},
  {"name": "Sundown",    "label": "Sundown",         "folder_id": "1XyZ..."}
]
```

Top of UI becomes a "Saved entities" dropdown that auto-fills label + folder.
"+ Add entity" uses the Picker to capture a new one.

## Deployment

- Develop in Codespace.
- Deploy to Render or Fly.io free tier.
- Secrets (`credentials.json`, `ANTHROPIC_API_KEY`) live in env vars, never
  in the repo.
