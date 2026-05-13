"""Correspondence Filer — Flask web UI.

Routes:
    GET    /                       — single-page UI
    GET    /oauth/{login,callback,logout}
    GET    /api/labels             — Gmail labels for the dropdown
    GET    /api/picker-config      — API key + OAuth token + app ID for the Picker
    POST   /api/run                — kick off job in background thread, returns job_id
    GET    /api/jobs/<id>/stream   — SSE stream forwarding run_filing_job events
    GET    /api/entities           — saved entities from entities.json
    POST   /api/entities           — save a new entity
    DELETE /api/entities/<id>      — delete a saved entity
"""

from __future__ import annotations

import json
import os
import queue
import tempfile
import threading
import uuid

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, redirect, render_template, request, session, stream_with_context, url_for
from flask_session import Session
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from werkzeug.middleware.proxy_fix import ProxyFix

from filer import SCOPES, list_labels, run_filing_job

load_dotenv()

if os.environ.get("FLASK_ENV") != "production":
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")

CLIENT_SECRETS_FILE = "client_secret_web.json"
# Write client secret from env var if provided (for Railway deployment)
_client_secret_b64 = os.environ.get("GOOGLE_CLIENT_SECRET_B64")
if _client_secret_b64:
    import base64 as _b64
    with open(CLIENT_SECRETS_FILE, "w") as _f:
        _f.write(_b64.b64decode(_client_secret_b64).decode())

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY", "dev-change-me")
app.config["SESSION_TYPE"] = "filesystem"
app.config["SESSION_FILE_DIR"] = ".flask_session"
app.config["SESSION_PERMANENT"] = False
Session(app)

JOBS: dict[str, queue.Queue] = {}
_SENTINEL = object()

ENTITIES_FILE = "entities.json"
_entities_lock = threading.Lock()


def _load_entities() -> list[dict]:
    if not os.path.exists(ENTITIES_FILE):
        return []
    try:
        with open(ENTITIES_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save_entities(entities: list[dict]) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".entities.", suffix=".json", dir=".")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(entities, f, indent=2)
        os.replace(tmp, ENTITIES_FILE)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _build_flow(state: str | None = None) -> Flow:
    return Flow.from_client_secrets_file(
        CLIENT_SECRETS_FILE,
        scopes=SCOPES,
        state=state,
        redirect_uri=url_for("oauth_callback", _external=True),
    )


def current_credentials() -> Credentials | None:
    data = session.get("credentials")
    if not data:
        return None
    creds = Credentials.from_authorized_user_info(json.loads(data), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(GoogleRequest())
        session["credentials"] = creds.to_json()
    return creds


def _gcp_project_number() -> str | None:
    """Picker needs the GCP project number (first segment of the Web client ID)."""
    override = os.environ.get("GOOGLE_APP_ID")
    if override:
        return override
    try:
        with open(CLIENT_SECRETS_FILE) as f:
            client_id = json.load(f)["web"]["client_id"]
        return client_id.split("-", 1)[0]
    except (FileNotFoundError, KeyError, json.JSONDecodeError):
        return None


@app.route("/")
def index():
    return render_template("index.html", signed_in=bool(current_credentials()))


@app.route("/oauth/login")
def oauth_login():
    flow = _build_flow()
    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    session["oauth_state"] = state
    session["oauth_code_verifier"] = flow.code_verifier
    return redirect(auth_url)


@app.route("/oauth/callback")
def oauth_callback():
    flow = _build_flow(state=session.get("oauth_state"))
    flow.code_verifier = session.get("oauth_code_verifier")
    flow.fetch_token(authorization_response=request.url)
    session["credentials"] = flow.credentials.to_json()
    session.pop("oauth_state", None)
    session.pop("oauth_code_verifier", None)
    return redirect(url_for("index"))


@app.route("/oauth/logout")
def oauth_logout():
    session.clear()
    return redirect(url_for("index"))


@app.route("/api/labels")
def api_labels():
    creds = current_credentials()
    if not creds:
        return jsonify({"error": "not_signed_in"}), 401
    return jsonify({"labels": list_labels(creds)})


def _normalize_date(s: str | None) -> str | None:
    if not s:
        return None
    return s.replace("-", "/")


def _run_job(job_id: str, creds: Credentials, params: dict) -> None:
    q = JOBS[job_id]
    try:
        for ev in run_filing_job(creds=creds, **params):
            q.put(ev)
    except Exception as e:
        q.put({"type": "error", "message": f"job crashed: {e}"})
    finally:
        q.put(_SENTINEL)


@app.route("/api/run", methods=["POST"])
def api_run():
    creds = current_credentials()
    if not creds:
        return jsonify({"error": "not_signed_in"}), 401

    body = request.get_json(silent=True) or {}
    label_id = body.get("label_id")
    folder_id = body.get("folder_id")
    if not label_id or not folder_id:
        return jsonify({"error": "label_id and folder_id are required"}), 400

    limit_raw = body.get("limit")
    try:
        limit = int(limit_raw) if limit_raw not in (None, "", 0) else None
    except (TypeError, ValueError):
        return jsonify({"error": "limit must be an integer"}), 400

    params = {
        "label_id": label_id,
        "drive_folder_id": folder_id,
        "before": _normalize_date(body.get("before")),
        "after": _normalize_date(body.get("after")),
        "limit": limit,
        "dry_run": bool(body.get("dry_run")),
        "reupload": bool(body.get("reupload")),
    }

    job_id = uuid.uuid4().hex
    JOBS[job_id] = queue.Queue()
    threading.Thread(target=_run_job, args=(job_id, creds, params), daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/jobs/<job_id>/stream")
def api_job_stream(job_id: str):
    q = JOBS.get(job_id)
    if q is None:
        return jsonify({"error": "unknown_job"}), 404

    @stream_with_context
    def gen():
        while True:
            ev = q.get()
            if ev is _SENTINEL:
                JOBS.pop(job_id, None)
                return
            yield f"data: {json.dumps(ev)}\n\n"

    return Response(
        gen(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/entities", methods=["GET", "POST"])
def api_entities():
    if request.method == "GET":
        return jsonify({"entities": _load_entities()})

    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    label_id = body.get("label_id")
    label_name = body.get("label_name")
    folder_id = body.get("folder_id")
    folder_name = body.get("folder_name")
    if not (name and label_id and folder_id):
        return jsonify({"error": "name, label_id and folder_id are required"}), 400

    entity = {
        "id": uuid.uuid4().hex,
        "name": name,
        "label_id": label_id,
        "label_name": label_name or "",
        "folder_id": folder_id,
        "folder_name": folder_name or "",
    }
    with _entities_lock:
        entities = _load_entities()
        entities.append(entity)
        _save_entities(entities)
    return jsonify({"entity": entity}), 201


@app.route("/api/entities/<entity_id>", methods=["DELETE"])
def api_entity_delete(entity_id: str):
    with _entities_lock:
        entities = _load_entities()
        remaining = [e for e in entities if e.get("id") != entity_id]
        if len(remaining) == len(entities):
            return jsonify({"error": "not_found"}), 404
        _save_entities(remaining)
    return jsonify({"ok": True})


@app.route("/api/picker-config")
def api_picker_config():
    creds = current_credentials()
    if not creds:
        return jsonify({"error": "not_signed_in"}), 401
    api_key = os.environ.get("GOOGLE_API_KEY")
    app_id = _gcp_project_number()
    if not api_key:
        return jsonify({"error": "missing_api_key", "message": "Set GOOGLE_API_KEY in .env"}), 503
    if not app_id:
        return jsonify({"error": "missing_app_id", "message": "Couldn't read project number from client_secret_web.json; set GOOGLE_APP_ID in .env"}), 503
    return jsonify({"api_key": api_key, "access_token": creds.token, "app_id": app_id})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
