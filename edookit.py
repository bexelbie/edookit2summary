# ABOUTME: Shared library for edookit.net: page fetching, parsing, translation, and email.
# ABOUTME: Handles cookie management, authentication, Azure OpenAI, and SMTP delivery.

import json
import os
import re
import smtplib
import subprocess
import sys
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


try:
    from bs4 import BeautifulSoup
except ImportError:
    print("Error: beautifulsoup4 required. Run: pip install beautifulsoup4", file=sys.stderr)
    sys.exit(1)


COOKIE_REFRESH_INSTRUCTIONS = """\
To refresh cookies:
  1. Open https://zshusova.edookit.net in Chrome/Firefox
  2. Log in with your Plus4U account
  3. Open DevTools (F12) → Network tab
  4. Click any request to zshusova.edookit.net
  5. Under Request Headers, copy the Cookie: line
  6. Update cookies.json with these keys:
     _nss, X-EdooCacheId, X-Auth-Id, PHPSESSID, uu.app.csrf"""

BASE_URL = "https://zshusova.edookit.net"


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
    "smtp_host":                "SMTP_HOST",
    "smtp_port":                "SMTP_PORT",
    "email_from":               "EMAIL_FROM",
    "email_to":                 "EMAIL_TO",
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
    cookie_keys = ["_nss", "X-EdooCacheId", "X-Auth-Id", "PHPSESSID", "uu.app.csrf"]
    cookie_str = "; ".join(
        f"{k}={cookies[k]}" for k in cookie_keys if k in cookies
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


def download_attachment(download_url, cookies, dest_dir):
    """Download an attachment to dest_dir. Returns (filepath, filename).

    Uses curl -L to follow edookit's redirect chain.
    """
    cookie_keys = ["_nss", "X-EdooCacheId", "X-Auth-Id", "PHPSESSID", "uu.app.csrf"]
    cookie_str = "; ".join(
        f"{k}={cookies[k]}" for k in cookie_keys if k in cookies
    )

    url = BASE_URL + download_url if download_url.startswith("/") else download_url
    import os, tempfile
    # Download to temp file first, then rename using Content-Disposition
    tmp_path = os.path.join(dest_dir, "download.tmp")
    result = subprocess.run(
        ["curl", "-s", "-L", "-D", "-", "-o", tmp_path, "-b", cookie_str, url],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"Attachment download failed: {result.stderr}")

    # Extract filename from Content-Disposition header
    filename = None
    for line in result.stdout.splitlines():
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
            "-d", json.dumps(payload),
        ],
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


def check_azure_openai(config):
    """Verify that the Azure OpenAI deployment is reachable.

    Sends a minimal completion request. Raises TranslationError if the
    deployment is unavailable, retired, or misconfigured.
    """
    _azure_openai_chat(
        config,
        messages=[{"role": "user", "content": "Say OK"}],
        max_tokens=3,
    )


def translate_to_english(text, config):
    """Translate Czech text to English using Azure OpenAI.

    Preserves markdown formatting. Returns the translated text, or the
    original text with a warning if translation fails.
    """
    if not text or not text.strip():
        return text

    messages = [
        {
            "role": "system",
            "content": (
                "You translate Czech school notifications to English. "
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
    with smtplib.SMTP(host, port) as server:
        server.sendmail(from_addr, [to_addr], msg.as_string())
