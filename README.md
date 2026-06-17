# Radicale Manager

Radicale Manager is a Flask web UI for managing CardDAV contacts on a Radicale server. It focuses on saved connection profiles, address book discovery, contact browsing, editing, import/export, and operational visibility for a small self-hosted setup.

## Current Features

- Saved Radicale connection profiles
- Optional encrypted profile passwords with `PROFILE_SECRET_KEY`
- Cached address book discovery
- Contact list, detail, edit, delete, copy, and move workflows
- VCF import and export
- Dashboard metrics and connection status
- Feature registry and route explorer
- Health and readiness endpoints

## Local Quick Start

Install dependencies:

```powershell
python -m pip install -r requirements.txt
```

Create a local environment file:

```powershell
Copy-Item .env.example .env
```

Run the app:

```powershell
python app\main.py
```

Open:

```text
http://localhost:5000
```

Run tests:

```powershell
python -m unittest discover -s app
```

For a fuller local setup guide, see [LOCAL_DEV.example.md](LOCAL_DEV.example.md).

## Configuration

The app reads configuration from environment variables, and local development can use a `.env` file.

| Variable | Purpose | Default |
| --- | --- | --- |
| `APP_SECRET_KEY` | Flask session secret | random per process |
| `PROFILE_STORE_PATH` | SQLite profile/cache database path | `/data/profiles.db` |
| `PROFILE_SECRET_KEY` | Optional password encryption secret | unset |
| `DEFAULT_RADICALE_URL` | Default URL shown on connection forms | `https://radicale.example.test` |
| `PORT` | Local server port | `5000` |
| `FLASK_DEBUG` | Enable Flask debug mode for local dev | disabled |

## Production Data

The Docker image defaults to `PROFILE_STORE_PATH=/data/profiles.db`. In production, mount `/data` as persistent storage. If `/data` only exists inside the container filesystem, profile data can be lost when the container is recreated.

## Docker

Build:

```powershell
docker build -t radicale-manager .
```

Run with persistent data:

```powershell
docker run --rm -p 5000:5000 -v radicale-manager-data:/data radicale-manager
```
