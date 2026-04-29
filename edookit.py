# ABOUTME: Shared library for edookit.net: page fetching, parsing, translation, and email.
# ABOUTME: Handles cookie management, OIDC session refresh, Azure OpenAI, and SMTP delivery.

import json
import os
import re
import secrets
import smtplib
import subprocess
import sys
from datetime import date
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import urlencode


try:
    from bs4 import BeautifulSoup
except ImportError:
    print("Error: beautifulsoup4 required. Run: pip install beautifulsoup4", file=sys.stderr)
    sys.exit(1)


COOKIE_REFRESH_INSTRUCTIONS = """\
To refresh cookies:
  1. Open https://zshusova.edookit.net in Chrome/Firefox
  2. Log in with your Plus4U account
  3. Open DevTools (F12) → Application → Cookies
  4. From zshusova.edookit.net, copy to cookies.json:
     _nss, X-EdooCacheId, X-Auth-Id, PHPSESSID, uu.app.csrf
  5. From uuidentity.plus4u.net, copy to cookies.json under "plus4u":
     uoid.ps, uoid.s, uoid.bs"""

BASE_URL = "https://zshusova.edookit.net"

_EDOOKIT_COOKIE_KEYS = ["_nss", "X-EdooCacheId", "X-Auth-Id", "PHPSESSID", "uu.app.csrf"]

# Plus4U identity provider OIDC configuration for automatic session refresh.
_OIDC_AUTH_URL = (
    "https://uuidentity.plus4u.net"
    "/uu-oidc-maing02/bb977a99f4cc4c37a2afce3fd599d0a7/oidc/auth"
)
_OIDC_CLIENT_ID = "0fa24fa43e794de89003790253a93cb6"
_OIDC_REDIRECT_URI = "https://zshusova.edookit.net/user/oidc-login-callback"


class AuthError(RuntimeError):
    """Raised when authentication has expired."""
    pass


class ParseError(RuntimeError):
    """Raised when a page doesn't contain expected content."""
    pass


class TranslationError(RuntimeError):
    """Raised when Azure OpenAI is unavailable or misconfigured."""
    pass


# Environment variable names for static config (delivered via EnvironmentFile)
_ENV_MAP = {
    "azure_openai_endpoint":    "AZURE_OPENAI_ENDPOINT",
    "azure_openai_key":         "AZURE_OPENAI_KEY",
    "azure_openai_deployment":  "AZURE_OPENAI_DEPLOYMENT",
    "azure_openai_api_version": "AZURE_OPENAI_API_VERSION",
    "gemini_api_key":           "GEMINI_API_KEY",
    "target_language":          "TARGET_LANGUAGE",
    "smtp_host":                "SMTP_HOST",
    "smtp_port":                "SMTP_PORT",
    "smtp_user":                "SMTP_USER",
    "smtp_pass":                "SMTP_PASS",
    "email_from":               "EMAIL_FROM",
    "email_to":                 "EMAIL_TO",
    "event_lookahead_days":     "EVENT_LOOKAHEAD_DAYS",
}


def load_config():
    """Load static configuration from environment variables.

    Returns a dict with the same keys as _ENV_MAP. Missing vars are
    omitted (callers check for required keys at point of use).
    """
    config = {}
    for key, env_var in _ENV_MAP.items():
        val = os.environ.get(env_var)
        if val:
            config[key] = val
    return config


def load_cookies(cookies_file):
    """Load cookies from a JSON file."""
    with open(cookies_file) as f:
        return json.load(f)


def save_cookies(cookies, cookies_file):
    """Write cookies back to the JSON file."""
    with open(cookies_file, "w") as f:
        json.dump(cookies, f, indent=2)
        f.write("\n")


def _update_cookies_from_headers(cookies, response_headers):
    """Parse Set-Cookie headers and update the cookies dict."""
    updated = False
    for line in response_headers.splitlines():
        if not line.lower().startswith("set-cookie:"):
            continue
        raw = line.split(":", 1)[1].strip()
        name_value = raw.split(";")[0].strip()
        if "=" in name_value:
            name, value = name_value.split("=", 1)
            name = name.strip()
            if name in cookies and cookies[name] != value:
                cookies[name] = value
                updated = True
            elif name not in cookies:
                cookies[name] = value
                updated = True
    return updated


def fetch_page(url, cookies, cookies_file=None):
    """Fetch an authenticated edookit page.

    If cookies_file is provided, any refreshed cookies from the response
    are written back to keep the session alive.
    """
    cookie_str = "; ".join(
        f"{k}={cookies[k]}" for k in _EDOOKIT_COOKIE_KEYS if k in cookies
    )
    result = subprocess.run(
        ["curl", "-s", "-D", "-", "-b", cookie_str, url],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"curl failed: {result.stderr}")

    for sep in ("\r\n\r\n", "\n\n"):
        if sep in result.stdout:
            headers, body = result.stdout.split(sep, 1)
            break
    else:
        headers, body = "", result.stdout

    if cookies_file and _update_cookies_from_headers(cookies, headers):
        save_cookies(cookies, cookies_file)

    return body


def check_auth(soup):
    """Raise AuthError if the page indicates an expired session."""
    title = soup.find("title")
    if title and "Přihlašovací" in title.text:
        raise AuthError("Not authenticated — got login page. Cookies expired.")
    login_link = soup.find("a", href=lambda h: h and ("/user/login" in h or "/oidc/logout" in h))
    if login_link:
        raise AuthError("Not authenticated — redirected to login. Cookies expired.")


def refresh_oidc_session(cookies, cookies_file):
    """Refresh the edookit session via the Plus4U OIDC authorization flow.

    Replays the browser's silent token renewal: requests an authorization
    code from the Plus4U identity provider, then exchanges it at the
    edookit callback endpoint for fresh session cookies.

    Raises AuthError if the Plus4U session has also expired.
    """
    plus4u = cookies.get("plus4u")
    if not plus4u:
        raise AuthError(
            "Session expired and no Plus4U cookies available for refresh.\n"
            + COOKIE_REFRESH_INSTRUCTIONS
        )

    plus4u_cookie_str = "; ".join(f"{k}={v}" for k, v in plus4u.items())

    # Request authorization code from Plus4U
    params = urlencode({
        "response_type": "code",
        "client_id": _OIDC_CLIENT_ID,
        "redirect_uri": _OIDC_REDIRECT_URI,
        "scope": "openid",
        "state": secrets.token_urlsafe(16),
    })
    auth_url = f"{_OIDC_AUTH_URL}?{params}"

    result = subprocess.run(
        ["curl", "-s", "-D", "-", "-o", "/dev/null",
         "-b", plus4u_cookie_str, auth_url],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise AuthError(f"OIDC auth request failed: {result.stderr}")

    # Extract callback URL from Location header
    callback_url = None
    for line in result.stdout.splitlines():
        if line.lower().startswith("location:"):
            callback_url = line.split(":", 1)[1].strip()
            break

    if not callback_url or "code=" not in callback_url:
        raise AuthError(
            "Plus4U session expired — could not obtain authorization code.\n"
            + COOKIE_REFRESH_INSTRUCTIONS
        )

    # Exchange authorization code at edookit callback (follows internal redirects)
    edookit_cookie_str = "; ".join(
        f"{k}={cookies[k]}" for k in _EDOOKIT_COOKIE_KEYS if k in cookies
    )
    if not callback_url.startswith("http"):
        callback_url = BASE_URL + callback_url

    result = subprocess.run(
        ["curl", "-s", "-L", "-D", "-", "-o", "/dev/null",
         "-b", edookit_cookie_str, callback_url],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise AuthError(f"OIDC callback failed: {result.stderr}")

    if not _update_cookies_from_headers(cookies, result.stdout):
        raise AuthError(
            "OIDC token exchange produced no cookie updates.\n"
            + COOKIE_REFRESH_INSTRUCTIONS
        )

    save_cookies(cookies, cookies_file)


def keepalive(cookies, cookies_file):
    """Ensure the edookit session is alive, refreshing via OIDC if needed.

    Checks the session with a page fetch. If the OIDC token has expired,
    refreshes it using the Plus4U identity provider. Raises AuthError
    only when both sessions have expired.
    """
    html = fetch_page(BASE_URL + "/", cookies, cookies_file)
    soup = BeautifulSoup(html, "html.parser")
    try:
        check_auth(soup)
        return
    except AuthError:
        pass

    print("Session expired, attempting OIDC refresh...", file=sys.stderr)
    refresh_oidc_session(cookies, cookies_file)
    print("OIDC refresh successful.", file=sys.stderr)

    # Verify the refreshed session works
    html = fetch_page(BASE_URL + "/", cookies, cookies_file)
    soup = BeautifulSoup(html, "html.parser")
    check_auth(soup)



def parse_detail_page(html):
    """Extract fields from any edookit detail page (assignment, message, evaluation, etc.).

    Returns a dict with 'name' (from detail-object-name), label/value pairs
    from the ft_row structure, and 'attachments' (list of {name, download_url}).
    """
    soup = BeautifulSoup(html, "html.parser")
    check_auth(soup)

    fields = {}

    name_el = soup.find("span", class_="detail-object-name")
    if name_el:
        fields["name"] = name_el.get_text(strip=True)

    for row in soup.find_all("div", class_="ft_row"):
        if "translate-container" in row.get("class", []):
            continue

        label_el = row.find("span", class_="ft_c1")
        if not label_el:
            continue
        label = label_el.get_text(strip=True).rstrip(":")
        if not label:
            continue

        # Skip labels that duplicate the 'name' field
        if label in ("Assignment name", "Subject", "Event Name"):
            continue

        # Rich content (descriptions, message bodies)
        rich = row.find("div", class_="rich_content")
        if rich:
            paragraphs = rich.find_all("p")
            if paragraphs:
                fields["description"] = "\n".join(p.get_text(strip=True) for p in paragraphs)
            else:
                fields["description"] = rich.get_text(strip=True)
            continue

        value_el = row.find("span", class_="ft_c2") or row.find("div", class_="ft_c2")
        if not value_el:
            continue

        # Course field: separate link text from class/student info
        if label == "Course":
            link = value_el.find("a")
            info = value_el.find("span", class_="link_info")
            parts = []
            if link:
                parts.append(link.get_text(strip=True))
            if info:
                parts.append(re.sub(r"\s+", " ", info.get_text(strip=True)))
            fields[label] = " — ".join(parts) if parts else value_el.get_text(strip=True)
            continue

        text = value_el.get_text(" ", strip=True)
        text = re.sub(r"\s+", " ", text)
        fields[label] = text

    # Parse attachments from files_table
    attachments = []
    files_table = soup.find("div", class_="files_table")
    if files_table:
        for row in files_table.find_all("ul", class_="table_row"):
            filename_link = row.find("a", class_="more")
            download_link = row.find("a", class_="downloadLink")
            if filename_link and download_link and download_link.get("href"):
                attachments.append({
                    "name": filename_link.get_text(strip=True),
                    "download_url": download_link["href"],
                })
    fields["attachments"] = attachments

    return fields


def parse_event_date(date_str, school_year=None):
    """Parse an edookit event date string into a datetime.date.

    Extracts the start date from strings like '8. 5.', '22. 4., from 9:00
    to 11:30', 'from 2. 4. to 6. 4.', '17. 11. 2025'.

    school_year is a string like '2025/26'. When present and the date has
    no year, months Aug-Dec map to the first year, Jan-Jul to the second.
    Without school_year, assumes the nearest occurrence (current or next
    calendar year).
    """
    if not date_str:
        return None

    # Normalize whitespace (thin spaces, non-breaking spaces)
    text = re.sub(r"[\u2009\u00a0\u202f]+", " ", date_str.strip())

    # Strip leading "from " to get at the start date
    text = re.sub(r"^from\s+", "", text, flags=re.IGNORECASE)

    # Extract first d. m. or d. m. yyyy pattern
    m = re.match(r"(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})?", text)
    if not m:
        return None

    day, month = int(m.group(1)), int(m.group(2))
    year_str = m.group(3)

    if year_str:
        return date(int(year_str), month, day)

    if school_year:
        # Parse "2025/26" → (2025, 2026)
        parts = school_year.split("/")
        start_year = int(parts[0])
        end_suffix = parts[1] if len(parts) > 1 else ""
        if len(end_suffix) == 2:
            end_year = int(str(start_year)[:2] + end_suffix)
        elif len(end_suffix) == 4:
            end_year = int(end_suffix)
        else:
            end_year = start_year + 1
        year = start_year if month >= 8 else end_year
        return date(year, month, day)

    # No school year context — pick the nearest occurrence
    today = date.today()
    candidate = date(today.year, month, day)
    if (today - candidate).days > 180:
        candidate = date(today.year + 1, month, day)
    return candidate


def download_attachment(download_url, cookies, dest_dir):
    """Download an attachment to dest_dir. Returns (filepath, filename).

    Uses curl -L to follow edookit's redirect chain.
    """
    cookie_str = "; ".join(
        f"{k}={cookies[k]}" for k in _EDOOKIT_COOKIE_KEYS if k in cookies
    )

    url = BASE_URL + download_url if download_url.startswith("/") else download_url
    import os, tempfile
    # Download to temp file first, then rename using Content-Disposition
    tmp_path = os.path.join(dest_dir, "download.tmp")
    result = subprocess.run(
        ["curl", "-s", "-L", "-D", "-", "-o", tmp_path, "-b", cookie_str, url],
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Attachment download failed: {result.stderr.decode('utf-8', errors='replace')}"
        )

    # Headers may contain non-UTF-8 bytes (Czech filenames); decode as latin-1
    headers_text = result.stdout.decode("latin-1")

    # Extract filename from Content-Disposition header
    filename = None
    for line in headers_text.splitlines():
        if line.lower().startswith("content-disposition:"):
            match = re.search(r'filename="?([^";\n]+)"?', line)
            if match:
                filename = match.group(1).strip()

    if not filename:
        filename = os.path.basename(download_url)

    final_path = os.path.join(dest_dir, filename)
    os.rename(tmp_path, final_path)
    return final_path, filename


AZURE_OPENAI_CONFIG_KEYS = [
    "azure_openai_endpoint", "azure_openai_key",
    "azure_openai_deployment", "azure_openai_api_version",
]

GEMINI_CONFIG_KEYS = ["gemini_api_key"]


def _azure_openai_chat(config, messages, max_tokens=None):
    """Send a chat completion request to Azure OpenAI.

    Returns the response body as a parsed dict. Raises TranslationError
    on HTTP errors or misconfiguration.
    """
    missing = [k for k in AZURE_OPENAI_CONFIG_KEYS if not config.get(k)]
    if missing:
        env_names = [_ENV_MAP[k] for k in missing]
        raise TranslationError(
            f"Azure OpenAI not configured — missing env vars: {', '.join(env_names)}"
        )

    endpoint = config["azure_openai_endpoint"].rstrip("/")
    deployment = config["azure_openai_deployment"]
    api_version = config["azure_openai_api_version"]
    url = (
        f"{endpoint}/openai/deployments/{deployment}"
        f"/chat/completions?api-version={api_version}"
    )

    payload = {"messages": messages}
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens

    result = subprocess.run(
        [
            "curl", "-s", "-w", "\n%{http_code}",
            "-X", "POST", url,
            "-H", "Content-Type: application/json",
            "-H", f"api-key: {config['azure_openai_key']}",
            "-d", "@-",
        ],
        input=json.dumps(payload),
        capture_output=True, text=True
    )

    if result.returncode != 0:
        raise TranslationError(f"curl failed reaching Azure OpenAI: {result.stderr}")

    lines = result.stdout.rsplit("\n", 1)
    body = lines[0] if len(lines) > 1 else result.stdout
    status = lines[-1].strip() if len(lines) > 1 else ""

    if status == "404":
        raise TranslationError(
            f"Azure OpenAI deployment '{deployment}' not found. "
            "The model may have been retired. Update AZURE_OPENAI_DEPLOYMENT."
        )
    if status == "401":
        raise TranslationError(
            "Azure OpenAI authentication failed. Check AZURE_OPENAI_KEY."
        )
    if not status.startswith("2"):
        try:
            err = json.loads(body).get("error", {}).get("message", body[:200])
        except (json.JSONDecodeError, AttributeError):
            err = body[:200]
        raise TranslationError(
            f"Azure OpenAI returned HTTP {status}: {err}"
        )

    try:
        return json.loads(body)
    except json.JSONDecodeError:
        raise TranslationError(f"Azure OpenAI returned invalid JSON: {body[:200]}")


def check_llm_config(config):
    """Verify that the configured LLM is reachable.

    Sends a minimal completion request. Raises TranslationError if the
    deployment is unavailable, retired, or misconfigured.
    """
    if config.get("gemini_api_key"):
        _gemini_chat(config, text="Say OK")
    else:
        _azure_openai_chat(
            config,
            messages=[{"role": "user", "content": "Say OK"}],
            max_tokens=3,
        )

def _gemini_chat(config, text):
    """Send a chat completion request to Gemini."""
    if not config.get("gemini_api_key"):
        raise TranslationError("Gemini not configured — missing GEMINI_API_KEY")

    key = config["gemini_api_key"]
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3-flash-preview:generateContent?key={key}"

    payload = {
        "contents": [{"parts": [{"text": text}]}]
    }

    result = subprocess.run(
        [
            "curl", "-s", "-w", "\n%{http_code}",
            "-X", "POST", url,
            "-H", "Content-Type: application/json",
            "-d", "@-",
        ],
        input=json.dumps(payload),
        capture_output=True, text=True
    )

    if result.returncode != 0:
        raise TranslationError(f"curl failed reaching Gemini: {result.stderr}")

    lines = result.stdout.rsplit("\n", 1)
    body = lines[0] if len(lines) > 1 else result.stdout
    status = lines[-1].strip() if len(lines) > 1 else ""

    if not status.startswith("2"):
        raise TranslationError(f"Gemini returned HTTP {status}: {body[:200]}")

    try:
        data = json.loads(body)
        if "candidates" in data and len(data["candidates"]) > 0:
            return data["candidates"][0]["content"]["parts"][0]["text"]
        return ""
    except (json.JSONDecodeError, KeyError, IndexError):
        raise TranslationError(f"Gemini returned invalid or unexpected JSON: {body[:200]}")


def translate_text(text, config):
    """Translate Czech text to the target language using the configured LLM.

    Preserves markdown formatting. Returns the translated text, or the
    original text with a warning if translation fails.
    """
    if not text or not text.strip():
        return text

    target_lang = config.get("target_language", "English")

    messages = [
        {
            "role": "system",
            "content": (
                f"You translate Czech school notifications to {target_lang}. "
                "Context: ZŠ Husova is an elementary school in Brno, Czech Republic. "
                "The student is currently in first grade (I.B is the class section).\n\n"
                "Common Czech subject abbreviations:\n"
                "- Čj = Czech language (Český jazyk)\n"
                "- M = Mathematics (Matematika)\n"
                "- Prv = Social studies/science for early grades (Prvouka)\n"
                "- Aj = English language (Anglický jazyk)\n"
                "- Tv = Physical education (Tělesná výchova)\n"
                "- Vv = Art (Výtvarná výchova)\n"
                "- Hv = Music (Hudební výchova)\n"
                "- Pč = Crafts/practical activities (Pracovní činnosti)\n\n"
                "Common terms:\n"
                "- DÚ = homework (domácí úkol)\n"
                "- Písemná práce = written test\n"
                "- Obecné hodnocení = general assessment\n"
                "- Třídní schůzky = parent-teacher meetings\n\n"
                "Rules:\n"
                "- Preserve all markdown formatting, structure, and line breaks exactly\n"
                "- Keep all dates, times, and numbers unchanged\n"
                "- Keep all personal names unchanged (e.g., Mgr. Vladimíra Kolková)\n"
                "- Keep textbook and workbook names in Czech (e.g., Slabikář, Písanka, Živá abeceda)\n"
                "- Translate subject names in titles (e.g., 'Čj - I.B' → 'Czech - I.B')\n"
                "- Output only the translated text, no commentary"
            ),
        },
        {"role": "user", "content": text},
    ]

    try:
        if config.get("gemini_api_key"):
            prompt = messages[0]["content"] + "\n\nText to translate:\n" + messages[1]["content"]
            return _gemini_chat(config, prompt)
        else:
            resp = _azure_openai_chat(config, messages)
            return resp["choices"][0]["message"]["content"]
    except (TranslationError, KeyError, IndexError) as e:
        # Don't fail the whole run if translation breaks — return original
        # with a note
        return f"[Translation failed: {e}]\n\n{text}"


SMTP_CONFIG_KEYS = ["smtp_host", "smtp_port", "email_from", "email_to"]

_EMAIL_HTML_TEMPLATE = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Type" content="text/html; charset=utf-8">
</head>
<body style="margin:0; padding:0; background-color:#f3f4f6; \
font-family:Arial, Helvetica, sans-serif; color:#1f2937;">
<table role="presentation" width="100%%" cellpadding="0" cellspacing="0" \
border="0" style="border-collapse:collapse; background-color:#f3f4f6;">
<tr><td align="center" style="padding:24px 12px;">
<table role="presentation" width="680" cellpadding="0" cellspacing="0" \
border="0" style="width:100%%; max-width:680px; border-collapse:collapse; \
background-color:#ffffff;">
<tr><td style="padding:24px;">
%s
</td></tr>
</table>
</td></tr>
</table>
</body>
</html>"""


def render_email_html(markdown_body):
    """Convert markdown to email-safe HTML with proper structure.

    Uses nl2br so that newlines within items become visible line breaks,
    and wraps the result in a table-based email template.
    """
    import markdown as md
    html_content = md.markdown(
        markdown_body, extensions=["extra", "nl2br"]
    )
    # Style section headers and list items for readability
    styled = html_content
    styled = styled.replace(
        "<h2>",
        '<h2 style="font-size:20px; color:#111827; border-bottom:1px solid '
        '#d1d5db; padding:10px 0 6px 0; margin:24px 0 12px 0;">',
    )
    styled = styled.replace(
        "<li>",
        '<li style="margin-bottom:16px; line-height:1.6;">',
    )
    styled = styled.replace(
        "<a ",
        '<a style="color:#6b7280; text-decoration:underline;" ',
    )
    return _EMAIL_HTML_TEMPLATE % styled


def send_email(subject, markdown_body, config, attachment_paths=None):
    """Send an email with markdown body rendered to HTML, plus optional attachments.

    Sends a multipart message with both plain text and HTML parts.
    Raises RuntimeError if SMTP config is missing or sending fails.
    """
    missing = [k for k in SMTP_CONFIG_KEYS if not config.get(k)]
    if missing:
        env_names = [_ENV_MAP[k] for k in missing]
        raise RuntimeError(
            f"Email not configured — missing env vars: {', '.join(env_names)}"
        )

    from_addr = config["email_from"]
    to_addr = config["email_to"]

    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr

    # Body: plain text + HTML alternative
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(markdown_body, "plain", "utf-8"))
    alt.attach(MIMEText(render_email_html(markdown_body), "html", "utf-8"))
    msg.attach(alt)

    # Attachments
    for path in (attachment_paths or []):
        import os
        filename = os.path.basename(path)
        with open(path, "rb") as f:
            part = MIMEApplication(f.read(), Name=filename)
        part["Content-Disposition"] = f'attachment; filename="{filename}"'
        msg.attach(part)

    host = config["smtp_host"]
    port = int(config["smtp_port"])
    
    if port == 465:
        server = smtplib.SMTP_SSL(host, port)
    else:
        server = smtplib.SMTP(host, port)
        try:
            server.ehlo()
            server.starttls()
            server.ehlo()
        except smtplib.SMTPNotSupportedError:
            pass  # server doesn't support TLS, proceed anyway

    with server:
        if config.get("smtp_user") and config.get("smtp_pass"):
            server.login(config["smtp_user"], config["smtp_pass"])
        server.sendmail(from_addr, [to_addr], msg.as_string())
