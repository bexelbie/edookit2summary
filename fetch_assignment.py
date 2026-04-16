# ABOUTME: CLI tool to fetch and parse a single homework assignment from edookit.net.
# ABOUTME: Thin wrapper around edookit.py shared library.

import json
import sys

from edookit import (
    AuthError, ParseError, COOKIE_REFRESH_INSTRUCTIONS,
    load_cookies, fetch_page, parse_detail_page,
)


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
        fields = parse_detail_page(html)
    except (AuthError, ParseError) as e:
        print(f"Error: {e}", file=sys.stderr)
        print(file=sys.stderr)
        print(COOKIE_REFRESH_INSTRUCTIONS, file=sys.stderr)
        sys.exit(1)

    if not fields:
        print("Error: No data found on page.", file=sys.stderr)
        sys.exit(1)

    print(json.dumps(fields, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
