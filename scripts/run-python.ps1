# Run the project's Python inside the contained environment, from the checkout
# root: the Windows form of `source scripts/project-env.sh && .venv/bin/python`.
#   scripts\run-python.ps1 scripts\doctor.py
#   scripts\run-python.ps1 -m pytest tests -q
. "$PSScriptRoot\project-env.ps1"
Set-Location -LiteralPath $WbProjectRoot
if (-not (Test-Path -LiteralPath $WbVenvPython)) { throw 'No .venv - run scripts\setup.ps1 first.' }
& $WbVenvPython @args
exit $LASTEXITCODE
