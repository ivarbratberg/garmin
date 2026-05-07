import os
import re
import shutil
from hashlib import sha256
from datetime import timedelta
from typing import Any

from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for
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


_TIME_METRIC_KEYS = (
    "sumElapsedDuration",
    "timerDurationInSeconds",
    "timerTime",
    "directRunningTimeInSeconds",
    "clock",
    "timestamp",
)


def _format_elapsed_label(seconds: float) -> str:
    sec = max(0, int(round(seconds)))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _series_from_activity_details(details: dict[str, Any]) -> tuple[list[str], list[dict[str, Any]]]:
    """Turn Garmin activity details JSON into x-labels and plottable metric series."""
    descriptors = details.get("metricDescriptors") or []
    samples = details.get("activityDetailMetrics") or []
    if not isinstance(descriptors, list) or not descriptors:
        return [], []
    if not isinstance(samples, list):
        return [], []

    n_desc = len(descriptors)
    columns: list[list[Any]] = [[] for _ in range(n_desc)]

    for sample in samples:
        if not isinstance(sample, dict):
            continue
        mvals = sample.get("metrics")
        if not isinstance(mvals, list):
            continue
        for i in range(min(n_desc, len(mvals))):
            columns[i].append(mvals[i])

    time_idx: int | None = None
    for preferred in _TIME_METRIC_KEYS:
        for i, desc in enumerate(descriptors):
            if isinstance(desc, dict) and desc.get("key") == preferred:
                time_idx = i
                break
        if time_idx is not None:
            break
    if time_idx is None:
        for i, desc in enumerate(descriptors):
            if not isinstance(desc, dict):
                continue
            key = (desc.get("key") or "").lower()
            if any(t in key for t in ("elapsed", "timer", "clock", "timestamp")):
                time_idx = i
                break

    labels: list[str] = []
    if time_idx is not None and columns[time_idx]:
        raw_times = columns[time_idx]
        for v in raw_times:
            if v is None:
                labels.append("")
                continue
            try:
                fv = float(v)
            except (TypeError, ValueError):
                labels.append(str(v))
                continue
            if fv > 1e6:
                fv = fv / 1000.0
            labels.append(_format_elapsed_label(fv))
    else:
        n_rows = max((len(c) for c in columns), default=0)
        labels = [str(i) for i in range(n_rows)]

    metrics_out: list[dict[str, Any]] = []
    for i, desc in enumerate(descriptors):
        if not isinstance(desc, dict):
            continue
        if i == time_idx:
            continue
        key = desc.get("key") or f"metric_{i}"
        unit = ""
        u = desc.get("unit")
        if isinstance(u, dict):
            unit = u.get("key") or u.get("factor") or ""
        data_raw = columns[i] if i < len(columns) else []
        data: list[float | None] = []
        for v in data_raw:
            if v is None:
                data.append(None)
                continue
            try:
                data.append(float(v))
            except (TypeError, ValueError):
                data.append(None)

        if not any(x is not None for x in data):
            continue

        readable = re.sub(r"([a-z])([A-Z])", r"\1 \2", key)
        readable = readable.replace("_", " ").strip()
        label = readable[:1].upper() + readable[1:] if readable else key
        unit_suffix = f" ({unit})" if unit else ""
        metrics_out.append(
            {
                "key": key,
                "label": f"{label}{unit_suffix}",
                "unit": unit or None,
                "data": data,
            }
        )

    row_count = max(
        len(labels),
        max((len(m["data"]) for m in metrics_out), default=0),
    )
    if len(labels) < row_count:
        labels = labels + [str(i) for i in range(len(labels), row_count)]
    elif len(labels) > row_count:
        labels = labels[:row_count]

    for m in metrics_out:
        d = m["data"]
        if len(d) > row_count:
            m["data"] = d[:row_count]
        elif len(d) < row_count:
            m["data"] = d + [None] * (row_count - len(d))

    return labels, metrics_out


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


@app.get("/api/activities/<int:activity_id>/chart-data")
def activity_chart_data(activity_id: int):
    email = session.get("garmin_email")
    token_store = session.get("garmin_token_store")
    if not email or not token_store:
        return jsonify({"error": "unauthorized"}), 401

    try:
        client = _build_client(email=email, token_store=token_store)
        summary = client.get_activity(str(activity_id))
        details = client.get_activity_details(str(activity_id), maxchart=4000, maxpoly=4000)
    except Exception:
        app.logger.exception("Could not load activity chart data.")
        return jsonify({"error": "garmin_error"}), 502

    title = summary.get("activityName") or f"Activity {activity_id}"
    labels, metrics = _series_from_activity_details(details)
    if not metrics:
        return jsonify(
            {
                "activityId": activity_id,
                "title": title,
                "labels": labels,
                "metrics": [],
                "message": "No time-series metrics available for this activity.",
            }
        )

    return jsonify(
        {
            "activityId": activity_id,
            "title": title,
            "labels": labels,
            "metrics": metrics,
        }
    )


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
