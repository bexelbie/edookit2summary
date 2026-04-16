# gather-du

Fetches and parses homework assignments from edookit.net (ZŠ Husova Brno).

## Setup

```
cd gather-du
python3 -m venv .venv
.venv/bin/pip install beautifulsoup4
```

## Usage

```
.venv/bin/python3 fetch_assignment.py <assignment-url> [cookies.json]
```

Example:
```
.venv/bin/python3 fetch_assignment.py \
  "https://zshusova.edookit.net/assignments/detail?assignment=76054"
```

Output is JSON with assignment name, description, deadline, course, student,
and status.

## Cookie session management

The script uses browser session cookies to authenticate. Each request
automatically refreshes the cookies and writes them back to `cookies.json`,
so the session stays alive as long as you run the script at least once every
~14 days (the PHPSESSID Max-Age).

### Obtaining cookies (first time or after expiry)

1. Open https://zshusova.edookit.net in Chrome/Firefox
2. Log in with your Plus4U account
3. Open DevTools (F12) → **Network** tab
4. Navigate to any page on the site (e.g. the dashboard)
5. Click the first request to `zshusova.edookit.net` in the network list
6. Under **Request Headers**, find the `Cookie:` line
7. Extract these five cookies and create `cookies.json`:

```json
{
  "_nss": "1",
  "X-EdooCacheId": "<value>",
  "X-Auth-Id": "<value>",
  "PHPSESSID": "<value>",
  "uu.app.csrf": "<value>"
}
```

### How session renewal works

- `PHPSESSID` expires after ~14 days but each authenticated request resets
  the timer.
- `uu.app.csrf` changes on every response — the script captures and saves the
  new value automatically.
- `X-Auth-Id` and `X-EdooCacheId` appear to be stable across sessions.
- If cookies expire, you'll get: `RuntimeError: Not authenticated — got login
  page. Check cookies.` — just re-grab cookies per the instructions above.

