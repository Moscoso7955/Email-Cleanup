"""Correspondence Filer — Flask web UI.

Module 1: OAuth (Web client) + session-backed credentials.
Routes:
    GET  /                 — landing page (sign-in or signed-in shell)
    GET  /oauth/login      — start Google OAuth flow
    GET  /oauth/callback   — finish OAuth, store creds in session
    GET  /oauth/logout     — clear session
"""

from __future__ import annotations

import json
import os

from dotenv import load_dotenv
from flask import Flask, redirect, render_template, request, session, url_for
from flask_session import Session
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

from filer import SCOPES

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
    return Credentials.from_authorized_user_info(json.loads(data), SCOPES)


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


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
