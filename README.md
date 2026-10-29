# agent-api

Open-source Python backend (FastAPI) that connects a simple chat and admin UI ([agent-ui](https://github.com/YOUR-ORG/agent-ui))
to **[Hermes Agent](https://hermes-agent.nousresearch.com)**. It keeps Hermes and its API key off the internet
and gives visitors and admins only what they need.

- **Chat:** sessions, streaming replies (SSE), friendly tool-progress labels, approvals, file attachments
- **Visitors:** saved replies, "Save my work" (link a browser's work to a verified email)
- **Admin:** login, dashboard checklist and stats, site settings, knowledge sources, Hermes status and skills
- **Mock agent:** the whole UI works without Hermes (`HERMES_MODE=mock`) for development, demos and tests
- **Deploy kit:** launchd services and a Caddyfile for a Mac mini; see [docs/HERMES_MAC_MINI_SETUP.md](docs/HERMES_MAC_MINI_SETUP.md)

```
agent-ui ──/api──► agent-api ──Bearer key──► Hermes API server (127.0.0.1:8642)
```

## Quick start (mock agent)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env            # HERMES_MODE=mock, ADMIN_PASSWORD=admin
uvicorn app.main:app --reload --port 8000
```
Then run agent-ui (`npm run dev`) and open http://localhost:5173 (admin: `/admin`, user `admin` / `admin`).
API docs: http://localhost:8000/docs

## Connect Hermes

1. In `~/.hermes/.env`: `API_SERVER_ENABLED=true`, `API_SERVER_KEY=<secret>`, then run `hermes gateway`.
2. In this project's `.env`: `HERMES_MODE=hermes`, `HERMES_API_KEY=<same secret>`.
3. Restart. `GET /api/health` should show `"reachable": true`.

Chat uses Hermes' **Responses API** (`POST /v1/responses` with `conversation` = our session id and `stream: true`),
so Hermes keeps the conversation state. With **Learn and improve** off, the request carries an instruction not
to write skills or memory, for faster, side-effect-free replies.

## API

| Method | Path | Who | Purpose |
|---|---|---|---|
| GET | `/api/health` | anyone | API + agent status |
| GET | `/api/config` | anyone | Public site config (brand, assistant, avatar, quick replies) |
| POST | `/api/chat/sessions` | visitor | New session (returns the greeting) |
| GET | `/api/chat/sessions/{id}/messages` | owner | History |
| POST | `/api/chat/sessions/{id}/stream` | owner | Send `{text, learn, attachments}`, SSE: `start`, `delta`, `progress`, `approval`, `error`, `done` |
| POST | `/api/chat/runs/{run_id}/approval` | visitor | `{approve, sessionId}` |
| POST | `/api/chat/uploads` | visitor | Multipart file upload |
| GET/POST/DELETE | `/api/saved-replies[/{id}]` | visitor | Saved replies |
| POST | `/api/save-work/start`, `/api/save-work/verify` | visitor | Email code to link saved work across devices |
| POST | `/api/auth/login`, `/api/auth/logout`; GET `/api/auth/me` | — | Admin session |
| GET | `/api/admin/dashboard` | admin | Checklist, stats, agent status |
| GET/PUT | `/api/admin/settings` | admin | Site settings (PUT deep-merges) |
| GET/POST/DELETE | `/api/admin/knowledge[/{id}]` | admin | Knowledge sources |
| GET | `/api/admin/hermes` | admin | Hermes status, skills, dashboard URL |

Visitors are identified by a signed session cookie. Once they verify an email, their sessions, saved replies and
uploads move to that email.

## Project layout

```
app/
  main.py          app factory (middleware, routers, DB, agent)
  config.py        settings from env / .env
  hermes.py        HermesAgent (real) + MockAgent, SSE parsing, tool labels
  db.py            SQLite (stdlib) — swap for Postgres when needed
  auth.py          sessions, admin password hashing
  routers/         public, chat, user (saved replies, save-work), admin
deploy/            launchd plists + Caddyfile for macOS
docs/              Mac mini + Hermes setup guide
tests/             pytest (API + Hermes adapter with a fake Hermes server)
```

## Extending

- **Friendly tool labels:** `TOOL_LABELS` in `app/hermes.py`.
- **Email for "Save my work":** replace `send_code()` in `app/routers/user.py` (Postmark, SES, Resend…).
- **Another agent runtime:** implement the `Agent` protocol in `app/hermes.py` and return it from `make_agent()`.
- **Approvals:** the UI handles `approval` events; `translate_hermes_event()` maps any Hermes event whose type
  contains "approval". Check the event names and the approval body (`/v1/runs/{id}/approval`) for your Hermes
  version, and keep command approvals switched on in Hermes itself as well.
- **Knowledge → RAG:** sources are stored here. Index them for Hermes with a RAG MCP server (LanceDB/Chroma).

## Tests

```bash
pytest -q
```

## Production checklist

`SECRET_KEY` set · `ADMIN_PASSWORD_HASH` set (no `ADMIN_PASSWORD`) · `COOKIE_SECURE=true` behind HTTPS ·
`DEV_SHOW_CODES=false` · a real mailer in `send_code()` · bind to 127.0.0.1 behind Caddy · Hermes on 127.0.0.1 only.

## License

MIT
