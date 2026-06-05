param(
    [switch]$IncludePersonalData,
    [string]$OutputDir = "dist"
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Version = (& py -3.13 -c "import grocery_cockpit as g; print(g.APP_VERSION)" 2>$null).Trim()
if (-not $Version) { $Version = "dev" }

$Stage = Join-Path $env:TEMP "grocery-cockpit-free-vm-$Version"
$Output = Join-Path $Root $OutputDir
$ZipPath = Join-Path $Output "grocery-cockpit-free-vm-$Version.zip"

Remove-Item -LiteralPath $Stage -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $Stage, $Output | Out-Null

$files = @(
    ".dockerignore",
    ".gitignore",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "Dockerfile",
    "LICENSE",
    "README.md",
    "ROADMAP.md",
    "SECURITY.md",
    "auto_scan_worker.py",
    "basket_scan_worker.py",
    "browser_scan_worker.mjs",
    "config.example.json",
    "grocery_cockpit.py",
    "package-lock.json",
    "package.json",
    "prepare_free_vm_bundle.ps1"
)

foreach ($file in $files) {
    Copy-Item -LiteralPath (Join-Path $Root $file) -Destination (Join-Path $Stage $file) -Force
}

foreach ($dir in @("static", "deploy")) {
    Copy-Item -LiteralPath (Join-Path $Root $dir) -Destination (Join-Path $Stage $dir) -Recurse -Force
}

foreach ($dir in @("docs", "tests")) {
    $sourceDir = Join-Path $Root $dir
    if (Test-Path $sourceDir) {
        Copy-Item -LiteralPath $sourceDir -Destination (Join-Path $Stage $dir) -Recurse -Force
    }
}

$githubDir = Join-Path $Root ".github"
if (Test-Path $githubDir) {
    Copy-Item -LiteralPath $githubDir -Destination (Join-Path $Stage ".github") -Recurse -Force
}

Get-ChildItem -LiteralPath $Stage -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force
Get-ChildItem -LiteralPath $Stage -Recurse -File -ErrorAction SilentlyContinue |
    Where-Object { $_.Extension -in @(".pyc", ".pyo") } |
    Remove-Item -Force

if ($IncludePersonalData) {
    Copy-Item -LiteralPath (Join-Path $Root "config.json") -Destination (Join-Path $Stage "config.json") -Force
    $DataStage = Join-Path $Stage "data"
    New-Item -ItemType Directory -Force -Path $DataStage | Out-Null
    foreach ($name in @("grocery.sqlite", "auto_scan_status.json", "basket_scan_status.json")) {
        $source = Join-Path (Join-Path $Root "data") $name
        if (Test-Path $source) {
            Copy-Item -LiteralPath $source -Destination (Join-Path $DataStage $name) -Force
        }
    }
    Get-ChildItem -Path (Join-Path $Root "data") -Filter "*_probe_results.json" -ErrorAction SilentlyContinue |
        ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $DataStage $_.Name) -Force }
} else {
    Copy-Item -LiteralPath (Join-Path $Root "config.example.json") -Destination (Join-Path $Stage "config.json") -Force
}

Remove-Item -LiteralPath $ZipPath -Force -ErrorAction SilentlyContinue
Compress-Archive -Path (Join-Path $Stage "*") -DestinationPath $ZipPath -Force
Write-Output $ZipPath
if ($IncludePersonalData) {
    Write-Output "Personal config and grocery database included. Keep this zip private."
} else {
    Write-Output "No personal database included. Use -IncludePersonalData for migration."
}
