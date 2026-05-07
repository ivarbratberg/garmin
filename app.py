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


def is_running_activity(activity: dict[str, Any]) -> bool:
    type_key = (activity.get("activityType") or {}).get("typeKey") or ""
    return "running" in type_key.lower()


def _activity_average_hr(activity: dict[str, Any]) -> float | None:
    for key in ("averageHR", "averageHeartRate", "avgHr", "avgHeartRate"):
        raw = activity.get(key)
        if raw is None:
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return None


def _hr_intensity_factor(hr: float, threshold_hr: float) -> float:
    """Relative intensity vs lactate-threshold HR, clamped to avoid wild values."""
    if threshold_hr <= 0:
        return 0.0
    return max(0.35, min(1.55, hr / threshold_hr))


def estimate_running_rtss(
    activity: dict[str, Any],
    threshold_hr: int | None,
    *,
    details: dict[str, Any] | None = None,
) -> float | None:
    """Approximate running stress on the TSS scale from HR vs threshold HR.

    Uses the common TSS-style duration weighting (IF² × hours × 100). This is an
    estimate only; official TrainingPeaks rTSS uses pace, not HR.
    """
    if threshold_hr is None or threshold_hr <= 0:
        return None
    if not is_running_activity(activity):
        return None

    duration_s = activity.get("duration")
    if duration_s is None:
        return None
    try:
        duration_f = float(duration_s)
    except (TypeError, ValueError):
        return None
    if duration_f <= 0:
        return None

    thr = float(threshold_hr)

    if details:
        stream_tss = _rtss_from_hr_stream(details, thr, duration_f)
        if stream_tss is not None:
            return stream_tss

    avg_hr = _activity_average_hr(activity)
    if avg_hr is None:
        return None

    IF = _hr_intensity_factor(avg_hr, thr)
    return (duration_f / 3600.0) * IF * IF * 100.0


def _rtss_from_hr_stream(
    details: dict[str, Any],
    threshold_hr: float,
    duration_s: float,
) -> float | None:
    """Integrate IF² over time using directHeartRate samples when present."""
    descriptors = details.get("metricDescriptors") or []
    samples = details.get("activityDetailMetrics") or []
    if not isinstance(descriptors, list) or not isinstance(samples, list):
        return None

    hr_idx: int | None = None
    time_idx: int | None = None
    for i, desc in enumerate(descriptors):
        if not isinstance(desc, dict):
            continue
        key = desc.get("key") or ""
        if key in ("directHeartRate", "heartRate"):
            hr_idx = i
        if key in _TIME_METRIC_KEYS:
            time_idx = i
    if hr_idx is None:
        return None

    if time_idx is None:
        for i, desc in enumerate(descriptors):
            if not isinstance(desc, dict):
                continue
            k = (desc.get("key") or "").lower()
            if any(t in k for t in ("elapsed", "timer", "clock")):
                time_idx = i
                break

    times: list[float] = []
    hrs: list[float] = []
    for sample in samples:
        if not isinstance(sample, dict):
            continue
        mvals = sample.get("metrics")
        if not isinstance(mvals, list):
            continue
        if hr_idx >= len(mvals):
            continue
        raw_hr = mvals[hr_idx]
        if raw_hr is None:
            continue
        try:
            hr = float(raw_hr)
        except (TypeError, ValueError):
            continue

        if time_idx is not None and time_idx < len(mvals):
            raw_t = mvals[time_idx]
            if raw_t is not None:
                try:
                    t = float(raw_t)
                    if t > 1e6:
                        t = t / 1000.0
                    times.append(t)
                    hrs.append(hr)
                    continue
                except (TypeError, ValueError):
                    pass
        times.append(float(len(times)))
        hrs.append(hr)

    if len(hrs) < 3:
        return None

    t0 = times[0]
    pairs = [(times[i] - t0, hrs[i]) for i in range(len(times))]

    total_if2_dt = 0.0
    for i in range(len(pairs) - 1):
        t1, hr1 = pairs[i]
        t2, hr2 = pairs[i + 1]
        dt = t2 - t1
        if dt <= 0:
            continue
        hr_mid = (hr1 + hr2) / 2.0
        IF = _hr_intensity_factor(hr_mid, threshold_hr)
        total_if2_dt += IF * IF * dt

    last_t = pairs[-1][0]
    if duration_s > last_t + 1.0:
        IF = _hr_intensity_factor(pairs[-1][1], threshold_hr)
        total_if2_dt += IF * IF * (duration_s - last_t)

    return (total_if2_dt / 3600.0) * 100.0


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

    threshold_hr = session.get("threshold_hr")
    use_hr_stream = os.getenv("RTSS_USE_ACTIVITY_STREAM", "0").lower() in (
        "1",
        "true",
        "yes",
    )
    rtss_map: dict[Any, float | None] = {}
    stream_client: Garmin | None = None
    for a in activities_list:
        aid = a.get("activityId")
        if aid is None:
            continue
        details: dict[str, Any] | None = None
        if use_hr_stream and threshold_hr and is_running_activity(a):
            try:
                if stream_client is None:
                    stream_client = _build_client(email=email, token_store=token_store)
                details = stream_client.get_activity_details(
                    str(aid), maxchart=2000, maxpoly=2000
                )
            except Exception:
                app.logger.debug("Could not load details for rTSS (activity %s)", aid)
                details = None
        rtss_map[aid] = estimate_running_rtss(
            a, threshold_hr, details=details
        )

    return render_template(
        "activities.html",
        activities=activities_list,
        email=email,
        threshold_hr=threshold_hr,
        rtss_map=rtss_map,
    )


@app.post("/settings/threshold-hr")
def set_threshold_hr():
    if not session.get("garmin_email") or not session.get("garmin_token_store"):
        flash("Please log in first.", "error")
        return redirect(url_for("login"))

    raw = (request.form.get("threshold_hr") or "").strip()
    if not raw:
        session.pop("threshold_hr", None)
        flash("Threshold HR cleared.", "info")
        return redirect(url_for("activities"))

    try:
        value = int(raw)
    except ValueError:
        flash("Threshold HR must be a whole number (bpm).", "error")
        return redirect(url_for("activities"))

    if value < 100 or value > 220:
        flash("Threshold HR should be between 100 and 220 bpm.", "error")
        return redirect(url_for("activities"))

    session["threshold_hr"] = value
    flash("Threshold HR saved. rTSS estimates updated for running activities.", "info")
    return redirect(url_for("activities"))


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
