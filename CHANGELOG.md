# Changelog

## 0.15.4 - Amazon Now Handoff

- Routed every Amazon action through the Amazon Now app handoff instead of Fresh web routes.
- Moved the menu button to the far left of the header.

## 0.15.3 - Item Controls

- Added compact individual dismiss controls to deal alerts.
- Added item editing and safe item deletion from every saved item card.

## 0.15.2 - Automated Demo Screenshots

- Added a one-command public screenshot generator using isolated synthetic data.
- Added desktop, mobile, and basket-recommendation README images.
- Added PNG dimension and blank-image checks to the generator.
- Added screenshot generation to CI and documented its privacy boundary.

## 0.15.1 - Decision Engine Contracts

- Added synthetic basket-optimization tests for one-app convenience, worthwhile splits, incomplete coverage, quantities, unit-price ranking, and suspicious-price rejection.
- Added alerting tests for 10-day and 30-day thresholds, required history, delivery and handling fees, deduplication, suspicious-price rejection, and expiry.
- Kept the decision suite browser-free and free of personal grocery data.
- Increased the Python test suite from 15 to 29 tests.

## 0.15.0 - Provider Adapter Boundary

- Moved provider identity, search routing, open-link policy, readiness, and scan timeouts into a documented Python adapter registry.
- Added a browser adapter registry for setup URLs, extractor selection, price parsing flags, and category-scan behavior.
- Kept compatibility wrappers so existing dashboard and worker behavior remains stable.
- Added browser-free contract tests covering all seven supported providers.
- Documented the provider adapter surface and contribution expectations.

## 0.14.9 - Bad-Match Fixture Suite

- Added a synthetic bad-match fixture suite covering exact, same-category, same-size, and unit-price matching modes.
- Added fixture explanations so contributors can understand why each candidate should match or be rejected.
- Covered grocery examples for detergent variants, Coke Zero, paneer, curd/ghee, curry-cut chicken, potato unit price, sesame seeds, and skincare brand identity.
- Kept fixture data synthetic and free of personal order history.

## 0.14.8 - Watchlist Import/Export

- Added public-safe watchlist export without price history, alerts, baskets, provider sessions, location, or access keys.
- Added watchlist import with merge-by-default and explicit replace mode.
- Added dashboard controls and CLI commands for watchlist backup and restore.
- Added tests for watchlist privacy boundaries and duplicate-safe import behavior.
- Aligned the npm package license with the repository's MIT license.

## 0.14.7 - OSS Prep

- Added public-safe project documentation.
- Added MIT license, contribution guide, security notes, and roadmap.
- Added a first unit test suite for matching, pack parsing, unit prices, and demo seed behavior.
- Made the example config generic and kept private runtime state ignored.
- Improved demo seed data so a clean install shows sample prices and alerts.
- Rebuilt the public-safe release bundle without personal data.

## 0.14.6 - Amazon Now Link Fix

- Routed Amazon Now rows to direct product pages when a valid product URL is available.
- Fell back to Amazon Now search when no trusted product URL is available.
- Tightened bad-match behavior for size-sensitive products such as Coke Zero 750ml.
