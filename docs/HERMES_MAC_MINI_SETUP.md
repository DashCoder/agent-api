# Hermes Agent on a Mac mini — setup guide

This guide turns a Mac mini (M6, macOS 26 Tahoe) into an always-on AI agent server for a client. It runs
**Hermes Agent** with commands sandboxed in **Docker**, and connects it to **agent-ui** (chat + admin console)
through **agent-api** (Python backend).

**Time:** about 2½ hours. **You need:** the Mac on the client's network (wired Ethernet recommended, Wi-Fi works),
an SSH-capable laptop, and the accounts listed in section 1.

Hermes changes quickly. Where a command or flag might differ in your version, check it with
`hermes <command> --help`. The same applies to `tailscale` and `colima`.

---

## 0. How to read this guide: two accounts

Every step says which account runs it:

**Run as:** 🛠 admin — the `inagentic` administrator account

**Run as:** 🤖 aiagents — the `aiagents` standard (non-admin) account that runs the agents

**Why two accounts?** The agent can run commands. If it's ever tricked into running a harmful one (by a malicious
web page, email or document), the damage is limited to what its account is allowed to do. A **standard** account
can't install system software, change system settings, read other users' files or use `sudo`. So:

- **admin does system-wide things:** installing software with Homebrew, network and security settings, FileVault,
  Tailscale, and registering services with launchd. These need `sudo`.
- **aiagents owns the agent and its data:** Hermes, its config and API keys, the Docker VM, agent-api, agent-ui and
  their databases. These live in `/Users/aiagents`, where nothing else needs to reach them.

**Switching accounts.** Log in over SSH as admin only (aiagents is never allowed to log in remotely). To work as
aiagents, start a login shell for it, and type `exit` to return:

```bash
sudo -iu aiagents      # now you are aiagents, in /Users/aiagents
whoami                 # -> aiagents
exit                   # back to inagentic
```

If a step asks for `sudo` while you're aiagents, you're in the wrong account: `exit` first.

### What you end up with

```
                 Tailscale (private)  ·  optional public chat
                               │
              ┌────────────────┴─────────────────┐
              │  Caddy            127.0.0.1:8080 │  serves agent-ui + proxies /api
              └────────┬─────────────────────────┘
                       │
              ┌────────┴─────────────────────────┐
              │  agent-api        127.0.0.1:8000 │  logins, chat sessions, admin API
              └────────┬─────────────────────────┘
                       │  Bearer API_SERVER_KEY
              ┌────────┴─────────────────────────┐
              │  Hermes gateway   127.0.0.1:8642 │  agent, skills, memory, Telegram…
              │  Hermes dashboard 127.0.0.1:9119 │  admin only
              └────────┬─────────────────────────┘
                       │  docker exec
              ┌────────┴─────────────────────────┐
              │  Colima VM → Hermes container    │  where agent commands actually run
              └──────────────────────────────────┘
```

| Component | Installed by | Runs as | Starts via |
|---|---|---|---|
| Homebrew, git, Python, Node, Caddy, Colima, Docker CLI | admin | — | — |
| Tailscale | admin | root | Homebrew service (boot) |
| Colima VM (Docker engine) | aiagents | aiagents | LaunchDaemon `ai.agents.colima` |
| Hermes gateway + dashboard | aiagents | aiagents | LaunchDaemons `ai.agents.hermes-*` |
| agent-api | aiagents | aiagents | LaunchDaemon `ai.agents.agent-api` |
| agent-ui (static files) + Caddy | aiagents (build) | aiagents | LaunchDaemon `ai.agents.caddy` |

Everything listens on **127.0.0.1 only**. Nothing is reachable from the network except through Tailscale.

---

## 1. Before you start

**Hardware**
- Mac mini M6. 32 GB of memory if you'll run local models; 16 GB is enough for cloud models plus Docker.
- **Network: wired Ethernet recommended, Wi-Fi works.** Day-to-day running is fine on either.
  **Why Ethernet is recommended:**
  - *Remote unlock after a power cut.* Until FileVault is unlocked, the Mac sits at the pre-login stage. Macs have
    historically joined Wi-Fi only after a user logs in, so remote SSH unlock (section 15) didn't work over Wi-Fi in
    early macOS 26 builds. It reportedly works over Wi-Fi from macOS 26.5, but a cable is the reliable option.
  - *Steadier live calls.* A cable gives consistent latency, which matters for real-time voice and avatar
    conversations more than for text chat.

  **If you use Wi-Fi:** join the network as the **admin** account with *Remember this network* ticked, so it's
  stored system-wide rather than in one user's keychain. Then run the power-cut test in section 16 before handover.
- A small **UPS** (£60–£100). Most unplanned reboots are short power cuts, and each one locks the disk until someone
  unlocks it.

**Accounts and keys.** Put each in your password manager as you create it.
- A model provider API key (e.g. Anthropic), in the **client's** account or yours billed onward, with a **monthly
  spending limit**. The limit caps the cost of a runaway agent loop.
- A Tailscale account (the free plan is fine).
- Optional: a Telegram bot token from @BotFather.

**Mac accounts you will create**

| Full name | Account name | Type |
|---|---|---|
| InAgentic Admin | `inagentic` | Administrator |
| InAgentic AI Agents | `aiagents` | Standard |

---

## 2. macOS first setup

**Run as:** 🛠 admin

1. **Create the admin account in Setup Assistant** as *InAgentic Admin / `inagentic`*. Skip the Apple ID.
   **Why:** a personal Apple ID would sync your data onto a client's machine. If iMessage is needed later, use an
   Apple ID owned by the client.
2. **Install updates:** System Settings → General → Software Update. Turn on *Security Responses and system files*.
   **Why:** security fixes then install without waiting for you.
3. **Turn on FileVault:** System Settings → Privacy & Security → FileVault. Save the recovery key in your password
   manager and agree with the client who else holds it.
   **Why:** the Mac will hold client data, API keys and agent memory. Without disk encryption, anyone who takes the
   Mac can read all of it.
4. **Name the Mac:** System Settings → General → Sharing → Local hostname: `ina-mini-01` (InAgentic Mac mini
   no. 1; the next machine is `ina-mini-02`). Record which number belongs to which client in your password manager.
   **Why:** the name becomes its Tailscale address (`ina-mini-01.<tailnet>.ts.net`). A numbered name works before you
   know the client, stays unique across machines, and keeps the client's name out of your tailnet, logs and
   certificates. To rename later, see *Renaming the Mac* in section 17.
5. **Keep it awake and make it restart after a power cut:**
   ```bash
   sudo pmset -a sleep 0 disksleep 0 displaysleep 10 autorestart 1 womp 1
   pmset -g | egrep 'sleep|autorestart'
   ```
   **Why:** a sleeping Mac doesn't answer calls or messages. `autorestart 1` powers it back on when the electricity
   returns.
6. **Turn on the firewall:** System Settings → Network → Firewall.
   **Why:** a second safety net in case a service is ever accidentally started on a public address.
7. **Enable SSH for the admin account only, key-based:** System Settings → General → Sharing → Remote Login → On →
   *Allow access for: Only these users* → InAgentic Admin. Then:
   ```bash
   # on YOUR laptop:
   ssh-copy-id inagentic@ina-mini-01.local
   # on the Mac:
   sudo tee /etc/ssh/sshd_config.d/100-keys-only.conf >/dev/null <<'EOF'
   PasswordAuthentication no
   KbdInteractiveAuthentication no
   PermitRootLogin no
   EOF
   sudo launchctl kickstart -k system/com.openssh.sshd
   ```
   Open a **second** SSH session to check it works before closing the first.
   **Why:** keys can't be guessed, unlike passwords. Limiting SSH to the admin account means the agent account can
   never be used to log in.

## 3. Create the agents account

**Run as:** 🛠 admin

```bash
sudo sysadminctl -addUser aiagents -fullName "InAgentic AI Agents" -password -   # asks for a password
id aiagents
```

Save the password in your password manager. You'll rarely type it, because you switch with `sudo -iu aiagents`.

**Why:** this is the sandbox boundary described in section 0. Because it isn't an admin, it isn't added to the SSH
allow-list either.

## 4. Install shared tools with Homebrew

**Run as:** 🛠 admin

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile
eval "$(/opt/homebrew/bin/brew shellenv)"

brew install git python@3.12 node uv ripgrep ffmpeg caddy
```

Make Homebrew's programs available to aiagents too:

```bash
sudo -u aiagents sh -c 'echo "eval \"\$(/opt/homebrew/bin/brew shellenv)\"" >> /Users/aiagents/.zprofile'
```

**Why admin installs everything:** Homebrew writes to `/opt/homebrew`, which only admin can change. aiagents can
run every program there but can't install or replace them. That stops the agent from swapping a tool for a
malicious one. It also means the Hermes installer (section 7) finds `git`, `uv`, `ripgrep` and `ffmpeg` already
installed instead of trying to install them without permission.

## 5. Tailscale (private remote access)

**Run as:** 🛠 admin

```bash
brew install tailscale
sudo brew services start tailscale          # runs tailscaled as root at every boot
sudo tailscale up --hostname ina-mini-01     # opens a login link; approve the Mac in your tailnet
tailscale status
```

In the Tailscale admin console: enable **MagicDNS** and **HTTPS certificates**, and use access controls (ACLs) so
only your devices and the client's staff can reach this Mac.

**Why:** Tailscale gives you an encrypted private network, so you never open router ports. The command-line daemon
(rather than the App Store app) starts at boot without anyone logging in, which a headless server needs.

---

## 6. Docker (sandbox for agent commands)

### 6.1 Why Docker

Hermes can run terminal commands, edit files and execute code. With Docker, every one of those runs inside a
**throw-away Linux container** instead of directly on the Mac:

- the container can't see the Mac's files, keys or other accounts unless you share them on purpose;
- Hermes starts its container hardened: read-only root filesystem, all Linux privileges dropped, no privilege
  escalation, and a 256-process limit;
- a bad command damages a disposable container, not the client's machine.

On macOS, Docker always runs inside a small Linux virtual machine. You choose the tool that manages that VM.

### 6.2 Choose the Docker engine

| Option | Cost for business use | Notes |
|---|---|---|
| **Colima** (used here) | Free, open source | Command-line only; runs under a normal user; good for headless servers |
| Docker Desktop | Free only for businesses with under 250 staff **and** under $10M revenue; paid otherwise | GUI app; expects someone logged in |
| OrbStack | Paid licence for commercial use | Fast, polished; GUI app |

### 6.3 Install Colima and the Docker command line

**Run as:** 🛠 admin

```bash
brew install colima docker docker-compose
mkdir -p /Users/aiagents/.docker/cli-plugins
sudo ln -sfn /opt/homebrew/opt/docker-compose/bin/docker-compose /Users/aiagents/.docker/cli-plugins/docker-compose
sudo chown -R aiagents:staff /Users/aiagents/.docker
```

**Why admin:** installing software is a system change (section 4). The VM itself will belong to aiagents.

### 6.4 Create the VM and test it

**Run as:** 🤖 aiagents

```bash
sudo -iu aiagents
mkdir -p ~/agent-workspace
colima start --vm-type vz --cpu 4 --memory 8 --disk 60 \
             --mount ~/agent-workspace:w
docker context ls                 # "colima" should be marked as current (*)
docker run --rm hello-world       # prints "Hello from Docker!"
docker run --rm alpine uname -m   # aarch64 — containers run as native ARM Linux
```

What the flags do, and why:

- `--vm-type vz` uses Apple's own virtualisation framework: fastest and most efficient on Apple Silicon.
- `--cpu 4 --memory 8 --disk 60` gives the VM 4 cores, 8 GB and 60 GB. The VM only uses memory when containers need it,
  but this is its ceiling. See 6.6 for other Mac sizes.
- `--mount ~/agent-workspace:w` shares **only** this one folder with the VM. By default Colima shares your whole
  home folder, which here would include `~/.hermes/.env` and its API keys. Limiting it means even a container that
  escapes its sandbox can't reach those secrets.
- If you need an Intel-only (x86) image, add `--vz-rosetta` to run it through Rosetta.

**Why aiagents:** the VM, its socket and its containers then belong to the agent account. The admin account isn't
involved in running agent commands at all.

### 6.5 Start Colima at boot

**Run as:** 🛠 admin

Do this in section 13 together with the other services. agent-api ships
`deploy/launchd/ai.agents.colima.plist`, which runs `colima start --foreground` as aiagents at every boot and restarts it if it stops.

Test it with a full reboot (section 15). If the VM doesn't start from the LaunchDaemon on your macOS version, the
error will be in `/Users/aiagents/Library/Logs/colima.log`. As a fallback, switch the VM type to QEMU, which doesn't need a logged-in
session: as admin `brew install qemu`, then as aiagents `colima delete && colima start --vm-type qemu …`.

### 6.6 Sizing

| Mac memory | Colima `--memory` | Hermes `container_memory` | Leaves room for |
|---|---|---|---|
| 16 GB | 4 GB | 3072 MB | macOS, Hermes and apps (cloud models only) |
| 32 GB | 8 GB | 5120 MB (Hermes default) | plus an 8B local model and the avatar/voice stack |
| 64 GB | 12 GB | 8192 MB | plus larger local models |

The container limit must be lower than the VM's memory. To resize later, as aiagents: `colima stop && colima start --memory 12`.

---

## 7. Install Hermes Agent

**Run as:** 🤖 aiagents

```bash
sudo -iu aiagents
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
exec zsh -l                 # reload the shell; hermes lives in ~/.local/bin
hermes version
hermes doctor
```

If the installer reports it couldn't install something, `exit`, install it as admin with `brew install <name>`,
then run the installer again as aiagents.

**Why aiagents:** Hermes stores everything in `~/.hermes`: config, API keys, memory, skills and sessions. Installed in
the agent account, all of that is private to it.

Configure the basics:

```bash
hermes model        # pick the provider (e.g. Anthropic) and paste the API key
hermes setup        # full wizard: messaging channels such as Telegram (optional)
hermes chat -q "Hello! What tools do you have?"
```

**Multiple agents.** Create one **profile** per agent role, e.g. `hermes profile create reception`. Each has its own
memory, skills, tools, port and key. Start with one profile and add more once the first works.

## 8. Run agent commands inside Docker

**Run as:** 🤖 aiagents

```bash
hermes config set terminal.backend docker
hermes config set terminal.docker_image python:3.11-slim
hermes config set terminal.container_memory 5120      # MB; see the sizing table in 6.6
hermes config set terminal.container_cpu 2
hermes config set terminal.container_persistent true  # keep /workspace between restarts
hermes doctor
```

The same settings in `~/.hermes/config.yaml`:

```yaml
terminal:
  backend: docker
  docker_image: python:3.11-slim
  timeout: 180              # seconds per command
  container_cpu: 2
  container_memory: 5120    # MB
  container_disk: 51200     # MB
  container_persistent: true
  docker_forward_env: []    # env vars passed into the container — keep it empty unless needed
```

Test it:

```bash
hermes chat -q "Run 'uname -a' and 'whoami' in the terminal and tell me the output."
```

The answer should mention **Linux** (not Darwin/macOS), which proves commands run in the container. `docker ps`
shows the Hermes container while it's active.

**Why:** this is where the Docker sandbox actually takes effect. Hermes starts one long-lived container on first use
and runs terminal, file and code tools through it. Files the agent writes in `/workspace` persist across
conversations. Keep `docker_forward_env` empty so API keys never enter the container.

**Also limit tools:** run `hermes tools` and disable anything the client doesn't need, separately per messaging
channel. **Why:** a tool that isn't enabled can't be misused.

## 9. Enable the Hermes API server

**Run as:** 🤖 aiagents

```bash
KEY=$(openssl rand -hex 32); echo "Save this key in your password manager: $KEY"
cat >> ~/.hermes/.env <<EOF
API_SERVER_ENABLED=true
API_SERVER_HOST=127.0.0.1
API_SERVER_PORT=8642
API_SERVER_KEY=$KEY
EOF
chmod 600 ~/.hermes/.env
```

**Why:** agent-api talks to Hermes through this API. `127.0.0.1` keeps it off the network, the key stops other local
programs using it, and `chmod 600` makes the file readable by aiagents only.

Test in the foreground:

```bash
hermes gateway             # look for: API server listening on http://127.0.0.1:8642
```

In a second SSH session (admin, then `sudo -iu aiagents`):

```bash
KEY=$(grep API_SERVER_KEY ~/.hermes/.env | cut -d= -f2)
curl -s http://127.0.0.1:8642/health
curl -s http://127.0.0.1:8642/v1/models -H "Authorization: Bearer $KEY"
curl -sN http://127.0.0.1:8642/v1/responses -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
     -d '{"model":"hermes-agent","input":"Say hi","conversation":"test","stream":true}'
```

Stop it with Ctrl-C; it runs as a service from section 13. Check the web dashboard the same way:

```bash
hermes dashboard --no-open --port 9119
# If it says web extras are missing, run the install command it prints, then try again.
```

The dashboard **has no login**, which is why it only ever listens on localhost and is reached through Tailscale.

## 10. Install agent-api

**Run as:** 🤖 aiagents

```bash
mkdir -p ~/apps && cd ~/apps
git clone https://github.com/YOUR-ORG/agent-api.git && cd agent-api
/opt/homebrew/bin/python3.12 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env && chmod 600 .env
.venv/bin/python -m app.cli hash-password                       # prints ADMIN_PASSWORD_HASH=...
python3 -c "import secrets;print(secrets.token_urlsafe(48))"    # value for SECRET_KEY
```

Edit `.env` (`nano .env`):

```ini
HERMES_MODE=hermes
HERMES_API_URL=http://127.0.0.1:8642
HERMES_API_KEY=<the key from section 9>
HERMES_DASHBOARD_URL=https://ina-mini-01.<tailnet>.ts.net:9119   # after section 14
SECRET_KEY=<random value>
COOKIE_SECURE=true
CORS_ORIGINS=https://ina-mini-01.<tailnet>.ts.net
ADMIN_USERNAME=admin
ADMIN_PASSWORD_HASH=<from hash-password>
ADMIN_PASSWORD=
DEV_SHOW_CODES=false
```

**Why:** agent-api is the only thing that holds the Hermes key; browsers never see it. The password is stored as a
hash, `SECRET_KEY` signs login cookies, and `COOKIE_SECURE=true` only sends them over HTTPS.

Quick test (with `hermes gateway` running in the other session):

```bash
.venv/bin/uvicorn app.main:app --port 8000 &
curl -s http://127.0.0.1:8000/api/health        # "mode":"hermes", "reachable":true
kill %1
```

## 11. Build agent-ui

**Run as:** 🤖 aiagents

```bash
cd ~/apps
git clone https://github.com/YOUR-ORG/agent-ui.git && cd agent-ui
npm ci && npm run build            # creates dist/
```

**Why:** the UI is just static files once built, which Caddy serves. No Node process needs to run in production.

## 12. Check the web server config

**Run as:** 🤖 aiagents

```bash
caddy validate --config ~/apps/agent-api/deploy/Caddyfile
```

`deploy/Caddyfile` serves `~/apps/agent-ui/dist` and passes `/api` to agent-api on **127.0.0.1:8080**, with
streaming switched on so replies appear word by word.

**Why one origin:** the UI and the API share the same address, so login cookies work without cross-site settings.

## 13. Run everything as services

**Run as:** 🛠 admin

```bash
exit     # if you are still aiagents
cd /Users/aiagents/apps/agent-api/deploy/launchd
sudo cp ai.agents.*.plist /Library/LaunchDaemons/
sudo chown root:wheel /Library/LaunchDaemons/ai.agents.*.plist
sudo chmod 644 /Library/LaunchDaemons/ai.agents.*.plist
for s in colima hermes-gateway hermes-dashboard agent-api caddy; do
  sudo launchctl bootstrap system /Library/LaunchDaemons/ai.agents.$s.plist
done
sudo launchctl list | grep ai.agents      # each line should show a process ID
```

**Why LaunchDaemons, installed by admin:** a LaunchDaemon starts at **boot**, before anyone logs in. That matters
because FileVault prevents automatic login, so login-based services (LaunchAgents, `brew services` as a user) would
never start on a headless server. Each plist has `UserName aiagents`, so the service still runs **without** admin
rights. Only root can register system services, hence admin. `KeepAlive` restarts a service if it crashes.

| Task | Command (admin) |
|---|---|
| Logs | `tail -f /Users/aiagents/Library/Logs/<name>.log` |
| Restart one | `sudo launchctl kickstart -k system/ai.agents.agent-api` |
| Stop / unregister | `sudo launchctl bootout system/ai.agents.agent-api` |
| Status | `sudo launchctl print system/ai.agents.hermes-gateway \| grep state` |

Two things to check:

- If you ran `hermes gateway start` or `hermes gateway install` earlier, Hermes may have created its own LaunchAgent
  (`/Users/aiagents/Library/LaunchAgents/ai.hermes.gateway.plist`). Remove it so two gateways don't fight over
  port 8642.
- If the gateway exits straight away, check the foreground command for your Hermes version
  (`hermes gateway --help`) and fix `ProgramArguments` in the plist.

The gateway plist sets `DOCKER_HOST=unix:///Users/aiagents/.colima/default/docker.sock`, so Hermes finds Colima
even without a login shell. If the gateway starts before Colima is ready that's fine: Hermes only starts its
container when the first command runs.

## 14. Remote access

**Run as:** 🛠 admin

```bash
# Chat + admin console, private to your tailnet:
sudo tailscale serve --bg --https=443 http://127.0.0.1:8080
# Full Hermes dashboard, private to your tailnet:
sudo tailscale serve --bg --https=9119 http://127.0.0.1:9119
tailscale serve status
```

- Chat: `https://ina-mini-01.<tailnet>.ts.net/`
- Admin console: `https://ina-mini-01.<tailnet>.ts.net/admin`
- Hermes dashboard: `https://ina-mini-01.<tailnet>.ts.net:9119`. Put this in agent-api's `HERMES_DASHBOARD_URL`, then
  restart agent-api. **Admin → Hermes** then shows an *Open full Hermes dashboard* button.

**Why Tailscale Serve:** it adds HTTPS and only lets devices in your tailnet connect, which is the protection the
login-less Hermes dashboard needs.

**Public chat for the client's customers** (optional): publish only port 443 with `sudo tailscale funnel --bg 443`, or use
a Cloudflare Tunnel to `http://127.0.0.1:8080`. **Never** publish 8642, 8000 or 9119. The admin console still needs
its login, but keep admin use to the tailnet address where possible.

Tailscale's `serve` and `funnel` options have changed between versions. If a command is rejected, check
`tailscale serve --help`.

## 15. Reboots and FileVault

**Run as:** 🛠 admin

- **Planned restart:** `sudo fdesetup authrestart`. It unlocks the disk once for the next boot, so every service comes
  back with no one at the Mac.
- **Unplanned (power cut, crash):** the disk stays locked and **no services run** until it is unlocked. On macOS 26,
  with Remote Login on and the Mac on **Ethernet** (Wi-Fi only if your version supports it, see section 1), you can SSH in
  before login (`ssh inagentic@<LAN IP>`) and type the
  admin password to unlock it and finish booting. Tailscale isn't running yet at that point, so you need another way
  onto the client's network (a small always-on device with Tailscale, or the router's VPN). Otherwise the client
  types the password at the Mac.

**Why:** this is the trade-off of FileVault. Encryption protects the data if the Mac is stolen, at the cost of
someone unlocking it after an unplanned restart. A UPS makes that rare.

## 16. Verify before handover

- [ ] `curl -s http://127.0.0.1:8000/api/health` shows `"mode":"hermes"` and `"reachable":true`
- [ ] Admin → **Hermes** shows *Reachable: Yes* and the model name
- [ ] Asking the agent to run `uname -a` reports **Linux** (Docker sandbox active), and `docker ps` (as aiagents)
      shows the Hermes container
- [ ] The chat page answers questions; the avatar and quick replies work
- [ ] A risky request ("delete the test folder") asks for approval. If your Hermes version doesn't send approvals
      through the API, enforce approvals in Hermes itself and keep commands in Docker.
- [ ] Telegram replies (if set up)
- [ ] `sudo fdesetup authrestart`: all five services come back by themselves
- [ ] Pull the power plug, restore it, unlock remotely: all five services come back. **On Wi-Fi this test is
      essential:** if you can't SSH in before login, connect Ethernet, or agree with the client that they type the
      admin password at the Mac after an unplanned outage
- [ ] From a device **outside** the tailnet, ports 8642, 8000, 9119 and 8080 can't be reached

## 17. Operations

**Updates.** Test each update on your own Mac first.

```bash
# as admin: system tools
brew update && brew upgrade
# as aiagents: the agent and apps
sudo -iu aiagents
hermes update && hermes config check          # if it reports changes: hermes config migrate
cd ~/apps/agent-api && git pull && .venv/bin/pip install -e .
cd ~/apps/agent-ui && git pull && npm ci && npm run build
exit
# as admin: restart services
sudo launchctl kickstart -k system/ai.agents.hermes-gateway
sudo launchctl kickstart -k system/ai.agents.agent-api
```

**Why the split:** admin owns the shared tools in `/opt/homebrew`; aiagents owns the agent and app code.

**Backups.** Time Machine to an encrypted external drive, plus a nightly copy of the agent's state:

```bash
# as admin (e.g. from a LaunchDaemon or cron):
sudo tar -czf /Volumes/Backup/agents-$(date +%F).tgz -C /Users/aiagents \
     .hermes apps/agent-api/data apps/agent-api/.env agent-workspace
```

The archive contains API keys, so keep the backup drive encrypted.

**Renaming the Mac** (optional, e.g. to the client's name). As admin:

```bash
sudo scutil --set ComputerName "acme-agents"
sudo scutil --set LocalHostName "acme-agents"
sudo scutil --set HostName "acme-agents"
sudo tailscale set --hostname acme-agents          # or rename it in the Tailscale admin console
```

Then update `HERMES_DASHBOARD_URL` and `CORS_ORIGINS` in `/Users/aiagents/apps/agent-api/.env`, restart agent-api
(`sudo launchctl kickstart -k system/ai.agents.agent-api`), and re-run the two `tailscale serve` commands if the HTTPS
address doesn't follow. Account names and services don't change.

**Monitoring.** Point an uptime monitor (Uptime Kuma, Healthchecks.io, Better Stack) at
`https://ina-mini-01.<tailnet>.ts.net/api/health` and have it alert you by email or Telegram.

**Security checklist**
- [ ] `aiagents` is a standard user and is not allowed to SSH in
- [ ] SSH is key-only for `inagentic`
- [ ] Hermes API, dashboard, agent-api and Caddy listen on 127.0.0.1 only
- [ ] `~/.hermes/.env` and `agent-api/.env` are `chmod 600`
- [ ] Hermes terminal backend is `docker`; Colima shares only `~/agent-workspace`; `docker_forward_env` is empty
- [ ] Tools limited per channel (`hermes tools`); no skills from public hubs without reading them first
- [ ] Model provider spending limit set
- [ ] FileVault on, recovery key stored, UPS fitted
- [ ] Data processing agreement signed with the client; remote-access log kept

## 18. Troubleshooting

| Symptom | What to check |
|---|---|
| Admin → Hermes shows *Reachable: No* | `sudo launchctl list \| grep hermes`; `tail -50 /Users/aiagents/Library/Logs/hermes-gateway.log`; `curl 127.0.0.1:8642/health` |
| *API key: Rejected* | `HERMES_API_KEY` in agent-api's `.env` must equal `API_SERVER_KEY` in `~/.hermes/.env`; restart both services |
| Agent says it can't run commands / Docker errors | As aiagents: `colima status`, `docker ps`. Check `colima.log`. Confirm `DOCKER_HOST` in the gateway plist |
| `uname -a` reports Darwin instead of Linux | `terminal.backend` isn't `docker`: check `hermes config get terminal.backend`, then restart the gateway |
| Colima doesn't start after a reboot | See 6.5: check `colima.log`, then try `--vm-type qemu` |
| Container runs out of memory | Raise Colima's `--memory` and Hermes' `container_memory` together (table 6.6) |
| Chat replies appear all at once | Caddy must keep `flush_interval -1` for `/api`; the tunnel mustn't buffer |
| Admin login loops back to the login page | `COOKIE_SECURE=true` requires HTTPS; use the Tailscale `https://` address |
| Gateway restarts in a loop | Wrong `ProgramArguments` for your Hermes version, or a Hermes LaunchAgent already holds port 8642 |
| Nothing comes back after a power cut | The disk is waiting for the FileVault unlock (section 15) |
| `hermes: command not found` in a service | The plist's `PATH` must include `/Users/aiagents/.local/bin` |
| `sudo` asks for aiagents' password | You're in the aiagents shell; `exit` back to admin |

---

## Appendix: commands by account

**🛠 admin (`inagentic`)**: system and services

```bash
brew install …                                   # all software installs
sudo pmset …; sudo fdesetup authrestart          # power and reboots
sudo tailscale …                                 # remote access
sudo launchctl bootstrap|kickstart|bootout system/ai.agents.<name>
sudo -iu aiagents                                # switch to the agent account
```

**🤖 aiagents**: the agent and its apps

```bash
colima start|stop|status; docker ps               # Docker VM and containers
hermes model|tools|config|profile|doctor|update   # Hermes
cd ~/apps/agent-api && .venv/bin/pip install -e . # backend
cd ~/apps/agent-ui && npm ci && npm run build     # frontend
```
