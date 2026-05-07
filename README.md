# Garmin Activities Flask App

Simple Flask web app that lets a user log in with Garmin credentials and view latest activities.

## 1) Create environment and install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 2) Run app

```bash
export FLASK_SECRET_KEY="replace-with-a-random-secret"
export FLASK_DEBUG=1
python app.py
```

Open [http://127.0.0.1:5000](http://127.0.0.1:5000).

## Notes

- This version uses server-side sessions (`Flask-Session`) so credentials are not stored in browser cookies.
- The app uses `python-garminconnect` token storage for subsequent requests, so raw passwords are not kept in session.
- Garmin tokens are stored under `instance/garmin_tokens` by default and removed on logout.
- You can override token storage location with `GARMIN_TOKEN_ROOT`.

## rTSS (running, HR-based estimate)

On the activities page, set **threshold HR (LTHR)** in bpm. For **running** activities, the app shows **rTSS (est.)** using a TSS-style formula: intensity factor ≈ HR ÷ LTHR (clamped), then **IF² × hours × 100**. This is an **approximation**; TrainingPeaks “rTSS” is normally pace-based, not HR-based.

- By default, the estimate uses Garmin’s **average HR** from the activity summary (no extra API calls).
- Set `RTSS_USE_ACTIVITY_STREAM=1` to integrate HR **per recorded sample** from activity details (more accurate, more Garmin API requests).

## Deploy (Render)

1. Push this project to GitHub.
2. Create a Render Blueprint deploy from `render.yaml` (recommended), or create a Web Service manually.
3. Configure:
   - Build command: `pip install -r requirements.txt`
   - Start command: `gunicorn app:app --bind 0.0.0.0:$PORT`
4. Add environment variables:
   - `FLASK_SECRET_KEY` = long random string
   - `SESSION_COOKIE_SECURE` = `true`
   - `FLASK_DEBUG` = `0`
   - (optional) `GARMIN_TOKEN_ROOT` = persistent writable path
5. On Free tier, token storage is ephemeral (`/tmp/garmin_tokens`) and users may need to log in again after restarts/deploys.
6. If you upgrade the service plan, attach a persistent disk and set `GARMIN_TOKEN_ROOT` to that mount path.
