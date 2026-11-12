# Plugin manifest: `inagentic-plugin.json`

Put this file at the root of a plugin repo so the admin **Plugins** page can check and install it from git.

```json
{
  "name": "sales",
  "title": "Sam",
  "description": "Sales pipeline from your CRM.",
  "version": "0.3.0",
  "pluginApi": [1],
  "uiPluginApi": [1, 2],
  "python": "api/sales",
  "ui": "ui/plugins/index.ts",
  "settings": [
    { "name": "SALES_CRM_TOKEN", "label": "CRM token", "secret": true, "required": true, "help": "Read-only token" }
  ]
}
```

| Field | Meaning |
|---|---|
| `name` | Plugin key: lowercase letters, digits, `-`, `_`. Must match the `agent_api.plugins` entry point. |
| `python` | Folder with the plugin's `pyproject.toml` (default: every `api/*/`). |
| `ui` | Optional admin pages: a file whose default export is a list of `AgentUIPlugin` (default `ui/plugins/index.ts`). |
| `settings` | Values asked for on install, written to agent-api's `.env` (mode 600). Names: `UPPER_CASE`. |

Without a manifest the page reads the name and version from `api/*/pyproject.toml` and shows a warning.

## How install works

1. **Check**: agent-api shallow-clones the repo into `~/apps/_staging/` and reads the manifest. Only sources in
   `PLUGIN_SOURCES` are allowed (default `github.com/DashCoder`). Private repos use the token in
   `~/.config/inagentic/github-token`.
2. **Install**: a job file goes to `~/apps/jobs/`. The `ai.agents.installer` LaunchDaemon runs `run-jobs.sh`, which
   calls `update.sh --add <url> --ref <ref> --commit <sha> --name <key> --auto-rollback`.
3. update.sh takes a backup, installs exactly that commit, rebuilds the UI, restarts agent-api and health-checks it.
   If the check fails, it puts the backup back on its own.

A plugin runs inside agent-api with full access to its data and tools. Install only code you trust.
