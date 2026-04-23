# Fork Change Log

This file tracks *only* changes added in the `PROPAGANDAnow/hermes-agent-template` fork beyond upstream `praveen-ks-2001/hermes-agent-template`.

## How to use this file

- Update this file in every PR that changes fork-only behavior.
- Keep entries grouped by shipped change, not by tiny implementation detail.
- For each entry, capture:
  - what we added or changed
  - why the fork needs it
  - which files are most likely to conflict on future upstream merges

## Current fork-only changes

### 2026-04-21 — Admin UI extensions

1. **Expanded generated Hermes config defaults**
   - Added richer default `config.yaml` generation so the template boots with sane Hermes UI/admin defaults.
   - Primary file: `server.py`
   - Merge risk: upstream changes to config bootstrapping or env serialization.

2. **Terminal admin screen**
   - Added an in-browser terminal view and server endpoints for interactive admin access.
   - Primary files: `server.py`, `templates/index.html`
   - Merge risk: upstream changes to auth, route prefixes, or setup UI shell.

3. **Cron management admin screen**
   - Added cron listing, pause/resume/run/remove controls, and cron output viewing in the admin UI.
   - Primary files: `server.py`, `templates/index.html`
   - Merge risk: upstream changes to route prefixes, scheduler integration, or admin navigation.

4. **File explorer and editor tab**
   - Added server-backed browsing/editing for selected filesystem roots from the admin UI.
   - Primary files: `server.py`, `templates/index.html`
   - Merge risk: upstream changes to setup UI structure, auth flow, or file-management assumptions.

5. **Hermes config defaults for UI / Slack home defaults**
   - Added fork-specific config defaults and Slack home-channel defaulting to improve zero-config startup.
   - Primary file: `server.py`
   - Merge risk: upstream changes to env var definitions or config rendering.

6. **Admin UI follow-up fixes**
   - Restored the closing style tag and hardened follow-up behavior around terminal and cron UI flows.
   - Primary file: `templates/index.html` (plus `server.py` follow-up handling)
   - Merge risk: upstream changes to front-end structure or client-side state management.

7. **Start WebUI from entrypoint when present**
   - Added optional startup of an external Hermes WebUI launcher script from `start.sh` when present.
   - Primary file: `start.sh`
   - Merge risk: upstream changes to startup/bootstrap behavior.

## Upstream merge notes

When syncing from upstream, pay special attention to these historically conflict-prone files:

- `server.py`
- `templates/index.html`
- `start.sh`

These files mix upstream setup/dashboard/auth behavior with fork-specific admin extensions.
