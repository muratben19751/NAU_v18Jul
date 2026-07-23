param(
    [string]$GistId = "cc5c8604a8762d9ccbb8ae932cd274c7"
)

$ErrorActionPreference = "Stop"

$target = Join-Path $env:TEMP "wiki-sync-gist"
$skillRoot = Join-Path $HOME ".claude/skills/wiki-sync"
$assetsRoot = Join-Path $skillRoot "assets"

Write-Host "[1/5] Preparing folders..."
New-Item -ItemType Directory -Force -Path $skillRoot | Out-Null
New-Item -ItemType Directory -Force -Path $assetsRoot | Out-Null

if (Test-Path $target) {
    Write-Host "[2/5] Removing existing temp clone..."
    Remove-Item $target -Recurse -Force
}

Write-Host "[3/5] Cloning gist to temp..."
Set-Location $env:TEMP
gh gist clone $GistId $target

Write-Host "[4/5] Copying files..."
Copy-Item (Join-Path $target "SKILL.md") $skillRoot -Force
Copy-Item (Join-Path $target "SCHEMA_TEMPLATE.md") $assetsRoot -Force
Copy-Item (Join-Path $target "bootstrap_wiki.py") $assetsRoot -Force
Copy-Item (Join-Path $target "wiki_tools.py") $assetsRoot -Force

Write-Host "[5/5] Cleaning up temp folder..."
Remove-Item $target -Recurse -Force

$checks = @(
    (Join-Path $skillRoot "SKILL.md"),
    (Join-Path $assetsRoot "SCHEMA_TEMPLATE.md"),
    (Join-Path $assetsRoot "bootstrap_wiki.py"),
    (Join-Path $assetsRoot "wiki_tools.py")
)

$missing = $checks | Where-Object { -not (Test-Path $_) }
if ($missing.Count -gt 0) {
    Write-Error "Install completed with missing files: $($missing -join ', ')"
}

Write-Host "Done. wiki-sync skill files installed successfully."
