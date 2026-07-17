# Project Context

## Overview

- `gather_updates.py` fetches edookit updates, formats a markdown summary, translates it, and emails the result.
- `edookit.py` holds shared fetching, auth, translation, and email helpers.
- Tests live under `tests/`.

## Verified Commands

- Run tests: `python3 -m unittest discover -s tests -v`

## Release Flow

- GitHub Releases are created from the tagged release commit message by `.github/workflows/release.yml`.
- The release tag must point at a commit on `main`.
- Before creating or pushing a release tag, the agent must show bex the exact release commit message and get approval.
- Do not improvise release notes in a shell command with escaped `\n` sequences. Use a method that preserves literal newlines in the commit message.
- If the release message or version bump is unclear, stop and ask bex before tagging or pushing.

