# ABOUTME: Fetches edookit inbox, filters new items, retrieves details, and outputs a summary.
# ABOUTME: Tracks last-run timestamp in cookies.json to only show new items.

import json
import re
import sys
import tempfile
from datetime import datetime

from bs4 import BeautifulSoup

from edookit import (
    AuthError, TranslationError, COOKIE_REFRESH_INSTRUCTIONS, BASE_URL,
    load_cookies, save_cookies, fetch_page, check_auth, parse_detail_page,
    check_azure_openai, translate_to_english, download_attachment, send_email,
    load_config, render_email_html, keepalive, is_work_time,
)


# Item types in display order, mapped to CSS class used in the inbox
ITEM_TYPES = [
    ("assignment", "Assignments"),
    ("actionRequired", "Requires Action"),
    ("inboxMessage", "Messages"),
    ("exam", "Written Tests"),
    ("poll", "Polls"),
    ("event", "Events"),
    ("evaluation", "Assessments"),
]

# Maps inbox CSS class to the URL parameter pattern in onclick
URL_PATTERNS = {
    "assignment": re.compile(r"/assignments/detail\?assignment=(\d+)"),
    "inboxMessage": re.compile(r"/messages/detail\?message=(\d+)"),
    "exam": re.compile(r"/exams/detail\?exam=(\d+)"),
    "poll": re.compile(r"/polls/detail\?poll=(\d+)"),
    "event": re.compile(r"/timetable/detail\?event=(\d+)"),
    "evaluation": re.compile(r"/evaluation/detail\?evaluationId=(\d+)"),
}


def parse_inbox_timestamp(text):
    """Parse an edookit timestamp like '15. 4. 2026, 12:22' into a datetime.

    Edookit uses thin spaces (\\u2009 or &thinsp;) between date parts.
    """
    # Normalize whitespace: thin spaces, non-breaking spaces, etc.
    text = re.sub(r"[\u2009\u00a0\u202f]+", " ", text.strip())
    # Handle "Today" and "Yesterday" — we can't resolve these to absolute
    # dates without knowing the server's timezone, so we treat them as
    # very recent (today's date as fallback)
    if "Today" in text or "Yesterday" in text:
        now = datetime.now()
        time_match = re.search(r"(\d{1,2}):(\d{2})", text)
        if time_match:
            h, m = int(time_match.group(1)), int(time_match.group(2))
            if "Yesterday" in text:
                from datetime import timedelta
                now = now - timedelta(days=1)
            return now.replace(hour=h, minute=m, second=0, microsecond=0)
        return now

    # Try parsing various edookit date formats
    # "15. 4. 2026, 12:22" or "Mo 2. 2. 2026, 10:47"
    # Strip leading day abbreviation if present
    text = re.sub(r"^[A-Za-z]{2}\s+", "", text)
    # Normalize "15. 4. 2026, 12:22" → "15.4.2026 12:22"
    text = re.sub(r"\.\s*", ".", text)
    text = text.replace(",", " ")
    text = re.sub(r"\s+", " ", text).strip()

    for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue

    return None


def parse_inbox(html):
    """Parse the /overview/updates inbox page into a list of items.

    Returns a list of dicts with keys: type, title, url, timestamp, creator,
    description (if available), timestamp_raw.
    """
    soup = BeautifulSoup(html, "html.parser")
    check_auth(soup)

    items = []
    for div in soup.find_all("div", class_="item"):
        classes = div.get("class", [])

        # Skip archive-notification overlay divs and tutorial items
        if "archive-notification" in classes or "inbox-help" in classes:
            continue

        # Determine item type from CSS classes
        item_type = None
        for type_key, _ in ITEM_TYPES:
            if type_key in classes:
                item_type = type_key
                break
        if not item_type:
            continue

        # Extract URL from onclick
        onclick = div.get("onclick", "")
        url = None
        url_match = re.search(r'window\.location\.href="([^"]+)"', onclick)
        if url_match:
            url = url_match.group(1).replace("&amp;", "&")

        # Title from .object-name
        name_el = div.find("div", class_="object-name")
        title = re.sub(r"\s+", " ", name_el.get_text(strip=True)) if name_el else ""

        # Creator
        creator_el = div.find("div", class_="creator")
        creator = creator_el.get_text(strip=True) if creator_el else ""

        # Timestamp
        time_el = div.find("div", class_="time")
        timestamp_raw = ""
        timestamp = None
        if time_el:
            timestamp_raw = time_el.get_text(strip=True)
            timestamp = parse_inbox_timestamp(timestamp_raw)

        # Description preview (messages have this in the inbox listing)
        desc_el = div.find("div", class_="description")
        description = desc_el.get_text(strip=True) if desc_el else ""

        # Evaluation grade (shown inline in inbox)
        grade = ""
        if item_type == "evaluation":
            eval_el = div.find("span", class_="evaluation")
            if eval_el:
                grade = eval_el.get_text(strip=True)

        items.append({
            "type": item_type,
            "title": title,
            "url": url,
            "timestamp": timestamp,
            "timestamp_raw": timestamp_raw,
            "creator": creator,
            "description": description,
            "grade": grade,
        })

    return items


def filter_new_items(items, last_run):
    """Return only items newer than last_run datetime."""
    if last_run is None:
        return items
    return [i for i in items if i["timestamp"] and i["timestamp"] > last_run]


def parse_action_items(html):
    """Parse the 'Requires Action' widget from the main dashboard page.

    Returns a list of dicts with keys: type, name, info, url, icon_type.
    """
    soup = BeautifulSoup(html, "html.parser")
    container = soup.find("div", id="requires-action-container")
    if not container:
        return []

    items = []
    for item_div in container.select("div.item-container"):
        link = item_div.find("a", class_="item")
        if not link:
            continue

        url = link.get("href", "")
        name_div = link.find("div", class_="name")
        info_div = link.find("div", class_="additional-info")
        icon_div = link.find("div", class_="icon")

        name = name_div.get_text(strip=True) if name_div else ""
        info = info_div.get_text(strip=True) if info_div else ""

        # Icon class tells us what kind of action (payment, poll, etc.)
        icon_type = ""
        if icon_div:
            icon_classes = [c for c in icon_div.get("class", []) if c != "icon"]
            icon_type = icon_classes[0] if icon_classes else ""

        items.append({
            "type": "actionRequired",
            "name": name,
            "info": info,
            "url": url,
            "icon_type": icon_type,
        })

    return items


def fetch_item_detail(item, cookies, cookies_file):
    """Fetch the detail page for an inbox item and return parsed fields."""
    if not item["url"]:
        return None
    url = BASE_URL + item["url"]
    html = fetch_page(url, cookies, cookies_file)
    try:
        return parse_detail_page(html)
    except (AuthError, RuntimeError):
        return None


def _clean_course(course_str):
    """Extract just the subject abbreviation from a course string.

    Course fields look like 'Čj - Český jazyk — Čj - I.B, Stella Bula Exelbierd'.
    Returns just the short subject name, e.g. 'Čj'.
    """
    if not course_str:
        return ""
    # Take everything before the first ' - ' or ' — '
    part = re.split(r"\s+[-—]\s+", course_str)[0].strip()
    return part


def _source_line(item):
    """Format the source/attribution line with a link to the original."""
    url = BASE_URL + item["url"] if item.get("url") else ""
    ts = item["timestamp_raw"]
    if url:
        return f"  From: {item['creator']} — [{ts}]({url})"
    return f"  From: {item['creator']} — {ts}"


def _attachment_lines(detail):
    """Format attachment list if any are present."""
    attachments = (detail or {}).get("attachments", [])
    if not attachments:
        return []
    lines = []
    for att in attachments:
        lines.append(f"  Attachment: {att['name']}")
    return lines


def format_assignment(item, detail):
    """Format a single assignment item as markdown."""
    lines = []
    name = (detail or {}).get("name", item["title"])
    lines.append(f"- **{name}**")
    desc = (detail or {}).get("description", item["description"])
    if desc:
        for line in desc.splitlines():
            lines.append(f"  {line}")
    deadline = (detail or {}).get("Deadline", "")
    if deadline:
        lines.append(f"  Deadline: {deadline}")
    course = _clean_course((detail or {}).get("Course", ""))
    if course:
        lines.append(f"  Course: {course}")
    lines.extend(_attachment_lines(detail))
    lines.append(_source_line(item))
    return "\n".join(lines)


def format_message(item, detail):
    """Format a single message item as markdown."""
    lines = []
    name = (detail or {}).get("name", item["title"])
    lines.append(f"- **{name}**")
    desc = (detail or {}).get("description", item["description"])
    if desc:
        for line in desc.splitlines():
            lines.append(f"  {line}")
    lines.extend(_attachment_lines(detail))
    lines.append(_source_line(item))
    return "\n".join(lines)


def format_evaluation(item, detail):
    """Format a single evaluation/assessment item as markdown."""
    lines = []
    d = detail or {}
    # Use the detail name (actual assignment, e.g. "Diktát") over the inbox title
    name = d.get("name", "")
    course = _clean_course(d.get("Course", ""))
    grade = item.get("grade") or d.get("Assessment", "")

    # Build a compact one-liner: "Course: Name — grade"
    parts = []
    if course:
        parts.append(course)
    if name:
        parts.append(name)
    label = ": ".join(parts) if parts else item["title"]

    if grade:
        lines.append(f"- **{label}** — {grade}")
    else:
        lines.append(f"- **{label}**")
    lines.append(_source_line(item))
    return "\n".join(lines)


def format_generic(item, detail):
    """Format a generic item (exam, poll, event) as markdown."""
    lines = []
    name = (detail or {}).get("name", item["title"])
    lines.append(f"- **{name}**")
    desc = (detail or {}).get("description", item["description"])
    if desc:
        for line in desc.splitlines():
            lines.append(f"  {line}")
    date_time = (detail or {}).get("Date and time", "")
    if date_time:
        lines.append(f"  When: {date_time}")
    course = _clean_course((detail or {}).get("Course", ""))
    if course:
        lines.append(f"  Course: {course}")
    lines.extend(_attachment_lines(detail))
    lines.append(_source_line(item))
    return "\n".join(lines)


def format_action_item(action):
    """Format a single 'Requires Action' item as markdown."""
    lines = []
    url = BASE_URL + action["url"] if action.get("url") else ""
    icon = action.get("icon_type", "")
    type_label = icon.capitalize() if icon else "Action"
    if url:
        lines.append(f"- **{type_label}:** [{action['name']}]({url})")
    else:
        lines.append(f"- **{type_label}:** {action['name']}")
    if action.get("info"):
        lines.append(f"  {action['info']}")
    return "\n".join(lines)


FORMATTERS = {
    "assignment": format_assignment,
    "inboxMessage": format_message,
    "exam": format_generic,
    "poll": format_generic,
    "event": format_generic,
    "evaluation": format_evaluation,
}


def format_summary(items_by_type, details_by_url, action_items=None):
    """Build the full markdown summary."""
    sections = []

    type_labels = dict(ITEM_TYPES)

    for type_key, label in ITEM_TYPES:
        # Action items use a different data structure
        if type_key == "actionRequired":
            if action_items:
                section_lines = [f"## {label}\n"]
                for action in action_items:
                    section_lines.append(format_action_item(action))
                sections.append("\n\n".join(section_lines))
            continue

        items = items_by_type.get(type_key, [])
        if not items:
            continue

        formatter = FORMATTERS.get(type_key, format_generic)
        section_lines = [f"## {label}\n"]
        for item in items:
            detail = details_by_url.get(item["url"])
            section_lines.append(formatter(item, detail))

        sections.append("\n\n".join(section_lines))

    if not sections:
        return "No new updates.\n"

    return "\n\n".join(sections) + "\n"


def _send_alert_email(subject, body, config):
    """Send a plain-text alert email (for errors like expired cookies)."""
    if not config.get("smtp_host"):
        return
    try:
        send_email(subject, body, config)
    except Exception as e:
        print(f"Warning: alert email failed: {e}", file=sys.stderr)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Gather edookit updates")
    parser.add_argument("cookies_file", nargs="?", default="cookies.json")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print markdown to stdout, skip email and last_run update")
    parser.add_argument("--dry-run-html", action="store_true",
                        help="Print rendered HTML to stdout, skip email and last_run update")
    args = parser.parse_args()
    is_dry = args.dry_run or args.dry_run_html
    config = load_config()

    try:
        cookies = load_cookies(args.cookies_file)
    except FileNotFoundError:
        print(f"Error: Cookie file not found: {args.cookies_file}", file=sys.stderr)
        print(file=sys.stderr)
        print(COOKIE_REFRESH_INSTRUCTIONS, file=sys.stderr)
        sys.exit(1)

    # Load last_run timestamp
    last_run = None
    last_run_str = cookies.get("last_run")
    if last_run_str:
        try:
            last_run = datetime.fromisoformat(last_run_str)
        except ValueError:
            pass

    # Ensure session is alive (triggers OIDC refresh if token has expired)
    if not is_dry:
        try:
            keepalive(cookies, args.cookies_file)
            print("Session OK.", file=sys.stderr)
        except AuthError as e:
            msg = (
                f"Edookit session expired. Cookies need to be refreshed.\n\n"
                f"{COOKIE_REFRESH_INSTRUCTIONS}"
            )
            print(f"Error: {e}", file=sys.stderr)
            _send_alert_email("Edookit: cookies expired", msg, config)
            sys.exit(1)

    # Schedule check: dry-run always runs the full pipeline; otherwise
    # check whether the current time falls in a work window.
    if not is_dry and not is_work_time(last_run):
        print("Not a work window — done.", file=sys.stderr)
        sys.exit(0)

    # Fetch and parse inbox
    print("Fetching inbox...", file=sys.stderr)
    try:
        inbox_html = fetch_page(BASE_URL + "/overview/updates", cookies, args.cookies_file)
        all_items = parse_inbox(inbox_html)
    except AuthError as e:
        msg = (
            f"Edookit session expired. Cookies need to be refreshed.\n\n"
            f"{COOKIE_REFRESH_INSTRUCTIONS}"
        )
        print(f"Error: {e}", file=sys.stderr)
        print(file=sys.stderr)
        print(COOKIE_REFRESH_INSTRUCTIONS, file=sys.stderr)
        if not is_dry:
            _send_alert_email("Edookit: cookies expired", msg, config)
        sys.exit(1)

    new_items = filter_new_items(all_items, last_run)

    if not new_items:
        print("No new updates since last run.", file=sys.stderr)
        # Good time to check that the translation model is still available
        try:
            check_azure_openai(config)
            print("Azure OpenAI model OK.", file=sys.stderr)
        except TranslationError as e:
            print(f"Warning: {e}", file=sys.stderr)
            if not is_dry:
                _send_alert_email(
                    "Edookit: translation model unavailable",
                    f"The Azure OpenAI model check failed:\n\n{e}\n\n"
                    "Translation will not work until this is fixed. "
                    "Update the AZURE_OPENAI_DEPLOYMENT environment variable.",
                    config,
                )
        if not is_dry:
            cookies["last_run"] = datetime.now().isoformat(timespec="seconds")
            save_cookies(cookies, args.cookies_file)
        sys.exit(0)

    print(f"Found {len(new_items)} new item(s), fetching details...", file=sys.stderr)

    # Fetch "Requires Action" widget from the dashboard (only when there are
    # new items — action items are undated, so we include them as a reminder
    # alongside real updates but never trigger an email on their own)
    action_items = []
    try:
        dash_html = fetch_page(BASE_URL + "/", cookies, args.cookies_file)
        action_items = parse_action_items(dash_html)
        if action_items:
            print(f"Including {len(action_items)} action item(s) as reminder.", file=sys.stderr)
    except (AuthError, RuntimeError) as e:
        print(f"Warning: could not fetch action items: {e}", file=sys.stderr)

    # Group by type
    items_by_type = {}
    for item in new_items:
        items_by_type.setdefault(item["type"], []).append(item)

    # Fetch details for each item
    details_by_url = {}
    for i, item in enumerate(new_items, 1):
        if item["url"]:
            print(f"  [{i}/{len(new_items)}] {item['title']}", file=sys.stderr)
            detail = fetch_item_detail(item, cookies, args.cookies_file)
            if detail:
                details_by_url[item["url"]] = detail

    # Download attachments to a temp directory
    downloaded_files = []
    with tempfile.TemporaryDirectory(prefix="edookit_") as tmp_dir:
        for detail in details_by_url.values():
            for att in detail.get("attachments", []):
                try:
                    print(f"  Downloading: {att['name']}", file=sys.stderr)
                    filepath, filename = download_attachment(
                        att["download_url"], cookies, tmp_dir
                    )
                    downloaded_files.append(filepath)
                except RuntimeError as e:
                    print(f"  Warning: {e}", file=sys.stderr)

        # Generate summary
        output = format_summary(items_by_type, details_by_url, action_items)

        # Translate — fall back to Czech with error note if translation fails
        translation_failed = False
        print("Translating to English...", file=sys.stderr)
        translated = translate_to_english(output, config)
        if translated.startswith("[Translation failed:"):
            print("Warning: translation failed, using Czech.", file=sys.stderr)
            translation_failed = True
        output = translated

        # Print to stdout (always)
        if args.dry_run_html:
            print(render_email_html(output))
        else:
            print(output)

        # Send email unless dry-run
        email_failed = False
        if not is_dry and config.get("smtp_host"):
            subject = f"Edookit: {len(new_items)} new update(s)"
            try:
                print("Sending email...", file=sys.stderr)
                send_email(subject, output, config, downloaded_files)
                print("Email sent.", file=sys.stderr)
            except Exception as e:
                print(f"Error: email failed: {e}", file=sys.stderr)
                email_failed = True
        # Temp dir and files are cleaned up here

    # Update last_run unless dry-run (update even on failures — data was sent
    # to stdout so it's not lost, and we don't want to re-process next run)
    if not is_dry:
        newest = max(
            (i["timestamp"] for i in new_items if i["timestamp"]),
            default=datetime.now(),
        )
        cookies["last_run"] = newest.isoformat(timespec="seconds")
        save_cookies(cookies, args.cookies_file)
        print(f"Updated last_run to {cookies['last_run']}", file=sys.stderr)

    if translation_failed or email_failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
