# Hermes Agent on a Mac mini (M6) — complete setup guide

This guide sets up a Mac mini as an always-on AI agent server for a client, and connects it to
**agent-ui** (chat + admin console) through **agent-api** (Python backend).

**Time:** about 2 hours. **Tested target:** Mac mini M6, macOS 26 (Tahoe), Hermes Agent 2026.x.

> Commands marked **(admin)** run as the `inagentic` admin account. Commands marked **(agents)** run as
> the `aiagents` service account (`sudo -iu aiagents` from the admin account). Hermes changes quickly: where a
> flag might differ in your version, check it with `hermes <command> --help`.

---

## What you end up with

```
                       Internet / Tailscale
                               │
              ┌────────────────┴─────────────────┐
              │  Caddy 127.0.0.1:8080            │  serves agent-ui (dist/) + proxies /api
              └────────┬─────────────────────────┘
                       │ /api
              ┌────────┴─────────────────────────┐
              │  agent-api 127.0.0.1:8000        │  login, chat sessions, saved replies, admin API
              └────────┬─────────────────────────┘
                       │ Bearer API_SERVER_KEY
              ┌────────┴─────────────────────────┐
              │  Hermes gateway 127.0.0.1:8642   │  agent, tools, skills, memory, Telegram…
              │  Hermes dashboard 127.0.0.1:9119 │  admin only, over Tailscale
              └──────────────────────────────────┘
   All four run as the non-admin user "aiagents" via launchd, and start at boot.
```

Nothing listens on the network directly. Remote access goes through **Tailscale**.

---

## 1. Before you start

**Hardware**
- Mac mini M6 (32 GB recommended if you'll run local models).
- **Wired Ethernet**, which is needed for remote FileVault unlock after a reboot.
- A small **UPS** (£60–£100), to ride out short power cuts.

**Accounts and keys** (store them in your password manager)
- A model provider API key, e.g. Anthropic, in the **client's** account or yours billed onward, with a monthly spending limit.
- A Tailscale account (free plan is fine).
- Optional: Telegram bot token (from @BotFather) for the Telegram channel.

**Mac accounts you will create**

| Account | Full name | Account name | Type |
|---|---|---|---|
| Admin | InAgentic Admin | `inagentic` | Administrator |
| Agents | InAgentic AI Agents | `aiagents` | **Standard** (no admin rights) |

---

## 2. macOS first setup (admin)

1. During Setup Assistant, create **InAgentic Admin / `inagentic`**. Don't sign in with a personal Apple ID.
   If iMessage is needed later, use an Apple ID owned by the client.
2. **System Settings → General → Software Update:** install everything; turn on automatic security responses.
3. **FileVault** (System Settings → Privacy & Security → FileVault): **On**. Save the recovery key in your password
   manager, and agree with the client who else holds it.
4. **Name the Mac** (System Settings → General → Sharing → Local hostname), e.g. `acme-agents`.
5. **Always on** — run in Terminal:
   ```bash
   sudo pmset -a sleep 0 disksleep 0 displaysleep 10 autorestart 1 womp 1
   pmset -g | egrep 'sleep|autorestart'
   ```
6. **Firewall:** System Settings → Network → Firewall → On.
7. **Remote Login (SSH):** System Settings → General → Sharing → Remote Login → On, and allow access **only for
   InAgentic Admin**. Then make it key-only:
   ```bash
   # on YOUR laptop: copy your public key to the Mac
   ssh-copy-id inagentic@acme-agents.local
   # on the Mac:
   sudo tee /etc/ssh/sshd_config.d/100-keys-only.conf >/dev/null <<'EOF'
   PasswordAuthentication no
   KbdInteractiveAuthentication no
   PermitRootLogin no
   EOF
   sudo launchctl kickstart -k system/com.openssh.sshd
   ```
   Test a new SSH login **before** closing your current session.

## 3. Create the `aiagents` service account (admin)

```bash
sudo sysadminctl -addUser aiagents -fullName "InAgentic AI Agents" -password -   # prompts for a password
id aiagents        # check it exists
```
Store its password in your password manager. It's a **standard** user: if the agent is ever tricked into
running a bad command, it can't change the system or read the admin's files.

## 4. Install tools (admin)

```bash
# Homebrew (installs to /opt/homebrew; usable by all accounts)
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile && eval "$(/opt/homebrew/bin/brew shellenv)"

brew install git python@3.12 node caddy
brew install --cask tailscale        # Tailscale app; sign in and enable "Run at login" / unattended mode
```

**Docker for sandboxed agent commands (recommended).** Docker Desktop is only free for small businesses
(under 250 staff *and* under $10M revenue). Colima is free:
```bash
brew install colima docker
```

## 5. Install Hermes Agent (agents)

```bash
sudo -iu aiagents                      # become the service user
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
source ~/.zshrc                        # or open a new shell; hermes lives in ~/.local/bin
hermes version && hermes doctor
```

Configure it:
```bash
hermes model        # choose the provider (e.g. Anthropic) and paste the API key
hermes tools        # enable only what this client needs (see the security checklist)
hermes setup        # full wizard: optional channels such as Telegram
hermes chat -q "Hello! What tools do you have?"   # quick test
```

**Use Docker for terminal commands** (optional but recommended for client machines):
```bash
colima start --cpu 4 --memory 6 --disk 40      # as aiagents
```
Then set the terminal backend to `docker` with `hermes tools` or in the dashboard's Config editor, and run
`hermes doctor` again.

**Multiple agents:** create one Hermes **profile** per agent role (`hermes profile create reception`). Each has
its own memory, skills, tools, port and key. Start with one profile; add more later.

## 6. Enable the Hermes API server (agents)

```bash
KEY=$(openssl rand -hex 32); echo "Save this key: $KEY"
cat >> ~/.hermes/.env <<EOF
API_SERVER_ENABLED=true
API_SERVER_HOST=127.0.0.1
API_SERVER_PORT=8642
API_SERVER_KEY=$KEY
EOF
chmod 600 ~/.hermes/.env
```

Test it in the foreground:
```bash
hermes gateway            # look for: [API Server] API server listening on http://127.0.0.1:8642
```
In a second SSH session:
```bash
curl -s http://127.0.0.1:8642/health
curl -s http://127.0.0.1:8642/v1/models -H "Authorization: Bearer $KEY"
curl -sN http://127.0.0.1:8642/v1/responses -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
     -d '{"model":"hermes-agent","input":"Say hi","conversation":"test","stream":true}'
```
Stop it with Ctrl-C once it works. It will run as a service in step 10.

The web dashboard has **no login**, so keep it on localhost:
```bash
hermes dashboard --no-open --port 9119
# If it says the web extras are missing, run the install command it prints, then try again.
```

## 7. Install agent-api (agents)

```bash
mkdir -p ~/apps && cd ~/apps
git clone https://github.com/YOUR-ORG/agent-api.git && cd agent-api
/opt/homebrew/bin/python3.12 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env && chmod 600 .env
.venv/bin/python -m app.cli hash-password            # copy the ADMIN_PASSWORD_HASH line it prints
python3 -c "import secrets;print(secrets.token_urlsafe(48))"   # for SECRET_KEY
```
Edit `.env`:
```ini
HERMES_MODE=hermes
HERMES_API_URL=http://127.0.0.1:8642
HERMES_API_KEY=<the KEY from step 6>
HERMES_DASHBOARD_URL=https://acme-agents.<your-tailnet>.ts.net:9119   # set after step 11
SECRET_KEY=<random value>
COOKIE_SECURE=true
CORS_ORIGINS=https://acme-agents.<your-tailnet>.ts.net
ADMIN_USERNAME=admin
ADMIN_PASSWORD_HASH=<from hash-password>
ADMIN_PASSWORD=
DEV_SHOW_CODES=false
```
Quick test (with `hermes gateway` running):
```bash
.venv/bin/uvicorn app.main:app --port 8000 &
curl -s http://127.0.0.1:8000/api/health     # "mode":"hermes","reachable":true
kill %1
```

## 8. Build agent-ui (agents)

```bash
cd ~/apps
git clone https://github.com/YOUR-ORG/agent-ui.git && cd agent-ui
npm ci && npm run build          # output in dist/
```

## 9. Web server (Caddy)

agent-api ships `deploy/Caddyfile`. It serves `~/apps/agent-ui/dist` and proxies `/api` to agent-api on
**127.0.0.1:8080**, with SSE streaming enabled. Check it:
```bash
caddy validate --config ~/apps/agent-api/deploy/Caddyfile
```

## 10. Run everything as services (admin)

The plists in `agent-api/deploy/launchd/` are **LaunchDaemons** that run as `aiagents`. They start at boot
(after FileVault unlock, with no login needed) and restart if they crash.

```bash
exit   # back to the admin account if you're still "aiagents"
cd /Users/aiagents/apps/agent-api/deploy/launchd
sudo cp ai.agents.*.plist /Library/LaunchDaemons/
sudo chown root:wheel /Library/LaunchDaemons/ai.agents.*.plist
sudo chmod 644 /Library/LaunchDaemons/ai.agents.*.plist
for s in hermes-gateway hermes-dashboard agent-api caddy; do
  sudo launchctl bootstrap system /Library/LaunchDaemons/ai.agents.$s.plist
done
sudo launchctl list | grep ai.agents        # PID column should be filled
```

- **Logs:** `/Users/aiagents/Library/Logs/{hermes-gateway,hermes-dashboard,agent-api,caddy}.log`
- **Restart one:** `sudo launchctl kickstart -k system/ai.agents.agent-api`
- **Stop / remove:** `sudo launchctl bootout system/ai.agents.agent-api`

> If you previously ran `hermes gateway start` or `hermes gateway install`, Hermes may have created its own
> LaunchAgent (`~/Library/LaunchAgents/ai.hermes.gateway.plist` for aiagents). Remove it so two gateways don't
> fight over port 8642: `launchctl bootout gui/$(id -u aiagents)/ai.hermes.gateway` and delete the file.
>
> If the gateway exits immediately, check the foreground command for your Hermes version
> (`hermes gateway --help`) and update `ProgramArguments` in the plist (e.g. `gateway run` vs `gateway`).

Colima (if used) must also start at boot. As `aiagents`: `brew services start colima`, or add a similar
LaunchDaemon running `colima start --foreground`.

## 11. Remote access with Tailscale

Sign the Mac in to Tailscale (as admin), enable **MagicDNS** and **HTTPS certificates** in the Tailscale admin
console, and restrict access with ACLs to your devices and the client's staff.

```bash
# The app (chat + admin), private to the tailnet:
sudo tailscale serve --bg --https=443 http://127.0.0.1:8080
# The Hermes dashboard, private to the tailnet (it has no login of its own):
sudo tailscale serve --bg --https=9119 http://127.0.0.1:9119
tailscale serve status
```
Now:
- Chat: `https://acme-agents.<tailnet>.ts.net/`
- Admin console: `https://acme-agents.<tailnet>.ts.net/admin`
- Full Hermes dashboard: `https://acme-agents.<tailnet>.ts.net:9119`. Put this in `HERMES_DASHBOARD_URL` and
  restart agent-api; the **Admin → Hermes** page then shows an "Open full Hermes dashboard" button.

**Public chat for the client's customers** (optional): publish only port 443 with
`sudo tailscale funnel --bg 443` (or use a Cloudflare Tunnel to `http://127.0.0.1:8080`). Never publish 9119 or
8642. The admin console stays protected by its login. For extra safety, keep admin use to the tailnet URL.

Tailscale's `serve`/`funnel` flags have changed between versions. If a command is rejected, check
`tailscale serve --help`.

## 12. Reboots and FileVault

- **Planned restarts:** `sudo fdesetup authrestart`. The disk unlocks once, so everything comes back without anyone at
  the Mac.
- **Unplanned (power cut):** the disk stays locked until unlocked. On macOS 26, with Remote Login on and the Mac on
  **Ethernet**, SSH in before login (`ssh inagentic@<LAN IP>`) and enter the admin password to unlock and
  finish booting. Tailscale isn't running yet at that point, so you need another way onto the client's LAN (a small
  always-on device with Tailscale, or the router's VPN), or the client unlocks it at the screen.
- **Test both before handover.**

## 13. Verify

- [ ] `curl -s http://127.0.0.1:8000/api/health` shows `"mode":"hermes","reachable":true`
- [ ] Admin → **Hermes** shows *Reachable: Yes* and the model name
- [ ] The chat page answers a question, and "Learn and improve" behaves as expected
- [ ] A risky request (e.g. "delete the test folder") asks for approval. If Hermes doesn't raise approvals through
      the API for your setup, enforce them in Hermes (command approvals and blocklist) and keep the terminal on Docker.
- [ ] Telegram (if set up) replies
- [ ] `sudo fdesetup authrestart`: all four services come back
- [ ] Pull the power, then unlock remotely: all four services come back
- [ ] From a device **not** on the tailnet, ports 8642, 8000 and 9119 are unreachable

## 14. Operations

**Updates.** Test on your own Mac first, then:
```bash
sudo -iu aiagents
hermes update && hermes config check          # if config changed: hermes config migrate
cd ~/apps/agent-api && git pull && .venv/bin/pip install -e .
cd ~/apps/agent-ui && git pull && npm ci && npm run build
exit
sudo launchctl kickstart -k system/ai.agents.hermes-gateway
sudo launchctl kickstart -k system/ai.agents.agent-api
```

**Backups.**
- Time Machine to an external drive (encrypted).
- Nightly copy of the important folders (as admin, e.g. via a LaunchDaemon or cron):
  ```bash
  sudo tar -czf /Volumes/Backup/agents-$(date +%F).tgz -C /Users/aiagents .hermes apps/agent-api/data apps/agent-api/.env
  ```

**Monitoring.** Point an uptime monitor (Uptime Kuma, Healthchecks.io, Better Stack) at
`https://acme-agents.<tailnet>.ts.net/api/health`, alerting you by email or Telegram.

**Security checklist**
- [ ] `aiagents` is a standard user; only `inagentic` can SSH in, with keys only
- [ ] Hermes API, dashboard, agent-api and Caddy all bind to 127.0.0.1
- [ ] `~/.hermes/.env` and `agent-api/.env` are `chmod 600`
- [ ] Hermes terminal backend is Docker; tools are limited per channel (`hermes tools`)
- [ ] No skills installed from public hubs without reading them first
- [ ] Model provider spending limit set
- [ ] FileVault on, recovery key stored, UPS fitted
- [ ] Data processing agreement signed with the client; remote-access log kept

## 15. Troubleshooting

| Symptom | Check |
|---|---|
| Admin → Hermes shows *Reachable: No* | `sudo launchctl list \| grep hermes`; `tail -50 ~aiagents/Library/Logs/hermes-gateway.log`; `curl 127.0.0.1:8642/health` |
| *API key: Rejected* | `HERMES_API_KEY` in agent-api `.env` must equal `API_SERVER_KEY` in `~/.hermes/.env`; restart both |
| Chat replies arrive all at once | Caddy must keep `flush_interval -1` for `/api`; Tailscale/Cloudflare must not buffer |
| Login works but admin pages bounce to login | `COOKIE_SECURE=true` needs HTTPS; over plain http set it to false |
| Gateway restarts in a loop | Wrong `ProgramArguments` for your Hermes version, or port 8642 already taken by a Hermes LaunchAgent |
| Nothing comes back after a power cut | FileVault is waiting for unlock — see step 12 |
| `hermes` not found in services | The plist PATH must include `/Users/aiagents/.local/bin` |
