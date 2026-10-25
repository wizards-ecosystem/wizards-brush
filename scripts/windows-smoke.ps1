# Native Windows smoke for a checkout: torch-free setup, the backend suite,
# start, an API answer, a graceful stop, and no process left behind.
#
#   powershell -ExecutionPolicy Bypass -File scripts\windows-smoke.ps1 [-Port 8765] [-SkipTests]
#
# Run it in a disposable copy (for example a folder whose path has a space and
# a non-ASCII character). It installs only the test dependency group: no CUDA
# stack and no model weights, so it proves the platform plumbing, not local
# inference. Exits non-zero on the first failure.
param([int]$Port = 8765, [switch]$SkipTests)

. "$PSScriptRoot\project-env.ps1"
Set-Location -LiteralPath $WbProjectRoot
$report = [ordered]@{}

function Step([string]$Name, [scriptblock]$Body) {
    Write-Host "==> $Name"
    & $Body
    if ($LASTEXITCODE -ne 0) { throw "$Name failed (exit $LASTEXITCODE)" }
    $report[$Name] = 'ok'
}

function Get-OwnProcesses {
    # Processes whose executable lives inside this checkout: the venv launcher,
    # the managed interpreter, node. Nothing else is ever matched.
    Get-CimInstance Win32_Process | Where-Object {
        $_.ExecutablePath -and $_.ExecutablePath.StartsWith($WbProjectRoot + '\', [System.StringComparison]::OrdinalIgnoreCase)
    }
}

$report['os'] = [Environment]::OSVersion.VersionString
$report['checkout'] = $WbProjectRoot
Step 'setup (test dependencies)' { & "$PSScriptRoot\setup.ps1" -Deps test -SkipModels }
if (-not $SkipTests) {
    Step 'backend test suite' { & $WbVenvPython -m pytest tests -q -p no:cacheprovider }
}
$python = & $WbVenvPython -c "from backend.app.utils.hostmem import probe; print(probe().as_dict())"
$report['host_ram'] = "$python"

Step 'start' { & "$PSScriptRoot\start.ps1" -NoBrowser -Port $Port; $global:LASTEXITCODE = 0 }
$system = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/system" -TimeoutSec 10
$report['api/system version'] = "$($system.version)"
$backends = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/backends" -TimeoutSec 10
$report['api/backends host_ram'] = ($backends.hardware.host_ram | ConvertTo-Json -Compress)
$running = @(Get-OwnProcesses)
$report['processes while running'] = ($running | ForEach-Object { "$($_.ProcessId) $($_.Name)" }) -join ', '
Step 'second start refused' {
    & $WbVenvPython (Join-Path $PSScriptRoot 'serve.py') start --port ($Port + 1)
    if ($LASTEXITCODE -eq 3) { $global:LASTEXITCODE = 0 } else { $global:LASTEXITCODE = 1 }
}
Step 'stop' { & "$PSScriptRoot\stop.ps1" }
Start-Sleep -Seconds 2
$left = @(Get-OwnProcesses)
$report['processes after stop'] = ($left | ForEach-Object { "$($_.ProcessId) $($_.Name)" }) -join ', '
if ($left.Count -gt 0) { throw "processes from this checkout are still running: $($report['processes after stop'])" }
Step 'doctor' { & $WbVenvPython (Join-Path $PSScriptRoot 'doctor.py') }

Write-Host ''
Write-Host 'Windows smoke passed:'
$report.GetEnumerator() | ForEach-Object { Write-Host ("  {0}: {1}" -f $_.Key, $_.Value) }
