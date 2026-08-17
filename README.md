# acadtrack

A self-hosted academic tracker for weekly timetables, attendance, syllabus coverage and upcoming assessments. Built with Flask and SQLite, with optional Google sign-in and Gemini-assisted progress interpretation.

## Run locally

Use Python 3.10 or newer. Create a virtual environment, install dependencies and copy the example environment:

```sh
python -m venv .venv
# Activate .venv using the command for your shell.
python -m pip install -r requirements.txt
cp examples/.env.example .env
```

Set `SECRET_KEY` to a new random value. For local development, set `ACADTRACK_LOCAL_LOGIN=1`, leave `GOOGLE_CLIENT_ID` empty and keep `ALLOWED_EMAILS=reader@example.com`. Start the application with `python app.py` and open `http://127.0.0.1:5000`. Local login is disabled when an OAuth client is configured.

For Google sign-in, provide your own OAuth client ID and secret and register the exact `GOOGLE_REDIRECT_URI` in your provider console. Set the email allowlist to the users who should access this single-tenant database; an empty allowlist rejects everyone. Use `ACADTRACK_HTTPS=1` behind HTTPS. All allowlisted users share the same academic records.

`GEMINI_API_KEYS` is optional. Leave it empty to run without AI requests. If enabled, selected syllabus and progress notes are sent to the configured model provider when the user requests interpretation.

## Synthetic example data

`timetable.json` and `semester.json` contain fictional demonstration courses, rooms, dates, topics and assessments. They describe no real person's enrollment or schedule. Import them into a new local database:

```sh
python seed_timetable.py timetable.json
python seed_timetable.py semester.json
```

Use the timetable/settings/course screens to enter your own data. Keep personal import files under `private/`, which Git ignores. `--replace` on the importer replaces matching course slots; use it only when that is intended.

## Tests

```sh
python -m pip install pytest pytest-socket
python -m pytest -q --disable-socket
```

Tests use temporary SQLite databases and synthetic fixtures. Provider calls are mocked; the socket guard prevents accidental live requests.

## Deployment

The `deploy/` directory contains example Linux systemd/nginx installation scripts. Configure your own host, domain, SSH key path and environment values. The example application port is `8000`; set `PORT` consistently if changing it. The scripts do not obtain credentials from another application. Review the files and back up your database before running them on a server.

Never commit `.env`, OAuth downloads, provider keys, cookies, databases, personal academic records or private handouts. Database files live under ignored `data/` by default. The supplied environment and seed files are examples only.
