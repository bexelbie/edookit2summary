# edookit2summary

Fetches school notifications from [edookit.net](https://zshusova.edookit.net)
(ZŠ Husova, Brno), translates them from Czech to English via Azure OpenAI,
and emails a summary. Designed to run on a systemd timer in a container.

## What it does

- Scrapes the edookit inbox for new assignments, messages, evaluations, exams,
  polls, and events
- Fetches detail pages and downloads any attachments
- Includes an upcoming events calendar (next 60 days by default) with 🆕
  markers on newly posted events
- Translates the summary from Czech to English (falls back to Czech if the
  model is unavailable)
- Emails the result as both plain text and HTML with attachments
- Tracks the last-run timestamp so only new items are processed

## Setup

### Dependencies

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### Configuration

**Edookit cookies** (`cookies.json`) — ephemeral session cookies that
auto-renew on each request. See `cookies.json.example` for the template.

**Static config** (environment variables) — LLM and SMTP settings.
See `edookit2summary.env.example` for the full list:

### General Settings

| Variable               | Description                                                  |
| ---------------------- | ------------------------------------------------------------ |
| `TARGET_LANGUAGE`      | Target language for translation (default `English`)          |
| `MAX_UPDATES`          | Max number of updates to process at once (default `50`)      |
| `EVENT_LOOKAHEAD_DAYS` | How far ahead to show upcoming events (default `60`)         |
| `LLM_MAX_RETRIES`     | Retry cycles across all LLM providers before giving up (`3`) |

### LLM: Gemini

| Variable         | Description                                                                                                    |
| ---------------- | -------------------------------------------------------------------------------------------------------------- |
| `GEMINI_API_KEY` | Google Gemini API key                                                                                          |
| `GEMINI_MODELS`  | Comma-separated model list (default `gemini-3-flash-preview,gemini-3.1-flash-lite-preview,gemini-3.1-pro-preview`) |

### LLM: Azure OpenAI

| Variable                   | Description                                                     |
| -------------------------- | --------------------------------------------------------------- |
| `AZURE_OPENAI_ENDPOINT`    | Azure OpenAI base URL                                           |
| `AZURE_OPENAI_KEY`         | Azure API key                                                   |
| `AZURE_OPENAI_DEPLOYMENT`  | Comma-separated deployment names (default `gpt-4.1-nano`)      |
| `AZURE_OPENAI_API_VERSION` | API version (e.g. `2025-01-01-preview`)                         |

### LLM failover

When both Gemini and Azure are configured, the system uses **cross-provider
failover**: Gemini models are tried first, then Azure models.  On failure the
next model is tried immediately with no delay.  After every model in every
provider has been tried once (one "cycle"), the system sleeps 2 minutes and
starts the next cycle.  It gives up after `LLM_MAX_RETRIES` cycles (default 3).

If only one provider is configured, the same retry logic applies — each
configured model is tried in order, with 2-minute pauses between cycles.

> **Known limitation — credential visibility in process listings.**
> LLM API keys are passed as HTTP headers to `curl` subprocesses.  While
> they no longer appear in URLs, header values are visible via `ps` or
> `/proc/*/cmdline` to other users on the same host.  If this is a concern,
> run the container with an isolated PID namespace (the default for
> Podman/Docker) or switch the HTTP calls to a Python library in a
> future refactor.

### SMTP Settings

| Variable     | Description                                       |
| ------------ | ------------------------------------------------- |
| `SMTP_HOST`  | SMTP server hostname                              |
| `SMTP_PORT`  | SMTP server port                                  |
| `SMTP_USER`  | SMTP username                                     |
| `SMTP_PASS`  | SMTP password                                     |
| `EMAIL_FROM` | Sender address                                    |
| `EMAIL_TO`   | Recipient address (supports comma-separated list) |

## Usage

```
# Set env vars (or source an env file)
export $(cat edookit2summary.env | xargs)

# Preview output without sending email or updating last_run
.venv/bin/python3 gather_updates.py --dry-run

# Preview rendered HTML
.venv/bin/python3 gather_updates.py --dry-run-html

# Run for real (sends email, updates last_run)
.venv/bin/python3 gather_updates.py
```

The cookies file defaults to `cookies.json` in the current directory. Pass a
different path as a positional argument if needed.

### fetch_assignment.py

Standalone tool that fetches and parses a single assignment detail page.
Output is JSON.

```
.venv/bin/python3 fetch_assignment.py \
  "https://zshusova.edookit.net/assignments/detail?assignment=76054"
```

## Container deployment

Build and run with Podman (or pull from `ghcr.io/bexelbie/edookit2summary`):

```
podman build -t edookit2summary .
podman run --rm \
  --env-file edookit2summary.env \
  -v ./data:/data:Z \
  --network your-network \
  edookit2summary
```

### Quadlet (systemd timer)

Copy `edookit2summary.container` and `edookit2summary.timer` to your quadlet
directory (e.g. `~/.config/containers/systemd/`). Edit the `.container` file
to set:

- `Volume=` — host path where `cookies.json` lives, mapped to `/data`
- `EnvironmentFile=` — path to your env file with Azure/SMTP config
- `Network=` — Podman network that can reach the SMTP server

Then:

```
systemctl --user daemon-reload
systemctl --user enable --now edookit2summary.timer
```

The timer runs daily at 15:00. Adjust `OnCalendar=` in the timer file to
change the schedule. Sessions are refreshed automatically via OIDC.

## Cookie session management

Edookit uses Plus4U OIDC authentication. The initial login must be done
manually in a browser, but subsequent session renewals happen automatically.

### How session renewal works

- The edookit OIDC token expires every ~30–60 minutes
- When the token expires, the script automatically refreshes it using the
  Plus4U identity cookie — no manual intervention needed
- The Plus4U identity cookie (`uoid.ps`) lasts ~19 days
- If the Plus4U identity cookie also expires, the tool sends an alert email
  and manual cookie refresh is required

### Obtaining cookies (first time or after Plus4U expiry)

1. Open https://zshusova.edookit.net in Chrome/Firefox
2. Log in with your Plus4U account
3. Open DevTools (F12) → **Application** → **Cookies**
4. From `zshusova.edookit.net`, copy these cookies to `cookies.json`:
   `_nss`, `X-EdooCacheId`, `X-Auth-Id`, `PHPSESSID`, `uu.app.csrf`
5. From `uuidentity.plus4u.net`, copy these cookies to the `plus4u` key
   in `cookies.json`: `uoid.ps`, `uoid.s`, `uoid.bs`

## Error handling

| Condition                                 | Behavior                                             |
| ----------------------------------------- | ---------------------------------------------------- |
| Cookies expired                           | Alert email sent, exit 1                             |
| Translation failed                        | Czech text included with error note in email, exit 1 |
| SMTP failed                               | Output still printed to stdout, exit 1               |
| SMTP credentials without TLS              | Refuses to connect, exit 1                           |
| LLM unreachable (no-updates health check) | Alert email sent, exit 0                             |
