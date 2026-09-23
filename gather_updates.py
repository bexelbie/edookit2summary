# ABOUTME: Fetches edookit inbox, filters new items, retrieves details, and outputs a summary.
# ABOUTME: Tracks delivered item identities in cookies.json to avoid duplicates.

import json
import re
import sys
import tempfile
from datetime import datetime, date, timedelta
from urllib.parse import urlsplit, urlunsplit
from datetime import datetime, date, time, timedelta, timezone
from zoneinfo import ZoneInfo

PRAGUE_TZ = ZoneInfo("Europe/Prague")
SEEN_ITEMS_KEY = "seen_items"
SEEN_ITEMS_RETENTION_DAYS = 90
SEEN_ITEMS_MAX_COUNT = 500
BOOTSTRAP_WINDOW = timedelta(hours=24)

from bs4 import BeautifulSoup

from edookit import (
    AuthError, TranslationError, COOKIE_REFRESH_INSTRUCTIONS, BASE_URL,
    load_cookies, save_cookies, fetch_page, check_auth, parse_detail_page,
    parse_event_date,
    check_llm_config, translate_text, download_attachment, send_email,
    load_config, render_email_html, keepalive, build_translation_prompt,
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


def _normalize_edookit_url(url):
    """Return a mail-safe Edookit URL path.

    Edookit sometimes emits relative paths without a leading slash.
    """
    if not url:
        return url

    url = url.replace("&amp;", "&").strip()
    parts = urlsplit(url)
    if parts.scheme or parts.netloc:
        return url

    path = parts.path or ""
    if path and not path.startswith("/"):
        path = "/" + path

    return urlunsplit(("", "", path, parts.query, parts.fragment))


def _normalize_timestamp(ts):
    """Return a Prague-aware datetime for comparisons and persistence."""
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=PRAGUE_TZ)
    return ts.astimezone(PRAGUE_TZ)


def _now_in_prague():
    """Return the current time with Prague tzinfo for consistent comparisons."""
    return datetime.now(PRAGUE_TZ)


def parse_inbox_timestamp(text):
    """Parse an edookit timestamp like '15. 4. 2026, 12:22' into a datetime.

    Edookit uses thin spaces (\\u2009 or &thinsp;) between date parts.
    Relative labels are resolved in Prague time for deterministic UTC filtering.
    """
    # Normalize whitespace: thin spaces, non-breaking spaces, etc.
    text = re.sub(r"[\u2009\u00a0\u202f]+", " ", text.strip())
    if "Today" in text or "Yesterday" in text:
        now = datetime.now(PRAGUE_TZ)
        time_match = re.search(r"(\d{1,2}):(\d{2})", text)
        if time_match:
            h, m = int(time_match.group(1)), int(time_match.group(2))
            if "Yesterday" in text:
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
            return _normalize_timestamp(datetime.strptime(text, fmt))
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
            url = _normalize_edookit_url(url_match.group(1))

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


def _item_identity(item):
    """Return the stable identity used to track delivered inbox items."""
    return _normalize_edookit_url(item.get("url", ""))


def _prune_seen_items(seen_items, now=None):
    """Keep delivered item identities within the age and count bounds."""
    now = now or _now_in_prague()
    cutoff = now - timedelta(days=SEEN_ITEMS_RETENTION_DAYS)
    retained = {}
    for identity, recorded_at in seen_items.items():
        try:
            timestamp = _normalize_timestamp(datetime.fromisoformat(recorded_at))
        except (TypeError, ValueError):
            continue
        if timestamp >= cutoff:
            retained[identity] = timestamp.isoformat(timespec="seconds")

    return dict(
        sorted(retained.items(), key=lambda entry: entry[1], reverse=True)
        [:SEEN_ITEMS_MAX_COUNT]
    )


def filter_new_items(items, seen_items):
    """Return inbox items not already delivered."""
    return [
        i for i in items
        if _item_identity(i) and _item_identity(i) not in seen_items
    ]


def _stored_timestamp(value):
    """Parse a stored timestamp, returning None for invalid values."""
    try:
        return _normalize_timestamp(datetime.fromisoformat(value))
    except (TypeError, ValueError):
        return None


def bootstrap_seen_items(items, seen_items, last_run=None, now=None):
    """Seed the identity ledger and return the timestamp used as its boundary."""
    if seen_items:
        return seen_items, None

    now = now or _now_in_prague()
    boundary = _stored_timestamp(last_run)
    if boundary is None:
        boundary = now - BOOTSTRAP_WINDOW

    seeded = dict(seen_items)
    for item in items:
        timestamp = item.get("timestamp")
        identity = _item_identity(item)
        if identity and (not timestamp or _normalize_timestamp(timestamp) <= boundary):
            seeded[identity] = now.isoformat(timespec="seconds")

    return seeded, boundary


def _item_timestamp_in_utc(item):
    """Convert an inbox timestamp to UTC for deterministic date filtering."""
    ts = item.get("timestamp")
    if not ts:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=PRAGUE_TZ)
    return ts.astimezone(timezone.utc)


def filter_items_for_utc_date(items, utc_date):
    """Return only items whose timestamp falls on the supplied UTC calendar day."""
    if not isinstance(utc_date, date):
        utc_date = date.fromisoformat(str(utc_date))

    start = datetime.combine(utc_date, time.min, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    return [
        item for item in items
        if item.get("timestamp") and start <= _item_timestamp_in_utc(item) < end
    ]


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

        url = _normalize_edookit_url(link.get("href", ""))
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


def parse_upcoming_events(html):
    """Parse the /timetable/upcoming page into a list of upcoming events.

    Returns (events, school_year) where events is a list of dicts with keys:
    title, description, url, event_date_str, creator, created_str.
    school_year is a string like '2025/26' from the term selector.
    """
    soup = BeautifulSoup(html, "html.parser")
    check_auth(soup)

    # Extract school year from the term selector
    school_year = None
    term_el = soup.find("a", class_="selected", id="selected-term")
    if term_el:
        school_year = term_el.get_text(strip=True)

    events = []
    table = soup.find("div", class_="events_table")
    if not table:
        return events, school_year

    for row in table.find_all("ul", class_="table_row"):
        # Skip header rows (no onclick)
        onclick = row.get("onclick", "")
        if not onclick:
            continue

        url = None
        url_match = re.search(r'window\.location\.href\s*=\s*["\']([^"\']+)', onclick)
        if url_match:
            url = _normalize_edookit_url(url_match.group(1))

        cols = row.find_all("li", recursive=False)
        if len(cols) < 3:
            continue

        # Column 0: event date
        date_col = cols[0]
        # The date is in the first <b> tag
        bold = date_col.find("b")
        event_date_str = " ".join(bold.get_text().split()) if bold else ""

        # Column 1: title + description
        desc_col = cols[1]
        title_el = desc_col.find("span")
        title = title_el.get_text(strip=True) if title_el else ""

        # Description is in the second <p> (after the title div)
        desc_ps = desc_col.find_all("p")
        description = ""
        if len(desc_ps) >= 2:
            description = desc_ps[-1].get_text(strip=True)

        # Column 2: creator + created date
        creator_col = cols[2]
        creator_b = creator_col.find("b")
        creator = creator_b.get_text(strip=True) if creator_b else ""

        events.append({
            "title": title,
            "description": description,
            "url": url,
            "event_date_str": event_date_str,
            "creator": creator,
        })

    return events, school_year


_DEFAULT_LOOKAHEAD_DAYS = 60


def fetch_upcoming_events(cookies, cookies_file, config):
    """Fetch upcoming events within the look-ahead window.

    Returns a list of event dicts sorted by date, each with an added
    'event_date' (datetime.date) key.
    """
    lookahead = int(config.get("event_lookahead_days", _DEFAULT_LOOKAHEAD_DAYS))
    today = date.today()
    cutoff = today + timedelta(days=lookahead)

    html = fetch_page(BASE_URL + "/timetable/upcoming", cookies, cookies_file)
    events, school_year = parse_upcoming_events(html)

    result = []
    for ev in events:
        ev_date = parse_event_date(ev["event_date_str"], school_year)
        if ev_date and today <= ev_date <= cutoff:
            ev["event_date"] = ev_date
            result.append(ev)

    result.sort(key=lambda e: e["event_date"])
    return result




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
    date_time = (detail or {}).get("Date and time") or (detail or {}).get("Date", "")
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


def format_upcoming_event(event, is_new=False):
    """Format a single upcoming event as markdown.

    Events from /timetable/upcoming have a different structure than inbox
    items — they carry event_date_str, title, description, url directly.
    """
    lines = []
    marker = "🆕 " if is_new else ""
    url = BASE_URL + event["url"] if event.get("url") else ""
    if url:
        lines.append(f"- {marker}**[{event['title']}]({url})**")
    else:
        lines.append(f"- {marker}**{event['title']}**")
    if event.get("description"):
        for line in event["description"].splitlines():
            lines.append(f"  {line}")
    if event.get("event_date_str"):
        lines.append(f"  When: {event['event_date_str']}")
    return "\n".join(lines)


FORMATTERS = {
    "assignment": format_assignment,
    "inboxMessage": format_message,
    "exam": format_generic,
    "poll": format_generic,
    "event": format_generic,
    "evaluation": format_evaluation,
}


def format_summary(items_by_type, details_by_url, action_items=None,
                   upcoming_events=None, new_event_urls=None):
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

        # Events section: merge new inbox events with upcoming calendar
        if type_key == "event" and upcoming_events:
            new_urls = new_event_urls or set()
            section_lines = [f"## {label}\n"]
            for event in upcoming_events:
                is_new = event.get("url", "") in new_urls
                section_lines.append(format_upcoming_event(event, is_new))
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


def build_test_config(config):
    """Build the Azure-only config used for the optional test email lane.

    Each AZURE_TEST_* value falls back independently to the matching
    AZURE_OPENAI_* setting so test runs can override only the model details
    they need.
    """
    test_config = dict(config)
    test_config.update({
        "email_to": config.get("email_test") or config.get("email_to"),
        "azure_openai_endpoint": (
            config.get("azure_test_endpoint")
            or config.get("azure_openai_endpoint")
        ),
        "azure_openai_key": (
            config.get("azure_test_key")
            or config.get("azure_openai_key")
        ),
        "azure_openai_deployment": (
            config.get("azure_test_deployment")
            or config.get("azure_openai_deployment")
        ),
    })
    test_config["gemini_api_key"] = ""
    test_config["gemini_models"] = ""
    return test_config


def _has_complete_test_azure_config(config):
    """Return True when the effective Azure test config is usable."""
    test_config = build_test_config(config)
    required_fields = (
        "azure_openai_endpoint",
        "azure_openai_key",
    )
    return all(test_config.get(field) for field in required_fields)


def send_test_email(subject, summary_markdown, config, downloaded_files):
    """Send the optional Azure-only test email using the same attachments."""
    test_recipient = config.get("email_test") or config.get("email_to")
    if not test_recipient:
        return False

    test_config = build_test_config(config)
    if not _has_complete_test_azure_config(config):
        missing = [
            field for field in (
                "azure_openai_endpoint",
                "azure_openai_key",
            )
            if not test_config.get(field)
        ]
        print(
            "Warning: effective Azure test config is incomplete; skipping test email. "
            f"Missing config: {', '.join(missing)}.",
            file=sys.stderr,
        )
        return False

    try:
        translated = translate_text(summary_markdown, test_config)
        if translated.startswith("[Translation failed:"):
            print("Warning: test-model translation failed, sending fallback Czech text.", file=sys.stderr)
        send_email(subject, translated, test_config, downloaded_files, to_addr=test_recipient)
        print("Test email sent.")
        return True
    except Exception as e:
        print(f"Warning: test email failed: {e}", file=sys.stderr)
        return False


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="Gather edookit updates")
    parser.add_argument("cookies_file", nargs="?", default="cookies.json")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print markdown to stdout, skip email and ledger update")
    parser.add_argument("--dry-run-html", action="store_true",
                        help="Print rendered HTML to stdout, skip email and ledger update")
    parser.add_argument(
        "--prompt-for-date",
        metavar="YYYY-MM-DD",
        help="Build the exact translation prompt for a UTC calendar day without calling the model",
    )
    args = parser.parse_args(argv)
    is_dry = args.dry_run or args.dry_run_html
    skip_delivery = is_dry or args.prompt_for_date is not None
    should_send_email = not skip_delivery
    progress_stream = sys.stderr if skip_delivery else sys.stdout
    config = load_config()
    config["cookies_file"] = args.cookies_file

    try:
        cookies = load_cookies(args.cookies_file)
    except FileNotFoundError:
        if config.get("plus4u_email") and config.get("plus4u_password"):
            print(
                f"Cookie file not found: {args.cookies_file}; bootstrapping a new session via Plus4U login.",
                file=progress_stream,
            )
            cookies = {}
        else:
            print(f"Error: Cookie file not found: {args.cookies_file}", file=sys.stderr)
            print(file=sys.stderr)
            print(COOKIE_REFRESH_INSTRUCTIONS, file=sys.stderr)
            sys.exit(1)

    stored_seen_items = cookies.get(SEEN_ITEMS_KEY)
    seen_items = stored_seen_items if isinstance(stored_seen_items, dict) else {}
    last_run = cookies.get("last_run")
    needs_bootstrap = not seen_items

    # Ensure session is alive before any authenticated fetch, including dry runs.
    try:
        keepalive(cookies, args.cookies_file, config)
        print("Session OK.", file=progress_stream)
    except AuthError as e:
        print(f"Error: {e}", file=sys.stderr)
        if should_send_email:
            _send_alert_email(
                "Edookit: authentication failed",
                f"Edookit could not authenticate.\n\n{e}",
                config,
            )
        sys.exit(1)

    # Fetch and parse inbox
    print("Fetching inbox...", file=progress_stream)
    try:
        inbox_html = fetch_page(BASE_URL + "/overview/updates", cookies, args.cookies_file)
        all_items = parse_inbox(inbox_html)
    except AuthError as e:
        print(f"Error: {e}", file=sys.stderr)
        if should_send_email:
            _send_alert_email(
                "Edookit: authentication failed",
                f"Edookit lost authentication during inbox fetch.\n\n{e}",
                config,
            )
        sys.exit(1)

    state_changed = False
    if needs_bootstrap:
        seen_items, bootstrap_boundary = bootstrap_seen_items(
            all_items,
            seen_items,
            last_run,
        )
        cookies[SEEN_ITEMS_KEY] = _prune_seen_items(seen_items)
        cookies.pop("last_run", None)
        state_changed = True
        if _stored_timestamp(last_run):
            print(
                f"Initialized seen-item ledger from last_run through "
                f"{bootstrap_boundary.isoformat(timespec='seconds')}.",
                file=progress_stream,
            )
        else:
            print(
                f"Initialized seen-item ledger with a {BOOTSTRAP_WINDOW} "
                f"bootstrap window ending {bootstrap_boundary.isoformat(timespec='seconds')}.",
                file=progress_stream,
            )
    else:
        cookies.pop("last_run", None)

    if args.prompt_for_date is not None:
        try:
            target_date = date.fromisoformat(args.prompt_for_date)
        except ValueError:
            print("Error: --prompt-for-date must be in YYYY-MM-DD format.", file=sys.stderr)
            sys.exit(1)
        new_items = filter_items_for_utc_date(all_items, target_date)
    else:
        new_items = filter_new_items(all_items, seen_items)

    max_updates = int(config.get("max_updates", 50))
    if len(new_items) > max_updates:
        print(
            f"Limiting {len(new_items)} new updates to {max_updates}.",
            file=progress_stream,
        )
        new_items = new_items[:max_updates]

    for item in new_items:
        print(
            f"Selected item: {item['type']} {_item_identity(item) or 'unknown'} "
            f"[{item['timestamp_raw'] or 'no timestamp'}] {item['title']}",
            file=progress_stream,
        )

    if not new_items:
        if args.prompt_for_date is not None:
            output = "No new updates.\n"
            prompts = build_translation_prompt(output, config)
            print(json.dumps({
                "utc_date": args.prompt_for_date,
                "summary_markdown": output,
                "system_prompt": prompts["system_prompt"],
                "user_prompt": prompts["user_prompt"],
            }, ensure_ascii=False, indent=2))
            sys.exit(0)

        if state_changed and not skip_delivery:
            save_cookies(cookies, args.cookies_file)
        print("No new updates since last run.", file=progress_stream)
        # Good time to check that the translation model is still available
        try:
            check_llm_config(config)
            print("Translation model OK.", file=progress_stream)
        except TranslationError as e:
            print(f"Warning: {e}", file=sys.stderr)
            if should_send_email:
                _send_alert_email(
                    "Edookit: translation model unavailable",
                    f"The LLM model check failed:\n\n{e}\n\n"
                    "Translation will not work until this is fixed. "
                    "Update the GEMINI_API_KEY or AZURE_OPENAI_DEPLOYMENT environment variables.",
                    config,
                )
        sys.exit(0)

    print(
        f"Found {len(new_items)} new item(s), fetching details...",
        file=progress_stream,
    )

    # Fetch "Requires Action" widget from the dashboard (only when there are
    # new items — action items are undated, so we include them as a reminder
    # alongside real updates but never trigger an email on their own)
    action_items = []
    try:
        dash_html = fetch_page(BASE_URL + "/", cookies, args.cookies_file)
        action_items = parse_action_items(dash_html)
        if action_items:
            print(
                f"Including {len(action_items)} action item(s) as reminder.",
                file=progress_stream,
            )
    except (AuthError, RuntimeError) as e:
        print(f"Warning: could not fetch action items: {e}", file=sys.stderr)

    # Fetch upcoming events calendar
    upcoming_events = []
    new_event_urls = set()
    try:
        print("Fetching upcoming events...", file=progress_stream)
        upcoming_events = fetch_upcoming_events(cookies, args.cookies_file, config)
        # Determine which upcoming events are also new in the inbox
        new_event_urls = {
            i["url"] for i in new_items if i["type"] == "event" and i["url"]
        }
        if upcoming_events:
            new_count = sum(1 for e in upcoming_events if e.get("url") in new_event_urls)
            print(
                f"Including {len(upcoming_events)} upcoming event(s) "
                f"({new_count} new).",
                file=progress_stream,
            )
    except (AuthError, RuntimeError) as e:
        print(f"Warning: could not fetch upcoming events: {e}", file=sys.stderr)

    # Group by type
    items_by_type = {}
    for item in new_items:
        items_by_type.setdefault(item["type"], []).append(item)

    # Fetch details for each item
    details_by_url = {}
    for i, item in enumerate(new_items, 1):
        if item["url"]:
            print(
                f"Fetching item {i}/{len(new_items)}...",
                file=progress_stream,
            )
            detail = fetch_item_detail(item, cookies, args.cookies_file)
            if detail:
                details_by_url[item["url"]] = detail

    # Download attachments only when email delivery is enabled; prompt-only mode
    # only needs the attachment names already present in the detail parse.
    downloaded_files = []
    with tempfile.TemporaryDirectory(prefix="edookit_") as tmp_dir:
        if should_send_email:
            for detail in details_by_url.values():
                for att in detail.get("attachments", []):
                    try:
                        print(
                            f"Downloading attachment: {att['name']}",
                            file=progress_stream,
                        )
                        filepath, _ = download_attachment(
                            att["download_url"], cookies, tmp_dir
                        )
                        downloaded_files.append(filepath)
                    except RuntimeError as e:
                        print(f"  Warning: {e}", file=sys.stderr)

        # Generate summary
        output = format_summary(
            items_by_type, details_by_url, action_items,
            upcoming_events=upcoming_events, new_event_urls=new_event_urls,
        )

        if args.prompt_for_date is not None:
            prompts = build_translation_prompt(output, config)
            print(json.dumps({
                "utc_date": args.prompt_for_date,
                "summary_markdown": output,
                "system_prompt": prompts["system_prompt"],
                "user_prompt": prompts["user_prompt"],
            }, ensure_ascii=False, indent=2))
            sys.exit(0)

        summary_markdown = output

        # Translate — fall back to Czech with error note if translation fails
        translation_failed = False
        target_lang = config.get("target_language", "English")
        print(f"Translating to {target_lang}...", file=progress_stream)
        translated = translate_text(summary_markdown, config)
        if translated.startswith("[Translation failed:"):
            print("Warning: translation failed, using Czech.", file=sys.stderr)
            translation_failed = True
        output = translated

        # Explicit preview modes print content; normal delivery logs metadata only.
        if args.dry_run_html:
            print(render_email_html(output))
        elif args.dry_run:
            print(output)

        # Send email unless dry-run
        email_failed = False
        if should_send_email and config.get("smtp_host"):
            subject = f"Edookit: {len(new_items)} new update(s)"
            try:
                print("Sending email...", file=progress_stream)
                send_email(subject, output, config, downloaded_files)
                print("Email sent.", file=progress_stream)
            except Exception as e:
                print(f"Error: email failed: {e}", file=sys.stderr)
                email_failed = True

            if not email_failed:
                seen_items = seen_items or {}
                sent_at = _now_in_prague().isoformat(timespec="seconds")
                for item in new_items:
                    identity = _item_identity(item)
                    if identity:
                        seen_items[identity] = sent_at
                cookies[SEEN_ITEMS_KEY] = _prune_seen_items(seen_items)
                save_cookies(cookies, args.cookies_file)
                for item in new_items:
                    print(
                        f"Delivered item: {item['type']} "
                        f"{_item_identity(item) or 'unknown'}",
                        file=progress_stream,
                    )
                state_changed = False

            if not email_failed and config.get("email_test"):
                print("Sending test email...", file=progress_stream)
                send_test_email(subject, summary_markdown, config, downloaded_files)
        # Temp dir and files are cleaned up here

    if not skip_delivery and not config.get("smtp_host"):
        seen_items = seen_items or {}
        sent_at = _now_in_prague().isoformat(timespec="seconds")
        for item in new_items:
            identity = _item_identity(item)
            if identity:
                seen_items[identity] = sent_at
        cookies[SEEN_ITEMS_KEY] = _prune_seen_items(seen_items)
        save_cookies(cookies, args.cookies_file)
        for item in new_items:
            print(
                f"Processed item without email: {item['type']} "
                f"{_item_identity(item) or 'unknown'}",
                file=progress_stream,
            )
        state_changed = False

    if state_changed and not skip_delivery:
        save_cookies(cookies, args.cookies_file)

    if translation_failed or email_failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
