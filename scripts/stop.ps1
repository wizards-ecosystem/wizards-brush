# Stop the server this checkout started, gracefully.
#
# scripts\serve.py asks the server to shut down through the app's lifespan (so
# a rented GPU is released and the database lock let go) and, only if it has
# not exited after -Timeout seconds, ends that one recorded process after
# checking it is still this checkout's interpreter. No process is stopped by
# name, and nothing this checkout did not start is touched.
param([int]$Timeout = 30)

. "$PSScriptRoot\project-env.ps1"
Set-Location -LiteralPath $WbProjectRoot
if (-not (Test-Path -LiteralPath $WbVenvPython)) { Write-Host 'not running (no .venv)'; exit 0 }
& $WbVenvPython (Join-Path $PSScriptRoot 'serve.py') stop --timeout $Timeout
exit $LASTEXITCODE
