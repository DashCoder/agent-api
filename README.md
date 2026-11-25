# agent-api

Open-source Python backend (FastAPI) that connects a simple chat and admin UI ([agent-ui](https://github.com/DashCoder/agent-ui))
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

**Chat without Hermes:** `HERMES_MODE=direct` sends chat straight to one model (chosen as "Chat" on Model & tools;
default `DIRECT_MODEL=qwen3.6:35b-a3b` on the local Ollama) with a short prompt and the installed agents' tools.
Replies start much sooner on a server without a GPU, and only one model needs to be in memory. You don't get Hermes'
long-term memory, skills, terminal, web browsing, scheduled jobs or Slack bot. Tools keep the same access rules
(see Security). Switch back any time with `HERMES_MODE=hermes`.
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
| GET | `/api/admin/audit` | admin | Audit trail (core and plugins) |
| POST | `/api/mcp` | Hermes (Bearer `MCP_KEY`) | MCP tools contributed by plugins |

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

## Plugins

agent-api is a small, stable core; extra features ship as **plugins**, separate Python packages that the core
loads at start-up. A plugin can add API routes, MCP tools for Hermes, its own database tables (with migrations), its
own settings and entries in `/api/config`, using only `app/plugins.py` and the shared services (`require_admin`,
the database, `audit()`). Nothing in the core imports a plugin.

```toml
# the plugin's pyproject.toml
[project.entry-points."agent_api.plugins"]
builder = "my_plugin:plugin"
```

```bash
.venv/bin/pip install -e ../my-plugin      # into agent-api's own virtualenv
echo "PLUGINS=builder" >> .env              # switch it on by name, then restart
```

Plugins declare which plugin-API versions they support (`PLUGIN_API`, currently 1). An incompatible or missing
plugin stops start-up with a clear error, rather than failing later. See `tests/test_plugins.py` for a complete
example plugin.

**MCP for Hermes.** All plugin tools are served from one endpoint, `/api/mcp`, protected by `MCP_KEY`:

```yaml
# ~/.hermes/config.yaml
mcp_servers:
  inagentic:
    url: "http://127.0.0.1:8000/api/mcp"
    headers:
      Authorization: "Bearer ${MCP_KEY}"
```

**Audit trail.** `db.audit(actor, action, target, **detail)` appends to `audit_events`; the core logs admin logins
and denied MCP calls, and plugins log their own actions. `GET /api/admin/audit` returns the trail.

## OpenRouter traces (optional, for debugging)

OpenRouter can send a trace of every model call (Settings → Observability → Broadcast → Webhook): model, provider,
tokens, cost, timing, finish reason and errors. agent-api shows them in **Agent logs** under the agent that made the
call, marks calls that were **blocked** (OpenRouter's moderation, or a provider's content filter) and has an
"Only failed or blocked" filter. Prompts and answers are never stored; switch on Privacy Mode in OpenRouter too.

OpenRouter needs a public URL and the mini isn't public, so `deploy/openrouter-relay/template.yaml` is a tiny AWS
relay: a Lambda function URL that queues traces (kept one day) and that agent-api pulls every 30 seconds. Nothing on
the mini is exposed. It costs well under a dollar a month; delete the stack when you're done.

1. Deploy it (any AWS account; tokens are made on your machine and never shown):
   ```
   PUSH=$(openssl rand -hex 24); PULL=$(openssl rand -hex 24)
   aws cloudformation deploy --stack-name openrouter-relay --template-file deploy/openrouter-relay/template.yaml \
     --capabilities CAPABILITY_IAM --parameter-overrides PushToken=$PUSH PullToken=$PULL
   aws cloudformation describe-stacks --stack-name openrouter-relay --query "Stacks[0].Outputs" --output table
   ```
2. In agent-api's `.env`: `OPENROUTER_RELAY_URL=<RelayUrl output>` and `OPENROUTER_RELAY_TOKEN=<PULL>`; restart.
3. In OpenRouter → Observability → Broadcast → Webhook: URL = the `WebhookUrl` output, headers
   `{"Authorization": "Bearer <PUSH>"}`, Privacy Mode on, then Test Connection and save.

Agents tag their OpenRouter calls (`user: "inagentic:<agent>"`), so each trace lands under the right agent; calls
without a tag (e.g. Hermes' own) are under the assistant. If you publish agent-api yourself, OpenRouter can also post
straight to `/api/hooks/openrouter` with `OPENROUTER_TRACES_TOKEN` as the bearer token.

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

## Security

Hermes has tools, a terminal and your connections, so by default only these may chat with it (`app/security.py`):

- the **admin** (signed in to the admin), and
- **trusted emails**: people who signed in with an email listed in `TRUSTED_EMAILS` (or on Settings & keys).

Anyone else gets "please sign in" (HTTP 401 `sign_in`, or 403 `not_trusted` for a verified email that isn't listed).
To let the public chat too, set `CHAT_ACCESS=public` **and** run a second Hermes profile with no tools, no terminal
and no MCP, at `HERMES_PUBLIC_API_URL` / `HERMES_PUBLIC_API_KEY`. Public chat is limited per IP (`CHAT_PER_MINUTE`,
`CHAT_PER_DAY`) and for the whole site (`CHAT_SITE_PER_DAY`); attempts to extract prompts, config or keys get a fixed
reply without a model call, and three in a day pause that connection for 24 hours.

- **Tools** (`/api/mcp`) run for the least trusted web chat running at that moment (from signed session cookies, never
  from arguments); with no chat running, for the server itself (Slack, schedules). Plugin tools need a trusted caller
  unless they declare `access="public"`, `"verified"` or `"admin"`; tools see `ctx.caller`.
- **Secrets** (this server's own keys, every saved key, and well-known token formats) are redacted from chat replies,
  tool results and tool errors.
- **Email sign-in** (`/api/auth/email-link`): the email has a link and a 6-digit code. The link signs in only the
  browser that opens it; the code signs in the browser that asked (5 tries). Links use `PUBLIC_URL`, or the address in
  use only if it's this machine or in `CORS_ORIGINS`. At most 3 emails an hour and 6 a day per real inbox
  (`a+x@` = `a@`), 10 an hour per IP. "Save my work" codes have the same limits.
- **Approvals**, voice, live avatar and uploads are limited or admin-only for untrusted callers.

## Production checklist

`SECRET_KEY` set · `ADMIN_PASSWORD_HASH` set (no `ADMIN_PASSWORD`) · `COOKIE_SECURE=true` behind HTTPS ·
`DEV_SHOW_CODES=false` · SES set on Settings & keys · `PUBLIC_URL` set · `TRUSTED_EMAILS` set · bind to 127.0.0.1
behind Caddy · Hermes on 127.0.0.1 only.

## License

MIT
