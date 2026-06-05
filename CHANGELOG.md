# Changelog

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
