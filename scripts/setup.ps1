# Native Windows setup: pinned tools, Python, dependencies, frontend, models.
# Re-runnable; writes only inside this checkout; needs no administrator rights.
#
#   powershell -ExecutionPolicy Bypass -File scripts\setup.ps1 [-Deps full|test] [-SkipModels]
#
#   full  everything setup.sh installs (the local CUDA stack and optional
#         processors). Local GPU inference on Windows is not qualified yet;
#         see docs/getting-started.md.
#   test  the dependency group CI uses: the app, its API and the test suite,
#         without torch. Enough to serve the UI, use a Remote GPU and run tests.
param(
    [ValidateSet('full', 'test')][string]$Deps = 'full',
    [switch]$SkipModels,
    [switch]$SkipFrontend
)

. "$PSScriptRoot\project-env.ps1"
Set-Location -LiteralPath $WbProjectRoot
$versions = Read-WbToolVersions

function Invoke-Checked([string]$What, [scriptblock]$Command) {
    & $Command
    if ($LASTEXITCODE -ne 0) { throw "$What failed (exit $LASTEXITCODE)" }
}

$releaseBundle = Test-Path -LiteralPath (Join-Path $WbProjectRoot 'RELEASE-MANIFEST.json')
if ($WbProjectRoot.Length -gt 70) {
    Write-Warning ("The checkout path is $($WbProjectRoot.Length) characters. Model caches nest deeply; " +
                   'a shorter path such as C:\wb avoids the 260-character limit. scripts\doctor.py checks it.')
}

$envFile = Join-Path $WbProjectRoot '.env'
if (-not (Test-Path -LiteralPath $envFile)) {
    Copy-Item -LiteralPath (Join-Path $WbProjectRoot '.env.example') -Destination $envFile
    Write-Host '==> Created .env - review model and network settings.'
}
# Owner-only access, the Windows counterpart of chmod 600.
& icacls $envFile /inheritance:r /grant:r "$($env:USERDOMAIN)\$($env:USERNAME):(F)" | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Warning 'Could not restrict .env to this account (a volume without ACLs?).' }

$withNode = (-not $releaseBundle) -and (-not $SkipFrontend)
if ($withNode) { & "$PSScriptRoot\bootstrap-tools.ps1" } else { & "$PSScriptRoot\bootstrap-tools.ps1" -UvOnly }

$python = $versions['PYTHON_VERSION']
Write-Host "==> Python $python inside .runtime\python"
Invoke-Checked 'uv python install' { & $WbUvExe python install $python }
$projectPython = (& $WbUvExe python find $python --managed-python).Trim()
Assert-WbInside $projectPython

if (-not (Test-Path -LiteralPath (Join-Path $WbProjectRoot '.venv'))) {
    Invoke-Checked 'uv venv' { & $WbUvExe venv --relocatable --python $projectPython .venv }
}

Write-Host "==> Locked backend dependencies ($Deps)"
if ($Deps -eq 'test') {
    Invoke-Checked 'uv sync' { & $WbUvExe sync --frozen --only-group test --python $projectPython }
} elseif ($releaseBundle) {
    Invoke-Checked 'uv sync' {
        & $WbUvExe sync --frozen --extra faces --extra detailer --extra rife --extra control `
            --no-dev --python $projectPython
    }
} else {
    Invoke-Checked 'uv sync' { & $WbUvExe sync --frozen --all-extras --group test --python $projectPython }
}
Invoke-Checked 'migrate_local_state' { & $WbVenvPython (Join-Path $PSScriptRoot 'migrate_local_state.py') }

if ($withNode) {
    Write-Host '==> Frontend deps + build'
    Push-Location (Join-Path $WbProjectRoot 'frontend')
    try {
        $npm = Join-Path $WbNodeDir 'npm.cmd'
        Invoke-Checked 'npm ci' { & $npm ci }
        Invoke-Checked 'npm run build' { & $npm run build }
    } finally { Pop-Location }
}

if ($Deps -eq 'full' -and -not $SkipModels) {
    Write-Host '==> Downloading enabled local models (one-time, large)'
    Invoke-Checked 'download_models' { & $WbVenvPython (Join-Path $PSScriptRoot 'download_models.py') }
}

Write-Host '==> Verifying the containment boundary'
Invoke-Checked 'doctor' { & $WbVenvPython (Join-Path $PSScriptRoot 'doctor.py') }

Write-Host ''
Write-Host 'Setup complete. Next steps:'
Write-Host '  wizards-brush.cmd start      # serve on HOST/PORT from .env and open the browser'
Write-Host '  wizards-brush.cmd stop       # graceful stop'
Write-Host '  wizards-brush.cmd doctor     # verify that no writable path escapes'
