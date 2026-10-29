# agent-api

Open-source Python backend (FastAPI) that connects a simple chat and admin UI ([agent-ui](https://github.com/YOUR-ORG/agent-ui))
to **[Hermes Agent](https://hermes-agent.nousresearch.com)**. It keeps Hermes and its API key off the internet
and gives visitors and admins only what they need.

- **Chat:** sessions, streaming replies (SSE), friendly tool-progress labels, approvals, file attachments
- **Visitors:** saved replies, "Save my work" (link a browser's work to a verified email)
- **Admin:** login, dashboard checklist and stats, site settings, knowledge sources, Hermes status and skills
- **Mock agent:** the whole UI works without Hermes (`HERMES_MODE=mock`) for development, demos and tests
- **Deploy kit:** launchd services (incl. Colima/Docker) and a Caddyfile for a Mac mini; see [docs/HERMES_MAC_MINI_SETUP.md](docs/HERMES_MAC_MINI_SETUP.md)
  (HTML version: `docs/HERMES_MAC_MINI_SETUP.html`, rebuilt with `python scripts/build_docs.py`)

```
agent-ui ──/api──► agent-api ──Bearer key──► Hermes API server (127.0.0.1:8642)
```

## Try it locally (5 minutes, no Hermes needed)

You need Python 3.11+ and Node 18+ (`brew install python@3.12 node` on a Mac). Put both projects side by side,
e.g. `~/dev/agent-api` and `~/dev/agent-ui`.

**1. Start the backend** (Terminal tab 1). It runs a built-in mock agent:
```bash
cd ~/dev/agent-api
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env          # HERMES_MODE=mock, admin password "admin"
uvicorn app.main:app --reload --port 8000
```
Check: http://localhost:8000/api/health shows `"mode":"mock"`.

**2. Start the UI** (Terminal tab 2):
```bash
cd ~/dev/agent-ui
npm install
cp .env.example .env
npm run dev
```

**3. Open http://localhost:5173 and try:**

| Try | What should happen |
|---|---|
| Click **What are your rates?** | The reply streams in: "Plans start at £49…" |
| Click the save icon on a reply | It appears under **Saved replies** |
| Type "delete old logs" | An **Approve / Deny** card appears |
| **🔐 Save my work** → any email | Dev mode shows the code on screen → enter it |
| Press ▶ on the avatar | Replies are read aloud |
| **Admin →**, log in as `admin` / `admin` | Dashboard, then Settings, Avatar and Hermes pages |
| Change the assistant name in **Settings** | The chat placeholder becomes "Ask <name> anything…" |

**Automated UI tests** (no backend needed): `npx playwright install chromium` (first time only), then
`npm run test:e2e` in agent-ui.

**Use the real agent:** set `HERMES_MODE=hermes` and `HERMES_API_KEY` in `agent-api/.env`, then restart the backend.
See [docs/HERMES_MAC_MINI_SETUP.md](docs/HERMES_MAC_MINI_SETUP.md), section 10.

API docs (Swagger): http://localhost:8000/docs

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
