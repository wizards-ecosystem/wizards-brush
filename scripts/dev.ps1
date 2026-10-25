# Dev mode on native Windows: FastAPI with reload on :8000 and the Vite dev
# server on :5173 (it proxies /api). Ctrl+C stops both.
#
# Only the two processes started here are stopped, each with its own child
# tree: the backend through scripts\serve.py (whose job object owns the reload
# worker), Vite through taskkill /T on the one recorded process id.

. "$PSScriptRoot\project-env.ps1"
Set-Location -LiteralPath $WbProjectRoot
if (-not (Test-Path -LiteralPath $WbVenvPython)) { throw 'No .venv - run scripts\setup.ps1 first.' }

$serve = Join-Path $PSScriptRoot 'serve.py'
$npm = Join-Path $WbNodeDir 'npm.cmd'
$backend = $null
$frontend = $null
try {
    Write-Host '==> Backend  http://127.0.0.1:8000  (API + /docs)'
    $backend = Start-Process -FilePath $WbVenvPython -NoNewWindow -PassThru `
        -WorkingDirectory $WbProjectRoot -ArgumentList @(('"' + $serve + '"'), 'start', '--reload', '--port', '8000')
    Write-Host '==> Frontend http://127.0.0.1:5173'
    $frontend = Start-Process -FilePath $npm -NoNewWindow -PassThru `
        -WorkingDirectory (Join-Path $WbProjectRoot 'frontend') -ArgumentList @('run', 'dev')
    while (-not $backend.HasExited -and -not $frontend.HasExited) { Start-Sleep -Milliseconds 500 }
} finally {
    if ($backend -and -not $backend.HasExited) {
        & $WbVenvPython $serve stop --timeout 10 | Out-Null
    }
    if ($frontend -and -not $frontend.HasExited) {
        & taskkill.exe /T /F /PID $frontend.Id | Out-Null
    }
}
