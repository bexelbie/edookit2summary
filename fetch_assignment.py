# ABOUTME: Prototype script to fetch and parse homework assignments from edookit.net.
# ABOUTME: Extracts assignment name, description, deadline, course, and status.

import json
import re
import subprocess
import sys
from http.cookies import SimpleCookie

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("Error: beautifulsoup4 required. Run: pip install beautifulsoup4", file=sys.stderr)
    sys.exit(1)


def load_cookies(cookies_file):
    """Load cookies from a JSON file."""
    with open(cookies_file) as f:
        return json.load(f)


def save_cookies(cookies, cookies_file):
    """Write cookies back to the JSON file."""
    with open(cookies_file, "w") as f:
        json.dump(cookies, f, indent=2)
        f.write("\n")


def update_cookies_from_headers(cookies, response_headers):
    """Parse Set-Cookie headers and update the cookies dict."""
    updated = False
    for line in response_headers.splitlines():
        if not line.lower().startswith("set-cookie:"):
            continue
        # Parse just the name=value part (before any ;)
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
    """Fetch an authenticated edookit page using curl.

    If cookies_file is provided, any refreshed cookies from the response
    are written back to keep the session alive.
    """
    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
    result = subprocess.run(
        ["curl", "-s", "-D", "-", "-b", cookie_str, url],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"curl failed: {result.stderr}")

    # curl -D - puts headers then a blank line then the body
    # Line endings vary by platform (\r\n or \n)
    for sep in ("\r\n\r\n", "\n\n"):
        if sep in result.stdout:
            headers, body = result.stdout.split(sep, 1)
            break
    else:
        headers, body = "", result.stdout

    if cookies_file and update_cookies_from_headers(cookies, headers):
        save_cookies(cookies, cookies_file)
        print("(cookies refreshed)", file=sys.stderr)

    return body


def parse_assignment(html):
    """Extract assignment fields from an edookit assignment detail page."""
    soup = BeautifulSoup(html, "html.parser")

    # Detect authentication failures: login page, redirect to login, etc.
    title = soup.find("title")
    if title and "Přihlašovací" in title.text:
        raise RuntimeError("Not authenticated — got login page. Cookies expired.")
    login_link = soup.find("a", href=lambda h: h and "/user/login" in h)
    if login_link:
        raise RuntimeError("Not authenticated — redirected to login. Cookies expired.")

    fields = {}

    # Assignment name
    name_el = soup.find("span", class_="detail-object-name")
    if name_el:
        fields["name"] = name_el.get_text(strip=True)

    # Parse all ft_row pairs (label: value)
    for row in soup.find_all("div", class_="ft_row"):
        # Skip the hidden translate-container rows
        if "translate-container" in row.get("class", []):
            continue

        label_el = row.find("span", class_="ft_c1")
        if not label_el:
            continue
        label = label_el.get_text(strip=True).rstrip(":")
        if not label:
            continue

        # Skip "Assignment name" — already captured as "name" above
        if label == "Assignment name":
            continue

        # Description uses a div with rich_content class
        rich = row.find("div", class_="rich_content")
        if rich:
            # Extract text from each <p> separately to preserve paragraph breaks
            paragraphs = rich.find_all("p")
            if paragraphs:
                fields["description"] = "\n".join(p.get_text(strip=True) for p in paragraphs)
            else:
                fields["description"] = rich.get_text(strip=True)
            fields["description_html"] = str(rich)
            continue

        value_el = row.find("span", class_="ft_c2") or row.find("div", class_="ft_c2")
        if not value_el:
            continue

        # For Course field, extract link text and info separately
        if label == "Course":
            link = value_el.find("a")
            info = value_el.find("span", class_="link_info")
            parts = []
            if link:
                parts.append(link.get_text(strip=True))
            if info:
                info_text = re.sub(r"\s+", " ", info.get_text(strip=True))
                parts.append(info_text)
            fields[label] = " — ".join(parts) if parts else value_el.get_text(strip=True)
            continue

        # Clean up whitespace (edookit uses &thinsp; etc.)
        text = value_el.get_text(" ", strip=True)
        text = re.sub(r"\s+", " ", text)

        fields[label] = text

    if not fields:
        raise RuntimeError(
            "No assignment data found in page. "
            "The page may have an unexpected format, or the assignment ID may be invalid."
        )

    return fields


COOKIE_REFRESH_INSTRUCTIONS = """\
To refresh cookies:
  1. Open https://zshusova.edookit.net in Chrome/Firefox
  2. Log in with your Plus4U account
  3. Open DevTools (F12) → Network tab
  4. Click any request to zshusova.edookit.net
  5. Under Request Headers, copy the Cookie: line
  6. Update cookies.json with these keys:
     _nss, X-EdooCacheId, X-Auth-Id, PHPSESSID, uu.app.csrf"""


def main():
    if len(sys.argv) < 2:
        print("Usage: fetch_assignment.py <url> [cookies.json]")
        print("  cookies.json should contain a JSON object with cookie key-value pairs.")
        print()
        print(COOKIE_REFRESH_INSTRUCTIONS)
        sys.exit(1)

    url = sys.argv[1]
    cookies_file = sys.argv[2] if len(sys.argv) > 2 else "cookies.json"

    try:
        cookies = load_cookies(cookies_file)
    except FileNotFoundError:
        print(f"Error: Cookie file not found: {cookies_file}", file=sys.stderr)
        print(file=sys.stderr)
        print(COOKIE_REFRESH_INSTRUCTIONS, file=sys.stderr)
        sys.exit(1)

    html = fetch_page(url, cookies, cookies_file)

    try:
        fields = parse_assignment(html)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        print(file=sys.stderr)
        print(COOKIE_REFRESH_INSTRUCTIONS, file=sys.stderr)
        sys.exit(1)

    print(json.dumps(fields, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
