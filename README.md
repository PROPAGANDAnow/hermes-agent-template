# Hermes Agent + Paperclip — Railway Template

Deploy [Hermes Agent](https://github.com/NousResearch/hermes-agent) on [Railway](https://railway.app) with a web-based admin dashboard for configuration, gateway management, user pairing, and a bundled [Paperclip](https://github.com/paperclipai/paperclip) runtime in the same template.

[![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/deploy/hermes-agent-ai?referralCode=QXdhdr&utm_medium=integration&utm_source=template&utm_campaign=generic)

> Hermes Agent is an autonomous AI agent by [Nous Research](https://nousresearch.com/) that lives on your server, connects to your messaging channels (Telegram, Discord, Slack, etc.), and gets more capable the longer it runs.

<!-- TODO: Add dashboard screenshot -->
<!-- ![Dashboard](docs/dashboard.png) -->

## Features

- **First-Run Setup vs Manage UX** — the admin UI separates initial setup from ongoing monitoring and management
- **Merged Hermes + Paperclip Runtime** — the image installs `paperclipai` alongside Hermes so both tools are available in one Railway deployment
- **Persistent Workspace Bootstrap** — deterministic Hermes workspaces under `/data/.hermes/workspaces/{default,projects,scratch,shared}` on every boot
- **Persistent Default CWD** — generated Hermes config points terminal `cwd` at `/data/.hermes/workspaces/default` instead of `/tmp`
- **Bundled Paperclip Home + Workspace** — Paperclip persists its state in `/data/.paperclip` and gets a dedicated workspace at `/data/workspaces/paperclip`
- **Stronger Readiness Checks** — setup is only complete when workspace layout, model, provider key, and at least one channel are configured
- **Admin Dashboard** — dark-themed UI to configure providers, channels, tools, and manage the gateway
- **Gateway Management** — start, stop, restart the Hermes gateway from the browser
- **Live Status + Logs** — gateway state, setup checklist, workspace state, pairing status, and live logs
- **User Pairing** — approve or deny users who message your bot, revoke access anytime
- **Basic Auth** — password-protected admin panel for configuration and operations
- **Safe Reset** — clears saved setup/config without deleting persistent workspaces by default

## Getting Started

The easiest way to get started:

### 1. Get an LLM Provider Key (free)

1. Register for free at [OpenRouter](https://openrouter.ai/)
2. Create an API key from your [OpenRouter dashboard](https://openrouter.ai/keys)
3. Pick a free model from the [model list sorted by price](https://openrouter.ai/models?order=pricing-low-to-high) (e.g. `google/gemma-3-1b-it:free`, `meta-llama/llama-3.1-8b-instruct:free`)

### 2. Set Up a Telegram Bot (fastest channel)

Hermes Agent interacts entirely through messaging channels — there is no chat UI like ChatGPT. Telegram is the quickest to set up:

1. Open Telegram and message [@BotFather](https://t.me/BotFather)
2. Send `/newbot`, follow the prompts, and copy the **Bot Token**
3. Send a message to your new bot — it will appear as a pairing request in the admin dashboard
4. To find your Telegram user ID, message [@userinfobot](https://t.me/userinfobot)

### 3. Deploy to Railway

1. Click the **Deploy on Railway** button above
2. Set the `ADMIN_PASSWORD` environment variable (or a random one will be generated and printed to deploy logs)
3. Attach a **volume** mounted at `/data` (persists config across redeploys)
4. Open your app URL — log in with username `admin` and your password

### 4. Configure Hermes in the Admin Dashboard

On first visit, the admin dashboard opens to the **Setup** view. Setup is only considered complete when all four checks pass:

1. **Workspace layout initialized** — the wrapper bootstraps `/data/.hermes/workspaces/default`, `projects`, `scratch`, and `shared`
2. **Model configured** — set `LLM_MODEL`
3. **Provider key configured** — add at least one provider API key
4. **Channel configured** — enable at least one channel such as Telegram, Discord, or Slack

Once those are saved, the **Manage** surfaces unlock:

- **Status** shows the readiness checklist, workspace state, gateway state, providers, and channels
- **Logs** streams gateway output
- **Users** handles pairing approvals
- **Manage panels** stay available for operations like status checks, logs, terminal access, file browsing, and pairing review

### 5. Use the bundled Paperclip runtime

This template now installs Paperclip in the same container.

- `paperclipai` is installed in the image and exposed through the admin terminal
- Paperclip state persists under `/data/.paperclip`
- A dedicated Paperclip workspace is created at `/data/workspaces/paperclip`

Recommended first-run flow:

1. Finish Hermes setup in the admin UI
2. Open the **Terminal** panel
3. Run:

```bash
paperclipai onboard --yes
```

You can then inspect Paperclip state from the **Files** panel by selecting the `paperclip` root.

### 6. Start Chatting

Message your Telegram bot. If you're a new user, a pairing request will appear in the admin dashboard under **Users** — click **Approve**, and you're in.

<!-- TODO: Add Telegram chat screenshot -->
<!-- ![Telegram Example](docs/telegram-example.png) -->

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `8080` | Web server port (set automatically by Railway) |
| `ADMIN_USERNAME` | `admin` | Admin login username |
| `ADMIN_PASSWORD` | *(auto-generated)* | Admin login password — if unset, a random password is printed to logs |
| `HERMES_HOME` | `/data/.hermes` | Persistent Hermes home directory |

All model, provider, channel, and tool settings are managed through the setup UI and saved into `HERMES_HOME/.env` plus `HERMES_HOME/config.yaml`.

## Workspace Behavior

### Hermes workspaces

The wrapper bootstraps these persistent directories under `HERMES_HOME/workspaces`:

- `default` — default Hermes terminal cwd
- `projects` — long-lived project work
- `scratch` — disposable ad-hoc work
- `shared` — shared files across tasks

A wrapper-owned metadata file is also written to `HERMES_HOME/workspaces/.bootstrap.json` so the UI can display workspace state.

### Paperclip runtime paths

The merged template also creates persistent Paperclip paths:

- `PAPERCLIP_HOME=/data/.paperclip`
- `PAPERCLIP_WORKSPACE=/data/workspaces/paperclip`

Resetting config from the admin UI **does not delete Hermes workspaces or the Paperclip home**.

## Supported Providers

OpenRouter, DeepSeek, DashScope, GLM / Z.AI, Kimi, MiniMax, HuggingFace

## Supported Channels

Telegram, Discord, Slack, WhatsApp, Email, Mattermost, Matrix

## Supported Tool Integrations

Parallel (search), Firecrawl (scraping), Tavily (search), FAL (image gen), Browserbase, GitHub, OpenAI Voice (Whisper/TTS), Honcho (memory)

## Architecture

```
Railway Container
├── Python Admin Server (Starlette + Uvicorn)
│   ├── /            — setup + management UI (basic auth)
│   ├── /api/*       — config, status, logs, gateway, pairing, readiness, workspaces, paperclip runtime state
│   └── /health      — health check (no auth)
├── hermes gateway   — managed as async subprocess
└── paperclipai CLI  — bundled runtime available from the admin terminal
```

The admin server runs on `$PORT` and manages Hermes as child subprocesses. Config is stored in `/data/.hermes/.env` and `/data/.hermes/config.yaml`. Workspace state lives under `/data/.hermes/workspaces`, and the generated Hermes config uses `/data/.hermes/workspaces/default` as the default terminal cwd. Paperclip is installed in the same image, persists state in `/data/.paperclip`, and gets a dedicated workspace at `/data/workspaces/paperclip`. Gateway stdout/stderr is captured into a ring buffer and streamed to the Logs panel.

## Running Locally

```bash
docker build -t hermes-agent .
docker run --rm -it -p 8080:8080 -e PORT=8080 -e ADMIN_PASSWORD=changeme -v hermes-data:/data hermes-agent
```

Open `http://localhost:8080` and log in with `admin` / `changeme`.

## Credits

- [Hermes Agent](https://github.com/NousResearch/hermes-agent) by [Nous Research](https://nousresearch.com/)
- UI inspired by [OpenClaw](https://github.com/praveen-ks-2001/openclaw-railway) admin template
