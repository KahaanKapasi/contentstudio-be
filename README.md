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

## Video generation

`/api/video/generations` turns a prompt into an MP4 through one of three providers; each is enabled
by its own keys (`GET /api/video/providers` reports which are missing). Real calls cost money.

| Provider | Env vars | Notes |
|---|---|---|
| Google Veo | `GEMINI_API_KEY` | Paid Gemini API only. Output is deleted by Google after ~2 days, so it is downloaded immediately. |
| Higgsfield | `HF_API_KEY_ID`, `HF_API_KEY_SECRET` | Separate prepaid balance. Model paths live in `HIGGSFIELD_MODELS` in `app/services/video_providers/catalog.py`. |
| Muapi | `MUAPI_API_KEY` | Hosted multi-model API. Models in `MUAPI_MODELS` (same file). |

`GEMINI_TEXT_MODEL` (default `gemini-3.7-flash`) is the text model, also used for the web-grounded
"improve prompt" step. Finished videos are saved to `storage/videos/` and, if `CLOUDINARY_URL` is set,
also uploaded so they survive Render's ephemeral disk. Jobs resume after a restart (the next
`GET /generations/{id}` polls the provider again) and are marked failed after 15 minutes.

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

## Instagram token auto-refresh
With an Instagram-Login token (`IG_GRAPH_BASE=https://graph.instagram.com/v21.0`) the app keeps the 60-day token alive itself:
the live token is stored in the `app_secrets` table (seeded from `IG_ACCESS_TOKEN`, re-seeded if that env var changes) and
refreshed on startup and before any Instagram call once it is 7+ days old. It lapses only if the app is unused for ~50 days.
`GET /api/dashboard/instagram/token` shows status (never the token); `POST /api/dashboard/instagram/token/refresh` forces a refresh
(Instagram requires the token to be 24h old).
