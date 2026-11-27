# Prompt: production gunicorn service for this project

You are setting up the production web server for this repository on an
Ubuntu/Debian VPS: gunicorn as a systemd service. Read this whole brief before
changing anything. Inspect the files it names yourself; don't rely on this
summary alone.

## How traffic reaches the app (fixed, don't change it)

- The VPS has a hosting control panel with a site called **"Aptem Reviews"** of
  type **Web Server (Proxy)**. It forwards every request for the site to
  **`127.0.0.1:8037`**. There is no domain: users open `http://72.62.133.105`
  (plus a port if the panel uses one).
- **nginx is banned on this server.** Don't install, configure or recommend
  nginx, and don't write nginx config or snippets. Don't install any other web
  server or reverse proxy either (no Apache, Caddy, Traefik or certbot). The
  panel's proxy is the only thing in front of gunicorn.
- So **gunicorn on `127.0.0.1:8037` serves the entire site**: the API, the
  Django admin, the admin's static files and the built React frontend.
- Bind TCP `127.0.0.1:8037` only. Don't use a unix socket, another port or
  `0.0.0.0`, so the port is never reachable from outside.
- Before starting, check that nothing else is on the port:
  `ss -ltnp | grep 8037`. If something is, stop and tell me.
- Don't change anything in the hosting panel yourself. If a panel setting
  matters (proxy timeout, upload size limit, whether `X-Forwarded-Proto` and
  `Host` are passed through), list it for me to check.

## What already exists (do not redo or break it)

- Django 6 project in `backend/`. WSGI entry point: `config.wsgi:application`
  (`backend/config/wsgi.py`). Settings: `backend/config/settings.py`, which
  loads secrets from `backend/.env` itself (custom `load_dotenv`), so systemd
  does **not** need an `EnvironmentFile` for Django's variables.
- Virtualenv at `<repo>/.venv`, created by `deploy/install_vps.sh`. Use that
  same venv; don't create a second one.
- A nightly sync already runs as `aptem-nightly.service` and `aptem-nightly.timer`
  (`deploy/systemd/`, `scripts/nightly_sync.sh`). It runs as the same non-root
  user and from the same checkout. Don't modify it. Gunicorn must stay
  independent of it: no shared lock, and a restart of one must not affect the other.
- The nightly job's cookie refresh rewrites `backend/.env`. Keep `.env`
  owned by the app user with mode 600.
- Database: Neon Postgres via `DATABASE_URL` (PgBouncer transaction pooler;
  see the note in `backend/aptem/services/cycle.py`). Keep the
  `CONN_HEALTH_CHECKS` and keepalive options in settings.
- URLs: `/admin/`, `/api/evidence-tool/` and `/api/aptem/` (`backend/config/urls.py`).
  Media is only served by Django when `DEBUG=True`.
- Frontend: Vite + React in `frontend/`. It calls `/api/evidence-tool` on the
  same origin (in dev, Vite proxies `/api` to `127.0.0.1:8000`). In production,
  Django serves the built `frontend/dist` on the same origin.
- `deploy/install_vps.sh` and `deploy/systemd/*` show the conventions to follow:
  unit templates with `@ROOT@`, `@APP_USER@` and `@APP_GROUP@` placeholders,
  rendered by an idempotent installer run with sudo.

## Playwright and browsers: not part of the web service

- The web app (`aptem/urls.py` → `views.py`, `review_document_views.py`,
  `table_views.py` and the services they import) does **not** use Playwright,
  Chrome or subprocesses. It talks to Aptem with plain `requests`
  (`services/aptem_auth.py`, `services/evidence_downloader.py`), to Azure Blob
  for review PDFs (`services/review_pdf_transfer.py`), and to OpenAI for
  analysis (`services/evidence_analyser.py`).
- Playwright and Chrome are used only by the nightly capture
  (`capture_modern_reviews` → `scripts/capture-aptem-modern.cjs`), and
  `deploy/install_vps.sh` already installs them. Don't install browsers or
  Playwright for gunicorn, and don't add browser paths or display settings to
  the web unit. Keep the hardening from breaking the nightly job: it is a
  separate unit, so `aptem-web.service` hardening must not be copied onto it.
- Several management commands (`probe_aptem_reviews`, `sync_visible_aptem_reviews`
  and others) launch browsers too. They're manual or old tools; leave them alone.

## Long-running requests (affects the gunicorn timeout)

Some API endpoints do heavy work inside the HTTP request: they download from
Aptem, extract text and call OpenAI before replying. Examples are
`process_source_next`, `process_evidence_batch`, `process_source_item` and
`retry_evidence` in `backend/aptem/views.py`. A batch can be up to
`APTEM_MAX_BATCH_SIZE` (100) files, with `APTEM_REQUEST_DELAY_SECONDS` (1.5 s)
between files and up to `APTEM_REQUEST_TIMEOUT_SECONDS` (120 s) per download.
Gunicorn's default 30 s timeout would kill those workers partway through.

- Measure the realistic duration: read the code and check how the frontend
  (`frontend/src/main.tsx`) calls these endpoints and with what batch size.
  Then set `--timeout` to cover it, and tell me the number and the reasoning.
- Use `gthread` workers with a few threads, so one long request doesn't block
  every other user.
- Tell me which panel proxy timeout I need to set (at least the gunicorn
  timeout). Otherwise the panel cuts the connection first.
- Don't move this work to a task queue (Celery or similar) in this change.
  Just flag it as a possible follow-up.
- Cookie refresh during a download rewrites `backend/.env`
  (`aptem_auth.write_env_value`). The web unit must keep write access to that file.

## Security requirement (must fix before going live)

`backend/aptem/permissions.py` grants staff access with no login to any request
that looks local (`REMOTE_ADDR`, the first `X-Forwarded-For` entry, or the
`Host` header is localhost or 127.0.0.1). `APTEM_LOCAL_AUTH_BYPASS` defaults to
`True` in settings and is not set in `.env`. Behind the panel's proxy, every
request arrives from 127.0.0.1, so the whole API would be open to the internet.
Proxies usually append to `X-Forwarded-For` rather than replace it, so its first
entry is whatever the client sent.

- Add `APTEM_LOCAL_AUTH_BYPASS=False` to `backend/.env` on the server, and
  verify it took effect. Ask me before changing the code default, because local
  dev relies on it.
- Unauthenticated requests to `/api/evidence-tool/...` through the public
  address (`http://72.62.133.105`) must be rejected. Test it with curl, including with spoofed
  `X-Forwarded-For: 127.0.0.1` and `Host: localhost` headers.

Also make sure production `.env` has `DJANGO_DEBUG=False`, a real
`DJANGO_SECRET_KEY` (not `CHANGE_ME`), and `DJANGO_ALLOWED_HOSTS` and
`DJANGO_CSRF_TRUSTED_ORIGINS` set to the server's public IP (there is no
domain; see task 2). Never print secret values.
Show variable names only.

## Tasks

1. Add `gunicorn` and `whitenoise` to `backend/requirements.txt`, each with a
   pinned major range matching the file's style, and install them into `.venv`.
2. Django settings changes for production, kept minimal and env-driven so local
   dev is unchanged:
   - `STATIC_ROOT` (e.g. `<repo>/backend/staticfiles`) so `collectstatic` works.
   - WhiteNoise middleware directly after `SecurityMiddleware`, with compressed
     manifest static storage.
   - **There is no domain.** The site is reached at `http://72.62.133.105`
     (plain HTTP, no TLS certificate). `DJANGO_ALLOWED_HOSTS` holds the
     server IP and `DJANGO_CSRF_TRUSTED_ORIGINS` holds `http://72.62.133.105[:port]`.
   - Put every HTTPS-only setting behind one env flag, `DJANGO_HTTPS`, which
     defaults to `False`: `SECURE_PROXY_SSL_HEADER`, `SESSION_COOKIE_SECURE`,
     `CSRF_COOKIE_SECURE`, HSTS and `SECURE_SSL_REDIRECT`. With it off, admin
     login must work over plain HTTP. Secure cookies over HTTP silently break
     login, and HSTS on an IP address can lock browsers out. When
     `manage.py check --deploy` warns about these settings, explain that they
     are deliberately off because the site has no TLS. Don't "fix" them.
   - Tell me plainly that over HTTP, admin passwords and evidence contents
     travel unencrypted. Suggest either a free hostname (sslip.io or DuckDNS,
     with a certificate issued by the panel), after which I set `DJANGO_HTTPS=True`,
     or a firewall rule allowing the site's port only from the college's IP.
     Don't do either one without asking me.
3. Serve the React build from Django, since there is no other web server:
   - Serve `frontend/dist/assets/*` (hashed files) through WhiteNoise, for
     example via `WHITENOISE_ROOT` or by adding `dist` to the static files,
     with long cache headers.
   - Add a catch-all view that returns `frontend/dist/index.html` for SPA
     routes. Register it last in `config/urls.py` so `/api/`, `/admin/` and
     `/static/` always win, and unknown `/api/...` paths still return 404 JSON,
     not the SPA.
   - Never expose `backend/media`, `APTEM_DOWNLOAD_DIRECTORY` or `debug/`.
     Evidence files must only be reachable through authenticated Django views.
4. `deploy/systemd/aptem-web.service` (template, no socket unit):
   - `User`/`Group` = app user. `WorkingDirectory=@ROOT@/backend`.
   - `ExecStart=@ROOT@/.venv/bin/gunicorn config.wsgi:application --bind 127.0.0.1:@PORT@`
     (`@PORT@` defaults to 8037). Consider a `gunicorn.conf.py` in `deploy/`.
   - Workers ≈ `2 × CPU + 1`, capped for a small VPS. Gunicorn's default sync
     workers block while sending a file, so consider `gthread` with a few threads.
   - Choose `--timeout` after checking `backend/aptem/views.py` and
     `review_document_views.py` for slow synchronous work (file downloads, PDF
     streaming, Aptem calls). Add `--graceful-timeout`, `--max-requests` with
     jitter, and access/error logs to journald.
   - `Restart=on-failure`, `ExecReload` sending HUP for zero-downtime reloads,
     and sensible hardening (`NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=full`).
     Keep write access to `backend/media` (or `APTEM_DOWNLOAD_DIRECTORY`),
     `backend/.env` and `logs/`.
   - `After=network-online.target`. Don't add a dependency on the nightly units.
5. `deploy/install_web.sh`: idempotent and run with sudo, in the same style as
   `install_vps.sh`.
   - It checks that port 8037 is free (or already ours), runs
     `npm ci && npm run build` in `frontend/` as the app user (install Node LTS
     only if it's missing), and runs `collectstatic --noinput`.
   - Then it renders and installs the unit, and enables and starts the service.
   - It must **not** run `migrate` automatically. Print the `showmigrations`
     status and let me decide, because the database is shared production Neon.
6. Add a "Web server (gunicorn)" section to `backend/docs/vps-nightly-sync.md`,
   or a sibling doc linked from it. Cover install, deploying an update
   (`git pull` → pip install → build frontend → collectstatic →
   `systemctl reload aptem-web`), logs, the panel settings to check, and a rollback.

## Constraints

- No nginx and no other web server or proxy (see above).
- Aptem is read-only: no code path may write to or delete from Aptem.
- The database is the source of truth: no schema changes, no data writes and
  no migrations as part of this work.
- Don't touch the Windows `.ps1` scripts or the nightly sync units.
- Ask before anything destructive or hard to reverse on the server (removing
  packages, changing anything in the hosting panel, changing firewall rules).

## Verify before saying it's done

- `systemd-analyze verify` on the rendered unit, and
  `manage.py check --deploy` (fix or explain every warning).
- Run the existing tests (`manage.py test aptem`) and make sure the new URL
  routes didn't break any of them.
- `systemctl status aptem-web` is active. A reload keeps it up.
- `ss -ltnp | grep 8037` shows gunicorn on `127.0.0.1:8037` only.
  From outside the server, `curl http://72.62.133.105:8037` must fail.
- `curl http://127.0.0.1:8037/...` directly, then the same through the
  panel's public address (`http://72.62.133.105`):
  - `/` and a deep SPA route return `index.html`.
  - `/assets/<hashed file>` and `/static/admin/css/base.css` return 200 with cache headers.
  - Unauthenticated API calls are rejected, including with spoofed localhost headers.
  - `/api/does-not-exist` returns 404, not the SPA.
  - `/media/...` is not served.
- `systemctl list-timers aptem-nightly.timer` still shows the next run.
- Report exactly what you ran and what passed or failed. Show the full output
  of any failure.
