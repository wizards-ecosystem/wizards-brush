# Install the pinned uv and Node inside .runtime\tools on native Windows.
# The counterpart of bootstrap-tools.sh: official zips, SHA-256 checked against
# scripts\tool-versions.env before anything is unpacked, no administrator
# rights, nothing added to the user's PATH. Release bundles pass -UvOnly.
param([switch]$UvOnly)

. "$PSScriptRoot\project-env.ps1"
$ProgressPreference = 'SilentlyContinue'   # 5.1's progress bar slows downloads tenfold
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$versions = Read-WbToolVersions
$arch = Get-WbArch

function Get-Verified([string]$Url, [string]$Expected, [string]$Destination) {
    Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $Destination
    $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $Destination).Hash.ToLowerInvariant()
    if ($actual -ne $Expected.ToLowerInvariant()) {
        Remove-Item -LiteralPath $Destination -Force
        throw "Checksum mismatch for $Url`n  expected $Expected`n  got      $actual"
    }
}

function New-Stage([string]$Name) {
    $stage = Join-Path $WbTmpDir ("$Name-" + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $stage | Out-Null
    return $stage
}

$uvVersion = $versions['UV_VERSION']
$uvOk = (Test-Path -LiteralPath $WbUvExe) -and ((& $WbUvExe --version 2>$null) -like "uv $uvVersion *")
if (-not $uvOk) {
    Write-Host "==> Installing uv $uvVersion inside the project"
    $stage = New-Stage 'uv-bootstrap'
    try {
        $target = "$($arch.Uv)-pc-windows-msvc"
        $zip = Join-Path $stage "uv-$target.zip"
        Get-Verified "https://github.com/astral-sh/uv/releases/download/$uvVersion/uv-$target.zip" `
            $versions["UV_SHA256_WINDOWS_$($arch.UvHash)"] $zip
        Expand-Archive -LiteralPath $zip -DestinationPath (Join-Path $stage 'out')
        $uvDir = Join-Path $WbToolsDir 'uv'
        New-Item -ItemType Directory -Force -Path $uvDir | Out-Null
        foreach ($exe in @('uv.exe', 'uvx.exe', 'uvw.exe')) {
            $from = Join-Path $stage "out\$exe"
            if (Test-Path -LiteralPath $from) { Copy-Item -LiteralPath $from -Destination $uvDir -Force }
        }
    } finally {
        Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
    }
}

if (-not $UvOnly) {
    $nodeVersion = $versions['NODE_VERSION']
    $nodeExe = Join-Path $WbNodeDir 'node.exe'
    $nodeOk = (Test-Path -LiteralPath $nodeExe) -and ((& $nodeExe --version) -eq "v$nodeVersion")
    if (-not $nodeOk) {
        Write-Host "==> Installing Node $nodeVersion inside the project"
        $stage = New-Stage 'node-bootstrap'
        try {
            $name = "node-v$nodeVersion-win-$($arch.Node)"
            $zip = Join-Path $stage "$name.zip"
            Get-Verified "https://nodejs.org/dist/v$nodeVersion/$name.zip" `
                $versions["NODE_SHA256_WIN_$($arch.NodeHash)"] $zip
            Expand-Archive -LiteralPath $zip -DestinationPath $stage
            if (Test-Path -LiteralPath $WbNodeDir) { Remove-Item -LiteralPath $WbNodeDir -Recurse -Force }
            Move-Item -LiteralPath (Join-Path $stage $name) -Destination $WbNodeDir
        } finally {
            Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

Write-Host '==> Project toolchain'
& $WbUvExe --version
if (-not $UvOnly) {
    & (Join-Path $WbNodeDir 'node.exe') --version
    & (Join-Path $WbNodeDir 'npm.cmd') --version
}
