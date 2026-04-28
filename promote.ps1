# promote.ps1 — Push current main (QA) to the production branch on GitHub.
# Run this when you're happy with QA and want to release to production.
#
# Usage: powershell -ExecutionPolicy Bypass -File promote.ps1

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "=== Farm Stall POS — Promote QA → Production ===" -ForegroundColor Cyan
Write-Host ""

# Make sure we're on main
$branch = git rev-parse --abbrev-ref HEAD
if ($branch -ne "main") {
    Write-Host "ERROR: You must be on the 'main' branch to promote. Currently on: $branch" -ForegroundColor Red
    exit 1
}

# Make sure main is clean
$dirty = git status --porcelain
if ($dirty) {
    Write-Host "ERROR: Uncommitted changes detected. Commit or stash them first." -ForegroundColor Red
    git status --short
    exit 1
}

# Pull latest main so we're up to date
Write-Host "Pulling latest main from origin..." -ForegroundColor Yellow
git pull origin main

# Get the commit we're about to promote
$commitHash = git rev-parse --short HEAD
$commitMsg  = git log -1 --pretty=format:"%s"
Write-Host ""
Write-Host "Promoting commit: $commitHash  ""$commitMsg""" -ForegroundColor White
Write-Host ""

# Confirm
$confirm = Read-Host "Promote this to PRODUCTION? (yes/no)"
if ($confirm -ne "yes") {
    Write-Host "Aborted." -ForegroundColor Yellow
    exit 0
}

# Switch to production branch (create if first time)
$prodExists = git branch --list production
if (-not $prodExists) {
    Write-Host "Creating 'production' branch for the first time..." -ForegroundColor Yellow
    git checkout -b production
} else {
    git checkout production
}

# Merge main into production (fast-forward if possible, else real merge)
Write-Host "Merging main into production..." -ForegroundColor Yellow
git merge main --no-edit

# Push both branches
Write-Host "Pushing production to origin..." -ForegroundColor Yellow
git push origin production

Write-Host "Pushing main to origin..." -ForegroundColor Yellow
git checkout main
git push origin main

Write-Host ""
Write-Host "Done! production branch is now at commit $commitHash." -ForegroundColor Green
Write-Host "GitHub: https://github.com/quintuszgeyser/FarmStallPos/tree/production" -ForegroundColor Cyan
Write-Host ""
