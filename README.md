# contentstudio-be

FastAPI backend for Content Studio (single-user football content tool). Frontend lives in
[studiodecontent](https://github.com/KahaanKapasi/studiodecontent).

## Run locally

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # fill in what you have; everything degrades gracefully without keys
uvicorn app.main:app --reload --port 8000
```

Tables are created (and the 3 Posts templates seeded) on startup. With no `DATABASE_URL` it uses
`storage/content_studio.db`.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

Offline and hermetic: temp SQLite DB, all secrets blanked, sockets blocked, Gemini/IG/X/Cloudinary mocked.

## Deploying (Render)

- Build: `pip install -r requirements.txt` · Start: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- Python is pinned by `.python-version`.
- **Set `DATABASE_URL` to a Postgres URL** — Render's free disk is ephemeral and wipes SQLite on each deploy.
- **Set `LOCAL_ACCESS_PASSWORD`** — otherwise the API is open to anyone with the URL.
- Add the other keys from `.env.example` as you get them.

## Known limits

- Getty search/download is not automatable (bot detection); download manually and upload in Posts.
- Instagram scraping is a stub; Video voice/avatar generation is not built (service undecided).
