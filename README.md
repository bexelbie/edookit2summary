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

**Static config** (environment variables) — Azure OpenAI and SMTP settings.
See `edookit2summary.env.example` for the full list:

| Variable | Description |
|---|---|
| `AZURE_OPENAI_ENDPOINT` | Azure OpenAI base URL |
| `AZURE_OPENAI_KEY` | API key |
| `AZURE_OPENAI_DEPLOYMENT` | Model deployment name (e.g. `gpt-4o-mini`) |
| `AZURE_OPENAI_API_VERSION` | API version (e.g. `2025-01-01-preview`) |
| `SMTP_HOST` | SMTP server hostname |
| `SMTP_PORT` | SMTP server port |
| `EMAIL_FROM` | Sender address |
| `EMAIL_TO` | Recipient address |
| `EVENT_LOOKAHEAD_DAYS` | How far ahead to show upcoming events (default `60`) |

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

| Condition | Behavior |
|---|---|
| Cookies expired | Alert email sent, exit 1 |
| Translation failed | Czech text included with error note in email, exit 1 |
| SMTP failed | Output still printed to stdout, exit 1 |
| Model unavailable (idle check) | Alert email sent, exit 0 |

