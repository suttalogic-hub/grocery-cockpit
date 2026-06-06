# Demo Screenshots

The public screenshots are generated from a temporary, synthetic Grocery Cockpit installation. The workflow never reads `config.json`, `data/`, browser profiles, cookies, order history, or personal price history.

## Generate

Install dependencies, then run:

```powershell
npm ci
npm run screenshots
```

The command:

1. Creates a temporary config with private access disabled.
2. Seeds the built-in synthetic grocery history.
3. Adds a synthetic three-item basket.
4. Starts the dashboard on a free localhost port.
5. Captures desktop, mobile, and basket-decision views.
6. Verifies every PNG has the expected dimensions and is not suspiciously small.
7. Removes the temporary database and config.

Generated files:

```text
docs/assets/demo-desktop.png
docs/assets/demo-mobile.png
docs/assets/demo-basket.png
```

Set `GROCERY_CHROME_PATH` when Chrome, Edge, or Chromium is installed in a non-standard location. Set `GROCERY_PYTHON` when the Python executable is not discoverable as `py -3.13`, `python3.13`, `python3`, or `python`.

Review all three images before committing them. The generator proves the files are valid, while visual review catches layout or content regressions.
