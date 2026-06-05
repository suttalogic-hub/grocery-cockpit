# OSS Readiness Checklist

This checklist is for preparing Grocery Cockpit for a public repository and maintainer program applications.

## Publish Blockers

- [ ] Remove or ignore all private runtime files.
- [ ] Verify `config.json` is not committed.
- [ ] Verify `data/` is not committed.
- [ ] Verify browser profiles and cookies are not committed.
- [ ] Replace private screenshots with demo-data screenshots.
- [ ] Run the full test/check command set.

## Project Signals To Build

- [ ] Public README with clear problem statement.
- [ ] Demo data that works for new users.
- [ ] Issues labeled for good first contributions.
- [ ] Initial release tag, for example `v0.1.0`.
- [ ] Changelog or release notes.
- [ ] At least a small test suite around core logic.
- [ ] Privacy and security documentation.

## OpenAI Codex for OSS Application Notes

OpenAI reviews active open-source projects for usage, ecosystem importance, and evidence of maintenance. For this project, the strongest story is:

- Local-first grocery price intelligence for Indian consumers.
- Reusable matching, unit-price, and false-positive rejection logic.
- Privacy-aware browser-session workflow for personal use.
- Maintainer work around issue triage, release management, tests, docs, and security posture.

Do not apply until the repository is public-safe, has an initial release, and has a short maintenance history.
