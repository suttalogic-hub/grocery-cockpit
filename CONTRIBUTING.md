# Contributing

Thanks for helping improve Grocery Cockpit.

This project is intentionally local-first and privacy-aware. Please keep changes scoped, testable, and careful about personal data.

## Good First Contributions

- Improve pack-size parsing and unit-price normalization.
- Add tests for product matching and false-positive rejection.
- Improve demo data and screenshots.
- Improve mobile dashboard accessibility.
- Refactor provider adapters away from core pricing logic.

## Privacy Rules

Do not commit:

- `config.json`
- `data/`
- browser profiles
- cookies, sessions, local storage, or order history exports
- screenshots containing personal addresses, names, phone numbers, or order details
- provider logs that include private URLs or account state

Use `config.example.json` and seeded demo data for examples.

## Development

```powershell
npm install
Copy-Item config.example.json config.json
py -3.13 grocery_cockpit.py seed
py -3.13 grocery_cockpit.py serve --host 127.0.0.1
```

Run checks before opening a pull request:

```powershell
py -3.13 -m unittest discover -s tests
py -3.13 -m py_compile grocery_cockpit.py provider_adapters.py auto_scan_worker.py basket_scan_worker.py
node tests/browser_provider_adapters.test.mjs
node --check browser_scan_worker.mjs
```

Regenerate public demo images after visible dashboard changes:

```powershell
npm run screenshots
```

Review [docs/DEMO_SCREENSHOTS.md](docs/DEMO_SCREENSHOTS.md) before committing generated images.

## Provider Adapters

Provider adapters should be optional and isolated. Prefer official APIs when available. Browser-session probes should be documented as personal/local workflows and should never require committing credentials or session files.

Read [docs/PROVIDER_ADAPTERS.md](docs/PROVIDER_ADAPTERS.md) before changing provider behavior. Keep the Python and browser adapter registries aligned, and update their contract tests with every provider-policy change.

## Pull Request Style

- Keep PRs focused.
- Explain user-visible behavior changes.
- Add tests for matching, pricing, or basket logic changes.
- Keep decision-engine test data synthetic and cover the user-facing recommendation or alert outcome.
- Mention any provider-specific assumptions.
