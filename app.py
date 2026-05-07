import os
import shutil
from hashlib import sha256
from datetime import timedelta
from typing import Any

from flask import Flask, flash, redirect, render_template, request, session, url_for
from flask_session import Session
from garminconnect import Garmin
from werkzeug.middleware.proxy_fix import ProxyFix


app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

is_debug = os.getenv("FLASK_DEBUG", "0") == "1"
default_secure_cookie = not is_debug

session_dir = os.path.join(app.instance_path, "flask_session")
os.makedirs(session_dir, exist_ok=True)
token_root_dir = os.getenv(
    "GARMIN_TOKEN_ROOT", os.path.join(app.instance_path, "garmin_tokens")
)
os.makedirs(token_root_dir, exist_ok=True)

app.config.update(
    SECRET_KEY=os.getenv("FLASK_SECRET_KEY", "change-me-in-production"),
    SESSION_TYPE="filesystem",
    SESSION_FILE_DIR=session_dir,
    SESSION_FILE_THRESHOLD=500,
    SESSION_PERMANENT=True,
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv(
        "SESSION_COOKIE_SECURE", str(default_secure_cookie)
    ).lower()
    == "true",
)
Session(app)


def _token_store_for_email(email: str) -> str:
    """Use a deterministic, non-PII directory for token persistence."""
    email_hash = sha256(email.lower().encode("utf-8")).hexdigest()
    token_store = os.path.join(token_root_dir, email_hash)
    os.makedirs(token_store, exist_ok=True)
    return token_store


def _build_client(email: str, token_store: str, password: str | None = None) -> Garmin:
    """Create an authenticated Garmin client using token store + optional password."""
    if password:
        client = Garmin(email=email, password=password)
    else:
        client = Garmin(email=email)
    client.login(token_store)
    return client


def load_activities(email: str, token_store: str, limit: int = 10) -> list[dict[str, Any]]:
    """Authenticate against Garmin Connect using tokens and return activities."""
    client = _build_client(email=email, token_store=token_store)
    return client.get_activities(0, limit)


@app.get("/")
def index():
    if session.get("garmin_email") and session.get("garmin_token_store"):
        return redirect(url_for("activities"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")

        if not email or not password:
            flash("Please provide both email and password.", "error")
            return render_template("login.html", email=email)

        token_store = _token_store_for_email(email)
        try:
            # First login uses password and writes refresh/access tokens to token_store.
            _build_client(email=email, token_store=token_store, password=password)
        except Exception:
            app.logger.exception("Garmin login failed.")
            flash("Login failed. Please check your credentials and try again.", "error")
            return render_template("login.html", email=email)

        session.permanent = True
        session["garmin_email"] = email
        session["garmin_token_store"] = token_store
        return redirect(url_for("activities"))

    return render_template("login.html", email="")


@app.get("/activities")
def activities():
    email = session.get("garmin_email")
    token_store = session.get("garmin_token_store")
    if not email or not token_store:
        flash("Please log in to view your activities.", "error")
        return redirect(url_for("login"))

    try:
        activities_list = load_activities(email=email, token_store=token_store, limit=10)
    except Exception:
        app.logger.exception("Could not load Garmin activities.")
        session.pop("garmin_token_store", None)
        flash("Could not load activities. Please log in again.", "error")
        return redirect(url_for("login"))

    return render_template("activities.html", activities=activities_list, email=email)


@app.get("/logout")
def logout():
    token_store = session.get("garmin_token_store")
    session.clear()
    if token_store and os.path.isdir(token_store):
        shutil.rmtree(token_store, ignore_errors=True)
    flash("You are now logged out.", "info")
    return redirect(url_for("login"))


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=is_debug)
