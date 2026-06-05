# Changelog

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

