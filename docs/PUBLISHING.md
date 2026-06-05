# Publishing Checklist

Use this when preparing the public GitHub repository.

## Local Checks

```powershell
py -3.13 -m unittest discover -s tests
py -3.13 -m py_compile grocery_cockpit.py auto_scan_worker.py basket_scan_worker.py
node --check browser_scan_worker.mjs
.\prepare_free_vm_bundle.ps1
```

## Privacy Checks

```powershell
rg -n "YOUR_PRIVATE_KEY|YOUR_PRIVATE_ADDRESS|YOUR_PRIVATE_PINCODE" . --glob "!data/**" --glob "!dist/**" --glob "!config.json" --glob "!node_modules/**"
```

Replace those placeholders with your own private strings before running the check.
The command should return no personal hits.

Also verify the release zip:

```powershell
Expand-Archive dist\grocery-cockpit-free-vm-*.zip -DestinationPath $env:TEMP\grocery-cockpit-check -Force
rg -n "YOUR_PRIVATE_KEY|YOUR_PRIVATE_ADDRESS|YOUR_PRIVATE_PINCODE" $env:TEMP\grocery-cockpit-check
```

## First Public Release

1. Create a clean repository from the public-safe staging folder.
2. Push to GitHub as `grocery-cockpit`.
3. Confirm GitHub Actions passes.
4. Add topics: `grocery`, `price-tracker`, `local-first`, `pwa`, `python`, `playwright`, `india`.
5. Create release `v0.1.0`.
6. Upload the public-safe zip.
7. Open a few starter issues:
   - Extract provider adapters from core logic.
   - Add more synthetic bad-match tests.
   - Improve demo screenshots.
   - Add watchlist import/export.

## Applying For OSS Support

Do not apply immediately after creating the repository. First build some public maintenance signal:

- at least one tagged release
- passing CI
- real README screenshots
- a few issues/PRs
- clear evidence that the project is actively maintained
