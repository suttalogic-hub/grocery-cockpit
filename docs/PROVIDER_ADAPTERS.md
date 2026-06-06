# Provider Adapters

Grocery Cockpit separates provider-specific behavior from pricing, matching, alerts, and basket logic.

This boundary keeps the core testable without launching browser sessions. It also gives contributors one place to inspect before changing how a provider is opened, searched, or scanned.

## Adapter Surfaces

`provider_adapters.py` owns policies used by Python services:

- provider identity, display name, type, and readiness
- search URL construction
- product-link versus search-link behavior
- rotating and focused scan timeout minimums
- provider readiness modes

`browser_provider_adapters.mjs` owns policies used by the Playwright worker:

- setup home URL
- extraction implementation selection
- provider-specific price parsing flags
- category-scan behavior
- probe-result matching mode

Extraction implementations remain in `browser_scan_worker.mjs`. Moving one behind the adapter registry does not imply that every provider page has the same HTML or behavior.

Unknown provider IDs retain a generic fallback for compatibility with local experiments. A provider must be added to both registries before it is considered supported.

## Contract

Every supported provider must:

1. Have the same provider ID in both adapter registries.
2. Expose a public name, kind, status, and search URL template in Python.
3. Expose a browser setup home URL and extractor selection in JavaScript.
4. Keep credentials, cookies, addresses, and browser profiles outside source control.
5. Add or update adapter contract tests when provider behavior changes.

The current provider IDs are:

```text
zepto
blinkit
swiggy_instamart
amazon_fresh
jiomart
dmart
bigbasket
```

`amazon_fresh` is the stable internal ID for the user-facing Amazon Now provider.

## Testing

Adapter policy tests do not launch a browser:

```powershell
py -3.13 -m unittest discover -s tests
node .\tests\browser_provider_adapters.test.mjs
```

Use a real provider probe only after these tests pass and only with your own account and delivery location.
