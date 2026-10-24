# Open the app in a repository-owned browser profile so cookies, storage and
# caches stay inside the project, like scripts/browser.sh. Edge ships with
# Windows; Chrome is used when BROWSER_BIN names it. Falls back to the
# default browser (with its normal profile) only when neither is found.
param([string]$Url = '')

. "$PSScriptRoot\project-env.ps1"
if (-not $Url) {
    $Url = if ($env:WB_URL) { $env:WB_URL } else { 'http://localhost:8000' }
}

$candidates = @()
if ($env:BROWSER_BIN) { $candidates += $env:BROWSER_BIN }
foreach ($base in @(${env:ProgramFiles(x86)}, $env:ProgramFiles)) {
    if (-not $base) { continue }
    $candidates += (Join-Path $base 'Microsoft\Edge\Application\msedge.exe')
    $candidates += (Join-Path $base 'Google\Chrome\Application\chrome.exe')
}
$browser = $candidates | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1

if (-not $browser) {
    Write-Warning 'No Edge or Chrome found; opening the default browser with its own profile.'
    Start-Process $Url
    exit 0
}
$profileDir = Join-Path $WbRuntimeDir 'browser'
$cacheDir = Join-Path $WbCacheDir 'browser'
Assert-WbInside $profileDir
Assert-WbInside $cacheDir
New-Item -ItemType Directory -Force -Path $profileDir, $cacheDir | Out-Null
Start-Process -FilePath $browser -ArgumentList @(
    ('"--user-data-dir=' + $profileDir + '"'), ('"--disk-cache-dir=' + $cacheDir + '"'),
    '--no-first-run', '--no-default-browser-check', $Url)
