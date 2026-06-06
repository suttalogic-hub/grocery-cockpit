# Roadmap

## v0.1 Public Baseline

- Publish a public-safe repository with private data ignored.
- Keep demo mode working from `config.example.json` and `grocery_cockpit.py seed`.
- Add tests for pack parsing, unit pricing, match modes, and false-positive rejection.
- Document privacy boundaries and provider-adapter limitations.
- Add watchlist import/export without price history.
- Add fixture-driven synthetic bad-match coverage for exact, category, same-size, and unit-price modes.

## v0.2 Core Quality

- Move provider-specific logic behind clearer adapter interfaces. Completed in `0.15.0`.
- Add more basket optimization and alerting tests. Completed in `0.15.1`.
- Add screenshot generation from demo data for README assets.
- Add CI for Python syntax, unit tests, and Node syntax checks.

## v0.3 Self-Hosted Personal Use

- Harden Docker and VM deployment paths.
- Add a documented backup/restore flow.
- Add health checks for background scans.
- Improve mobile PWA install and offline behavior.

## Longer Term

- Support community-maintained adapters where legal and appropriate.
- Prefer official APIs where providers expose them.
- Build a safer plugin system for private provider integrations.
- Add richer basket constraints, substitutions, and household budgeting views.
