# Serve the app on native Windows from one port (HOST/PORT in .env).
#
#   scripts\start.ps1               start in the background, wait, open the browser
#   scripts\start.ps1 -Foreground   run in this window; Ctrl+C stops it
#   scripts\start.ps1 -NoBrowser    start in the background only
#   scripts\start.ps1 -Build        rebuild the frontend first (source checkouts)
#
# A second start while one is running is refused (scripts\serve.py records the
# server). A record left by a crash or a reboot is recognised as stale and
# replaced, and the app itself resumes the queued jobs on its way up.
param([switch]$Foreground, [switch]$NoBrowser, [switch]$Build, [int]$Port = 0)

. "$PSScriptRoot\project-env.ps1"
Set-Location -LiteralPath $WbProjectRoot

if (-not (Test-Path -LiteralPath $WbVenvPython)) {
    throw 'No .venv - run scripts\setup.ps1 (or wizards-brush.cmd setup) first.'
}
$releaseBundle = Test-Path -LiteralPath (Join-Path $WbProjectRoot 'RELEASE-MANIFEST.json')
$dist = Join-Path $WbProjectRoot 'frontend\dist\index.html'
if ($releaseBundle) {
    if (-not (Test-Path -LiteralPath $dist)) { throw 'Release bundle is missing its prebuilt frontend; download it again.' }
} elseif ($Build -or -not (Test-Path -LiteralPath $dist)) {
    Write-Host '==> Building frontend'
    Push-Location (Join-Path $WbProjectRoot 'frontend')
    try {
        $npm = Join-Path $WbNodeDir 'npm.cmd'
        if (-not (Test-Path -LiteralPath (Join-Path $WbProjectRoot 'frontend\node_modules'))) {
            & $npm ci; if ($LASTEXITCODE -ne 0) { throw 'npm ci failed' }
        }
        & $npm run build; if ($LASTEXITCODE -ne 0) { throw 'npm run build failed' }
    } finally { Pop-Location }
}

$serve = Join-Path $PSScriptRoot 'serve.py'
$serveArgs = @('start')
if ($Port -gt 0) { $serveArgs += @('--port', "$Port") }

if ($Foreground) {
    & $WbVenvPython $serve @serveArgs
    exit $LASTEXITCODE
}

& $WbVenvPython $serve status | Out-Null
if ($LASTEXITCODE -eq 0) {
    & $WbVenvPython $serve status
    throw 'The Wizard''s Brush is already running from this folder. Use scripts\stop.ps1 first.'
}

$logs = Join-Path $WbRuntimeDir 'logs'
New-Item -ItemType Directory -Force -Path $logs | Out-Null
$out = Join-Path $logs 'server.out.log'
$err = Join-Path $logs 'server.err.log'
# Start-Process joins arguments with spaces and does not quote them; the
# script path may contain spaces, so it is quoted here.
$argumentLine = @('"' + $serve + '"') + $serveArgs
$launched = Start-Process -FilePath $WbVenvPython -ArgumentList $argumentLine `
    -WorkingDirectory $WbProjectRoot -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput $out -RedirectStandardError $err

$record = Join-Path $WbStateDir 'server.json'
$deadline = (Get-Date).AddSeconds(120)
$url = $null
$candidate = $null
while ((Get-Date) -lt $deadline) {
    if ($launched.HasExited) {
        Write-Host (Get-Content -LiteralPath $out, $err -ErrorAction SilentlyContinue | Select-Object -Last 40 | Out-String)
        throw "The server exited during startup (exit $($launched.ExitCode)). Logs: $logs"
    }
    if (Test-Path -LiteralPath $record) {
        try {
            $info = Get-Content -LiteralPath $record -Raw | ConvertFrom-Json
            $hostName = if ($info.host -eq '0.0.0.0' -or $info.host -eq '::') { '127.0.0.1' } else { $info.host }
            $candidate = "http://${hostName}:$($info.port)"
            $null = Invoke-WebRequest -UseBasicParsing -Uri "$candidate/" -TimeoutSec 3
            $url = $candidate
            break
        } catch [System.Net.WebException] {
            # Any HTTP answer (401 with a token set, for example) means it is up.
            if ($_.Exception.Response -and $candidate) { $url = $candidate; break }
        } catch { }
    }
    Start-Sleep -Milliseconds 500
}
if (-not $url) { throw "The server did not answer within two minutes. Logs: $logs" }
Write-Host "==> Serving on $url (logs in $logs)"
if (-not $NoBrowser) { & "$PSScriptRoot\browser.ps1" -Url $url }
