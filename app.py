"""Correspondence Filer — Flask web UI.

Routes:
    GET  /                  — single-page UI
    GET  /oauth/{login,callback,logout}
    GET  /api/labels        — Gmail labels for the dropdown
    GET  /api/picker-config — API key + OAuth token + app ID for Google Picker
"""

from __future__ import annotations

import json
import os

from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from flask_session import Session
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

from filer import SCOPES, list_labels

load_dotenv()

if os.environ.get("FLASK_ENV") != "production":
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")

CLIENT_SECRETS_FILE = "client_secret_web.json"

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY", "dev-change-me")
app.config["SESSION_TYPE"] = "filesystem"
app.config["SESSION_FILE_DIR"] = ".flask_session"
app.config["SESSION_PERMANENT"] = False
Session(app)


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
    return redirect(auth_url)


@app.route("/oauth/callback")
def oauth_callback():
    flow = _build_flow(state=session.get("oauth_state"))
    flow.fetch_token(authorization_response=request.url)
    session["credentials"] = flow.credentials.to_json()
    session.pop("oauth_state", None)
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
    app.run(host="127.0.0.1", port=5000, debug=True)
