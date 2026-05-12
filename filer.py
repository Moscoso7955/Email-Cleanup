"""Correspondence Filer — pull Gmail threads under a label, render each as a PDF,
ask Claude for a topic descriptor, and upload to a Google Drive folder.

Usage (CLI):
    python filer.py --label "13 - Bar Phoebe" --drive-folder-id 1OwWA0... [--before 2026/01/01] [--after 2025/01/01] [--limit 10] [--dry-run]

Usage (library):
    from filer import run_filing_job, list_labels
    for event in run_filing_job(label_id=..., drive_folder_id=...):
        ...
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import sys
from datetime import datetime
from html import escape
from typing import Iterator, Optional

from anthropic import Anthropic
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from xhtml2pdf import pisa


SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/drive",
]
CREDENTIALS_FILE = "credentials.json"
TOKEN_FILE = "token.json"
MANIFEST_FILE = "manifest.jsonl"
SUBFOLDERS = ("Thread PDFs", "Attachments", "Links")
TOPIC_MODEL = "claude-haiku-4-5-20251001"


# ---------- auth ----------

def get_credentials() -> Credentials:
    creds: Optional[Credentials] = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return creds


def _gmail(creds: Credentials):
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _drive(creds: Credentials):
    return build("drive", "v3", credentials=creds, cache_discovery=False)


# ---------- labels ----------

def list_labels(creds: Optional[Credentials] = None) -> list[dict]:
    if creds is None:
        creds = get_credentials()
    resp = _gmail(creds).users().labels().list(userId="me").execute()
    out = []
    for l in resp.get("labels", []):
        if l.get("type") == "system" and l["name"] not in ("INBOX", "STARRED", "IMPORTANT"):
            continue
        out.append({"id": l["id"], "name": l["name"]})
    out.sort(key=lambda x: x["name"].lower())
    return out


# ---------- drive helpers ----------

def find_or_create_child(drive, parent_id: str, name: str) -> str:
    safe = name.replace("\\", "\\\\").replace("'", "\\'")
    q = (
        f"'{parent_id}' in parents and name='{safe}' "
        "and mimeType='application/vnd.google-apps.folder' and trashed=false"
    )
    resp = drive.files().list(q=q, fields="files(id,name)", pageSize=1).execute()
    files = resp.get("files", [])
    if files:
        return files[0]["id"]
    meta = {"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]}
    return drive.files().create(body=meta, fields="id").execute()["id"]


def upload_pdf(drive, folder_id: str, filename: str, pdf_bytes: bytes) -> str:
    media = MediaIoBaseUpload(io.BytesIO(pdf_bytes), mimetype="application/pdf", resumable=False)
    meta = {"name": filename, "parents": [folder_id]}
    return drive.files().create(body=meta, media_body=media, fields="id").execute()["id"]


# ---------- manifest ----------

def load_manifest() -> set[str]:
    if not os.path.exists(MANIFEST_FILE):
        return set()
    ids: set[str] = set()
    with open(MANIFEST_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("thread_id"):
                ids.add(rec["thread_id"])
    return ids


def append_manifest(rec: dict) -> None:
    with open(MANIFEST_FILE, "a") as f:
        f.write(json.dumps(rec) + "\n")


# ---------- gmail ----------

def list_thread_ids(gmail, label_id: str, before: Optional[str], after: Optional[str], limit: Optional[int]) -> list[str]:
    q_parts = []
    if before:
        q_parts.append(f"before:{before}")
    if after:
        q_parts.append(f"after:{after}")
    q = " ".join(q_parts) if q_parts else None

    ids: list[str] = []
    page_token = None
    while True:
        kw = {"userId": "me", "labelIds": [label_id], "maxResults": 100}
        if q:
            kw["q"] = q
        if page_token:
            kw["pageToken"] = page_token
        resp = gmail.users().threads().list(**kw).execute()
        ids.extend(t["id"] for t in resp.get("threads", []))
        if limit and len(ids) >= limit:
            return ids[:limit]
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return ids


def _header(msg: dict, name: str) -> str:
    for h in msg.get("payload", {}).get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def _b64(data: str) -> bytes:
    padding = "=" * ((4 - len(data) % 4) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _extract_body(payload: dict) -> tuple[str, str]:
    text_part = ""
    html_part = ""

    def walk(part):
        nonlocal text_part, html_part
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if mime == "text/plain" and data and not text_part:
            text_part = _b64(data).decode("utf-8", errors="replace")
        elif mime == "text/html" and data and not html_part:
            html_part = _b64(data).decode("utf-8", errors="replace")
        for sub in part.get("parts", []) or []:
            walk(sub)

    walk(payload)
    return text_part, html_part


def _thread_subject(thread: dict) -> str:
    msgs = thread.get("messages", [])
    if not msgs:
        return "(no subject)"
    return _header(msgs[0], "Subject") or "(no subject)"


def _thread_first_date(thread: dict) -> datetime:
    msgs = thread.get("messages", [])
    if not msgs:
        return datetime.now()
    return min(datetime.fromtimestamp(int(m.get("internalDate", "0")) / 1000) for m in msgs)


def _render_thread_html(thread: dict) -> str:
    subject = _thread_subject(thread)
    parts = [
        "<html><head><meta charset='utf-8'/>"
        "<style>body{font-family:Helvetica,Arial,sans-serif;font-size:11pt}"
        "h1{font-size:14pt}.meta{color:#555;font-size:9pt;margin:6pt 0}"
        "hr{border:0;border-top:1px solid #ccc;margin:12pt 0}</style></head><body>",
        f"<h1>{escape(subject)}</h1>",
    ]
    for m in thread.get("messages", []):
        text, html = _extract_body(m.get("payload", {}))
        body = html if html else f"<pre>{escape(text)}</pre>"
        parts.append(
            "<hr/>"
            "<div class='meta'>"
            f"<b>From:</b> {escape(_header(m, 'From'))}<br/>"
            f"<b>To:</b> {escape(_header(m, 'To'))}<br/>"
            f"<b>Date:</b> {escape(_header(m, 'Date'))}"
            "</div>"
            f"<div>{body}</div>"
        )
    parts.append("</body></html>")
    return "".join(parts)


def _html_to_pdf(html: str) -> bytes:
    buf = io.BytesIO()
    result = pisa.CreatePDF(html, dest=buf, encoding="utf-8")
    if result.err:
        raise RuntimeError("xhtml2pdf rendering failed")
    return buf.getvalue()


# ---------- claude ----------

def _ask_topic(client: Anthropic, subject: str, sample_text: str) -> str:
    prompt = (
        "Give a 2-4 word topic descriptor in Title Case summarizing this email thread. "
        "Output ONLY the topic — no quotes, no punctuation, no preamble.\n\n"
        f"Subject: {subject}\n\nBody excerpt:\n{sample_text[:2000]}"
    )
    resp = client.messages.create(
        model=TOPIC_MODEL,
        max_tokens=40,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(getattr(b, "text", "") for b in resp.content).strip()
    text = re.sub(r"[^A-Za-z0-9 \-]", "", text).strip()
    words = text.split()
    return " ".join(words[:4]) if words else "Untitled"


def _safe_filename(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "-", name).strip() or "Untitled"


# ---------- main job ----------

def run_filing_job(
    label_id: str,
    drive_folder_id: str,
    *,
    before: Optional[str] = None,
    after: Optional[str] = None,
    limit: Optional[int] = None,
    dry_run: bool = False,
    creds: Optional[Credentials] = None,
    anthropic_api_key: Optional[str] = None,
) -> Iterator[dict]:
    """Generator yielding progress events. See HANDOFF.md for event schema."""
    try:
        if creds is None:
            creds = get_credentials()
        gmail = _gmail(creds)
        drive = _drive(creds)
        client = Anthropic(api_key=anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY"))

        labels = gmail.users().labels().list(userId="me").execute().get("labels", [])
        label_name = next((l["name"] for l in labels if l["id"] == label_id), label_id)

        if dry_run:
            pdfs_folder = drive_folder_id
        else:
            pdfs_folder = find_or_create_child(drive, drive_folder_id, "Thread PDFs")
            find_or_create_child(drive, drive_folder_id, "Attachments")
            find_or_create_child(drive, drive_folder_id, "Links")

        thread_ids = list_thread_ids(gmail, label_id, before, after, limit)
        yield {"type": "start", "label": label_name, "thread_count": len(thread_ids)}

        filed = load_manifest()
        uploaded = skipped = failed = 0

        for tid in thread_ids:
            try:
                yield {"type": "thread", "status": "processing", "thread_id": tid}

                if tid in filed:
                    skipped += 1
                    yield {"type": "thread", "status": "skipped", "thread_id": tid, "reason": "already filed"}
                    continue

                thread = gmail.users().threads().get(userId="me", id=tid, format="full").execute()
                subject = _thread_subject(thread)
                date_str = _thread_first_date(thread).strftime("%Y-%m-%d")

                sample = ""
                for m in thread.get("messages", []):
                    t, _ = _extract_body(m.get("payload", {}))
                    if t:
                        sample = t
                        break

                topic = _ask_topic(client, subject, sample)
                filename = _safe_filename(f"{date_str} - {topic}.pdf")

                if dry_run:
                    yield {
                        "type": "thread",
                        "status": "planned",
                        "thread_id": tid,
                        "filename": filename,
                        "subject": subject,
                    }
                    continue

                pdf_bytes = _html_to_pdf(_render_thread_html(thread))
                file_id = upload_pdf(drive, pdfs_folder, filename, pdf_bytes)

                append_manifest({
                    "thread_id": tid,
                    "subject": subject,
                    "filename": filename,
                    "drive_file_id": file_id,
                    "filed_at": datetime.utcnow().isoformat(),
                })
                uploaded += 1
                yield {
                    "type": "thread",
                    "status": "uploaded",
                    "thread_id": tid,
                    "filename": filename,
                    "subject": subject,
                    "drive_file_id": file_id,
                }
            except Exception as e:
                failed += 1
                yield {"type": "thread", "status": "error", "thread_id": tid, "message": str(e)}

        yield {"type": "complete", "uploaded": uploaded, "skipped": skipped, "failed": failed}
    except Exception as e:
        yield {"type": "error", "message": str(e)}


# ---------- cli ----------

def _resolve_label_id(creds: Credentials, name: str) -> Optional[str]:
    labels = _gmail(creds).users().labels().list(userId="me").execute().get("labels", [])
    return next((l["id"] for l in labels if l["name"] == name), None)


def main():
    load_dotenv()
    p = argparse.ArgumentParser(description="File Gmail threads to Google Drive as PDFs.")
    p.add_argument("--label", required=True, help="Gmail label name (e.g. '13 - Bar Phoebe')")
    p.add_argument("--drive-folder-id", required=True, help="Target Drive folder ID")
    p.add_argument("--before", help="Gmail date filter (YYYY/MM/DD)")
    p.add_argument("--after", help="Gmail date filter (YYYY/MM/DD)")
    p.add_argument("--limit", type=int, help="Max threads to process")
    p.add_argument("--dry-run", action="store_true", help="Plan without uploading")
    args = p.parse_args()

    creds = get_credentials()
    label_id = _resolve_label_id(creds, args.label)
    if not label_id:
        print(f"Label not found: {args.label}", file=sys.stderr)
        sys.exit(1)

    for ev in run_filing_job(
        label_id=label_id,
        drive_folder_id=args.drive_folder_id,
        before=args.before,
        after=args.after,
        limit=args.limit,
        dry_run=args.dry_run,
        creds=creds,
    ):
        t = ev["type"]
        if t == "start":
            print(f"[start] {ev['label']} — {ev['thread_count']} threads")
        elif t == "thread":
            st = ev["status"]
            if st == "uploaded":
                print(f"  uploaded: {ev['filename']}")
            elif st == "planned":
                print(f"  planned:  {ev['filename']}")
            elif st == "skipped":
                print(f"  skipped:  {ev['thread_id']} ({ev.get('reason','')})")
            elif st == "error":
                print(f"  error:    {ev['thread_id']}: {ev['message']}", file=sys.stderr)
        elif t == "complete":
            print(f"[done] uploaded={ev['uploaded']} skipped={ev['skipped']} failed={ev['failed']}")
        elif t == "error":
            print(f"[fatal] {ev['message']}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
