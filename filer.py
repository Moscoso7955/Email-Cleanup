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
import time
from datetime import datetime
from html import escape
from typing import Iterator, Optional

from anthropic import Anthropic, APIStatusError
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

def _patch_reportlab_colors():
        try:
                    import reportlab.lib.colors as _rl_colors
                    _orig_toColor = _rl_colors.toColor
                    def _safe_toColor(arg, default=None):
                                    try:
                                                        return _orig_toColor(arg, default)
        except (AssertionError, Exception):
                            import sys
                            print(f"[color-patch] dropping bad color: {arg!r}", file=sys.stderr)
                            return _rl_colors.white
                    _rl_colors.toColor = _safe_toColor
        try:
                        import xhtml2pdf.util as _x2p_util
                        _x2p_util.toColor = _safe_toColor
except Exception:
            pass
except Exception:
        pass
_patch_reportlab_colors()

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
    resp = drive.files().list(
                q=q,
                fields="files(id,name)",
                pageSize=1,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
                corpora="allDrives",
    ).execute()
    files = resp.get("files", [])
    if files:
                return files[0]["id"]
    meta = {"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]}
    return drive.files().create(body=meta, fields="id", supportsAllDrives=True).execute()["id"]

def upload_pdf(drive, folder_id: str, filename: str, pdf_bytes: bytes) -> str:
        media = MediaIoBaseUpload(io.BytesIO(pdf_bytes), mimetype="application/pdf", resumable=False)
    meta = {"name": filename, "parents": [folder_id]}
    return drive.files().create(body=meta, media_body=media, fields="id", supportsAllDrives=True).execute()["id"]

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

_HEX8_RE = re.compile(r"#([0-9a-fA-F]{6})[0-9a-fA-F]{2}\b")
_HEX4_RE = re.compile(r"#([0-9a-fA-F])([0-9a-fA-F])([0-9a-fA-F])[0-9a-fA-F]\b")
_RGBA_RE = re.compile(r"rgba?\([^)]+\)", re.IGNORECASE)
_HSLA_RE = re.compile(r"hsla?\([^)]*\)", re.IGNORECASE)
_VAR_RE  = re.compile(r"var\(\s*--[^)]*\)", re.IGNORECASE)
_CURRENTCOLOR_RE = re.compile(r"\bcurrentcolor\b", re.IGNORECASE)

def _hsla_to_hex(match: re.Match) -> str:
        inner = match.group(0)
    cleaned = re.sub(r'\bnone\b', '0', inner, flags=re.IGNORECASE)
    nums = re.findall(r"[\d.]+", cleaned)
    if len(nums) < 3:
                return "inherit"
    try:
                h = float(nums[0]) / 360.0
        s_raw = nums[1]
        l_raw = nums[2]
        s = float(s_raw.rstrip("%")) / (100.0 if "%" in s_raw or float(s_raw) > 1 else 1.0)
        l = float(l_raw.rstrip("%")) / (100.0 if "%" in l_raw or float(l_raw) > 1 else 1.0)
        h = max(0.0, min(1.0, h))
        s = max(0.0, min(1.0, s))
        l = max(0.0, min(1.0, l))
except Exception:
        return "inherit"
    import colorsys
    r, g, b = colorsys.hls_to_rgb(h, l, s)
    return "#{:02x}{:02x}{:02x}".format(int(r * 255), int(g * 255), int(b * 255))

def _rewrite_css_colors(text: str) -> str:
        text = re.sub(r'\[\w[^\]]*\]', '', text)
    text = _HEX8_RE.sub(r"#\1", text)
    text = _HEX4_RE.sub(r"#\1\1\2\2\3\3", text)
    def _rgba_to_rgb(m: re.Match) -> str:
                inner = m.group(0)
        nums = re.findall(r"[\d.]+", inner)
        if len(nums) < 3:
                        return "inherit"
        try:
                        r = int(float(nums[0].rstrip("%")) * (2.55 if "%" in inner.split(",")[0] else 1))
            g = int(float(nums[1].rstrip("%")) * (2.55 if "%" in inner.split(",")[1] else 1))
            b = int(float(nums[2].rstrip("%")) * (2.55 if len(inner.split(",")) > 2 and "%" in inner.split(",")[2] else 1))
            r, g, b = max(0,min(255,r)), max(0,min(255,g)), max(0,min(255,b))
            return f"rgb({r}, {g}, {b})"
except Exception:
            return "inherit"
    text = _RGBA_RE.sub(_rgba_to_rgb, text)
    text = _HSLA_RE.sub(_hsla_to_hex, text)
    text = _VAR_RE.sub("inherit", text)
    text = _CURRENTCOLOR_RE.sub("inherit", text)
    _SAFE_COLOR_RE = re.compile(
                r'((?:^|;)\s*(?:color|background-color|border-color|border-[a-z-]*-color)\s*:\s*)'
                r'(?!#[0-9a-fA-F]{3,8}\b|rgb\(|rgba\(|[a-zA-Z]+\b)',
                re.MULTILINE | re.IGNORECASE
    )
    def _strip_bad_color(m: re.Match) -> str:
                return m.group(1) + 'inherit'
    text = _SAFE_COLOR_RE.sub(_strip_bad_color, text)
    text = re.sub(r'(?i)\b((?:border(?:-[a-z]+)?-width|outline-width|outline(?:-width)?|line-height|letter-spacing|word-spacing|padding(?:-[a-z]+)?|margin(?:-[a-z]+)?)\s*:\s*)(?:medium|thick|thin|auto|none|normal|initial|unset|revert|inherit|small|large|x-large|xx-large|smaller|larger)', r'\g<1>0px', text, flags=re.MULTILINE)
    text = re.sub(r'(?i)((?:^|;)\s*(?:color|background-color|border-color|border-[a-z]*-color)\s*:\s*)(?:medium|thick|thin|auto|none|normal|initial|unset|revert|small|large|x-large|xx-large|smaller|larger)(\s*(?:;|$))', r'\1inherit\2', text, flags=re.MULTILINE)
    return text

def _strip_table_layout_attrs(html: str) -> str:
        """Remove table/cell width and padding attrs that cause ReportLab negative availWidth."""
    def strip_layout_attrs(m: re.Match) -> str:
                tag = m.group(0)
        tag = re.sub(r'\s+(?:width|height|cellpadding|cellspacing|valign|align)\s*=\s*(?:"[^"]*"|\'[^\']*\'|\S+)', '', tag, flags=re.IGNORECASE)
        return tag
    html = re.sub(r'<(?:table|tr|td|th)\b[^>]*>', strip_layout_attrs, html, flags=re.IGNORECASE)

    def strip_cell_style(m: re.Match) -> str:
                tag_open = m.group(1)
        style_val = m.group(2)
        rest = m.group(3)
        style_val = re.sub(
                        r'(?i)\b(?:width|min-width|max-width|padding(?:-top|-right|-bottom|-left)?)\s*:[^;]*(;|$)',
                        '',
                        style_val
        )
        style_val = style_val.strip().strip(';')
        if style_val:
                        return f'{tag_open} style="{style_val}"{rest}'
else:
            return f'{tag_open}{rest}'

    html = re.sub(
                r'(<(?:td|th)\b[^>]*?)\s+style="([^"]*)"([^>]*>)',
                strip_cell_style,
                html,
                flags=re.IGNORECASE
    )
    return html


def _sanitize_message_html(html: str) -> str:
        """Rewrite unsupported CSS values so xhtml2pdf accepts them."""
    def style_block(m: re.Match) -> str:
                return ""

    def style_attr_dq(m: re.Match) -> str:
                return f'style="{_rewrite_css_colors(m.group(1))}"'

    def style_attr_sq(m: re.Match) -> str:
                return f"style='{_rewrite_css_colors(m.group(1))}'"

    html = re.sub(r"<style([^>]*)>(.*?)</style>", style_block, html, flags=re.IGNORECASE | re.DOTALL)
    html = re.sub(r'style="([^"]*)"', style_attr_dq, html, flags=re.IGNORECASE)
    html = re.sub(r"style='([^']*)'", style_attr_sq, html, flags=re.IGNORECASE)
    html = re.sub(r' bgcolor=(?:"[^"]*"|[^"\' ][^ >]*)', '', html, flags=re.IGNORECASE)
    html = re.sub(r' background=(?:"[^"]*"|[^"\' ][^ >]*)', '', html, flags=re.IGNORECASE)
    html = _strip_table_layout_attrs(html)
    return html

def _render_thread_html(thread: dict) -> str:
        subject = _thread_subject(thread)
    parts = [
                "<html><head><meta charset='utf-8'/>"
                "<style>body{font-family:Helvetica,Arial,sans-serif;font-size:11pt}"
                "h1{font-size:14pt}.meta{color:#555;font-size:9pt;margin:6pt 0}"
                "hr{border:0;border-top:1px solid #ccc;margin:12pt 0}"
                "table{width:100%;table-layout:auto;border-collapse:collapse}"
                "td,th{padding:2pt;word-break:break-word}"
                "</style></head><body>",
                f"<h1>{escape(subject)}</h1>",
    ]
    for m in thread.get("messages", []):
                text, html = _extract_body(m.get("payload", {}))
        body = _sanitize_message_html(html) if html else f"<pre>{escape(text)}</pre>"
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

def _render_thread_plaintext_html(thread: dict) -> str:
        """Fallback: render thread as plain-text only HTML (no tables, no complex CSS)."""
    subject = _thread_subject(thread)
    parts = [
                "<html><head><meta charset='utf-8'/>"
                "<style>body{font-family:Helvetica,Arial,sans-serif;font-size:11pt}"
                "h1{font-size:14pt}.meta{color:#555;font-size:9pt;margin:6pt 0}"
                "hr{border:0;border-top:1px solid #ccc;margin:12pt 0}"
                "pre{white-space:pre-wrap;word-break:break-word;font-size:9pt}"
                "</style></head><body>",
                f"<h1>{escape(subject)}</h1>",
    ]
    for m in thread.get("messages", []):
                text, html = _extract_body(m.get("payload", {}))
        if not text and html:
                        text = re.sub(r'<[^>]+>', ' ', html)
            text = re.sub(r'&nbsp;', ' ', text)
            text = re.sub(r'&lt;', '<', text)
            text = re.sub(r'&gt;', '>', text)
            text = re.sub(r'&amp;', '&', text)
            text = re.sub(r'[ \t]+', ' ', text)
            text = re.sub(r'\n{3,}', '\n\n', text)
        parts.append(
                        "<hr/>"
                        "<div class='meta'>"
                        f"<b>From:</b> {escape(_header(m, 'From'))}<br/>"
                        f"<b>To:</b> {escape(_header(m, 'To'))}<br/>"
                        f"<b>Date:</b> {escape(_header(m, 'Date'))}"
                        "</div>"
                        f"<pre>{escape(text or '(no body)')}</pre>"
        )
    parts.append("</body></html>")
    return "".join(parts)

def _html_to_pdf(html: str) -> bytes:
        buf = io.BytesIO()
    pisa.CreatePDF(html, dest=buf, encoding="utf-8")
    data = buf.getvalue()
    if not data:
                raise RuntimeError("xhtml2pdf produced empty output")
    return data

def _thread_to_pdf(thread: dict) -> bytes:
        """Render thread to PDF, falling back to plain-text if rich HTML causes layout errors."""
    html = _render_thread_html(thread)
    try:
                return _html_to_pdf(html)
except Exception as e:
        err_str = str(e)
        if "availWidth" in err_str or "PmlTable" in err_str or "PmlKeepInFrame" in err_str:
                        import sys
            print(f"[pdf-fallback] rich HTML failed ({err_str[:120]}), retrying plain-text", file=sys.stderr)
            return _html_to_pdf(_render_thread_plaintext_html(thread))
        raise

# ---------- claude ----------

def _ask_topic(client: Anthropic, subject: str, sample_text: str) -> str:
        prompt = (
                    "Give a 2-4 word topic descriptor in Title Case summarizing this email thread. "
                    "Output ONLY the topic — no quotes, no punctuation, no preamble.\n\n"
                    f"Subject: {subject}\n\nBody excerpt:\n{sample_text[:2000]}"
        )
    last_err: Optional[Exception] = None
    for attempt in range(5):
                try:
                                resp = client.messages.create(
                                    model=TOPIC_MODEL,
                                    max_tokens=40,
                                    messages=[{"role": "user", "content": prompt}],
                )
            text = "".join(getattr(b, "text", "") for b in resp.content).strip()
            text = re.sub(r"[^A-Za-z0-9 \-]", "", text).strip()
            words = text.split()
            return " ".join(words[:4]) if words else "Untitled"
except APIStatusError as e:
            last_err = e
            if e.status_code in (429, 500, 502, 503, 504, 529):
                                time.sleep(2 ** attempt)
                continue
            raise
    raise last_err if last_err else RuntimeError("topic request failed")

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
        reupload: bool = False,
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

                if tid in filed and not reupload:
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

                pdf_bytes = _thread_to_pdf(thread)
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
