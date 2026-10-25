# Repository-owned process environment for native Windows. Dot-source it:
#   . "$PSScriptRoot\project-env.ps1"
# The Windows counterpart of project-env.sh: every cache, temp and profile
# directory a dependency might write to points inside this checkout, for this
# PowerShell process and its children only. Nothing is written to the user's
# PATH, registry or profile.
#
# Windows PowerShell 5.1 compatible and ASCII only (5.1 reads a BOM-less script
# in the ANSI code page, so a non-ASCII character here would be mangled).

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$script:WbProjectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')).TrimEnd('\')
$script:WbRuntimeDir = Join-Path $WbProjectRoot '.runtime'
$script:WbCacheDir = Join-Path $WbRuntimeDir 'cache'
$script:WbConfigDir = Join-Path $WbRuntimeDir 'config'
$script:WbDataDir = Join-Path $WbRuntimeDir 'data'
$script:WbStateDir = Join-Path $WbRuntimeDir 'state'
$script:WbTmpDir = Join-Path $WbRuntimeDir 'tmp'
$script:WbToolsDir = Join-Path $WbRuntimeDir 'tools'
$script:WbModelsDir = Join-Path $WbProjectRoot 'models'
$script:WbVenvPython = Join-Path $WbProjectRoot '.venv\Scripts\python.exe'

function Test-WbInside([string]$Candidate) {
    # True when $Candidate is the checkout or below it, after following every
    # junction or symbolic link on the way down. Spelling a path as
    # .runtime\cache is not containment if .runtime is a junction to D:\.
    $full = [System.IO.Path]::GetFullPath($Candidate).TrimEnd('\')
    $root = $WbProjectRoot
    if (-not ($full -eq $root -or $full.StartsWith($root + '\', [System.StringComparison]::OrdinalIgnoreCase))) {
        return $false
    }
    $relative = $full.Substring($root.Length).TrimStart('\')
    $walk = $root
    foreach ($part in ($relative -split '\\' | Where-Object { $_ })) {
        $walk = Join-Path $walk $part
        $item = Get-Item -LiteralPath $walk -Force -ErrorAction SilentlyContinue
        if ($null -eq $item) { break }
        if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
            $target = @($item.Target) | Select-Object -First 1
            if (-not $target) { return $false }
            if (-not [System.IO.Path]::IsPathRooted($target)) {
                $target = Join-Path (Split-Path $walk -Parent) $target
            }
            $resolved = [System.IO.Path]::GetFullPath($target).TrimEnd('\')
            if (-not ($resolved -eq $root -or $resolved.StartsWith($root + '\', [System.StringComparison]::OrdinalIgnoreCase))) {
                return $false
            }
        }
    }
    return $true
}

function Assert-WbInside([string]$Candidate) {
    if (-not (Test-WbInside $Candidate)) {
        throw "Refusing project path that resolves outside the checkout: $Candidate"
    }
}

function Read-WbToolVersions {
    # KEY=VALUE lines of scripts\tool-versions.env, shared with the POSIX tools.
    $versions = @{}
    foreach ($line in Get-Content -LiteralPath (Join-Path $PSScriptRoot 'tool-versions.env')) {
        if ($line -match '^\s*([A-Z0-9_]+)=(.*)$') { $versions[$Matches[1]] = $Matches[2].Trim() }
    }
    return $versions
}

function Get-WbArch {
    switch ($env:PROCESSOR_ARCHITECTURE) {
        'AMD64' { return @{ Uv = 'x86_64'; Node = 'x64'; UvHash = 'X86_64'; NodeHash = 'X64' } }
        'ARM64' { return @{ Uv = 'aarch64'; Node = 'arm64'; UvHash = 'AARCH64'; NodeHash = 'ARM64' } }
        default { throw "Unsupported Windows architecture: $($env:PROCESSOR_ARCHITECTURE)" }
    }
}

foreach ($managed in @($WbRuntimeDir, $WbCacheDir, $WbConfigDir, $WbDataDir, $WbStateDir,
                       $WbTmpDir, $WbToolsDir, (Join-Path $WbProjectRoot '.venv'), $WbModelsDir,
                       (Join-Path $WbModelsDir 'huggingface'), (Join-Path $WbModelsDir 'weights'),
                       (Join-Path $WbModelsDir 'loras'), (Join-Path $WbProjectRoot 'output'),
                       (Join-Path $WbProjectRoot 'frontend\node_modules'),
                       (Join-Path $WbProjectRoot 'frontend\dist'), (Join-Path $WbProjectRoot '.env'))) {
    Assert-WbInside $managed
}

$wbHome = Join-Path $WbRuntimeDir 'home'
$wbEnv = [ordered]@{
    WB_PROJECT_ENV_LOADED = '1'
    WB_PROJECT_ROOT = $WbProjectRoot
    WB_RUNTIME_DIR = $WbRuntimeDir
    WB_CACHE_DIR = $WbCacheDir
    WB_CONFIG_DIR = $WbConfigDir
    WB_DATA_DIR = $WbDataDir
    WB_STATE_DIR = $WbStateDir
    WB_TMP_DIR = $WbTmpDir
    WB_TOOLS_DIR = $WbToolsDir
    WB_MODELS_DIR = $WbModelsDir
    # Windows libraries find "home" through USERPROFILE and their caches
    # through APPDATA/LOCALAPPDATA; HOME is set as well for ported tools.
    HOME = $wbHome
    USERPROFILE = $wbHome
    APPDATA = (Join-Path $wbHome 'AppData\Roaming')
    LOCALAPPDATA = (Join-Path $wbHome 'AppData\Local')
    XDG_CACHE_HOME = (Join-Path $WbCacheDir 'xdg')
    XDG_CONFIG_HOME = (Join-Path $WbConfigDir 'xdg')
    XDG_DATA_HOME = (Join-Path $WbDataDir 'xdg')
    XDG_STATE_HOME = (Join-Path $WbStateDir 'xdg')
    TMPDIR = $WbTmpDir
    TMP = $WbTmpDir
    TEMP = $WbTmpDir
    UV_CACHE_DIR = (Join-Path $WbCacheDir 'uv')
    UV_PYTHON_INSTALL_DIR = (Join-Path $WbRuntimeDir 'python')
    UV_TOOL_DIR = (Join-Path $WbRuntimeDir 'uv-tools')
    UV_TOOL_BIN_DIR = (Join-Path $WbRuntimeDir 'bin')
    UV_PROJECT_ENVIRONMENT = (Join-Path $WbProjectRoot '.venv')
    # uv must not look up or register interpreters in the Windows registry.
    UV_PYTHON_INSTALL_REGISTRY = '0'
    PIP_CACHE_DIR = (Join-Path $WbCacheDir 'pip')
    PIP_CONFIG_FILE = (Join-Path $WbConfigDir 'pip\pip.ini')
    PYTHONUSERBASE = (Join-Path $WbRuntimeDir 'python-user')
    PYTHONPYCACHEPREFIX = (Join-Path $WbCacheDir 'python-bytecode')
    PYTHONUTF8 = '1'
    RUFF_CACHE_DIR = (Join-Path $WbCacheDir 'ruff')
    NPM_CONFIG_CACHE = (Join-Path $WbCacheDir 'npm')
    NPM_CONFIG_PREFIX = (Join-Path $WbRuntimeDir 'npm-prefix')
    NPM_CONFIG_USERCONFIG = (Join-Path $WbConfigDir 'npm\npmrc')
    COREPACK_HOME = (Join-Path $WbCacheDir 'corepack')
    PLAYWRIGHT_BROWSERS_PATH = (Join-Path $WbCacheDir 'playwright')
    ELECTRON_CACHE = (Join-Path $WbCacheDir 'electron')
    NODE_COMPILE_CACHE = (Join-Path $WbCacheDir 'node-compile')
    HF_HOME = (Join-Path $WbModelsDir 'huggingface')
    HF_HUB_CACHE = (Join-Path $WbModelsDir 'huggingface\hub')
    HF_XET_CACHE = (Join-Path $WbModelsDir 'huggingface\xet')
    HF_ASSETS_CACHE = (Join-Path $WbModelsDir 'huggingface\assets')
    HF_MODULES_CACHE = (Join-Path $WbModelsDir 'huggingface\modules')
    HUGGINGFACE_HUB_CACHE = (Join-Path $WbModelsDir 'huggingface\hub')
    SENTENCE_TRANSFORMERS_HOME = (Join-Path $WbModelsDir 'sentence-transformers')
    TORCH_HOME = (Join-Path $WbModelsDir 'torch')
    TORCH_EXTENSIONS_DIR = (Join-Path $WbCacheDir 'torch-extensions')
    TORCHINDUCTOR_CACHE_DIR = (Join-Path $WbCacheDir 'torch-inductor')
    TRITON_CACHE_DIR = (Join-Path $WbCacheDir 'triton')
    CUDA_CACHE_PATH = (Join-Path $WbCacheDir 'cuda')
    NUMBA_CACHE_DIR = (Join-Path $WbCacheDir 'numba')
    MPLCONFIGDIR = (Join-Path $WbConfigDir 'matplotlib')
    IMAGEIO_USERDIR = (Join-Path $WbDataDir 'imageio')
    KERAS_HOME = (Join-Path $WbModelsDir 'keras')
    ONNX_HOME = (Join-Path $WbModelsDir 'onnx')
    CARGO_HOME = (Join-Path $WbRuntimeDir 'cargo')
    RUSTUP_HOME = (Join-Path $WbRuntimeDir 'rustup')
    CCACHE_DIR = (Join-Path $WbCacheDir 'ccache')
    SCCACHE_DIR = (Join-Path $WbCacheDir 'sccache')
    IPYTHONDIR = (Join-Path $WbConfigDir 'ipython')
    JUPYTER_CONFIG_DIR = (Join-Path $WbConfigDir 'jupyter')
    WANDB_DIR = (Join-Path $WbStateDir 'wandb')
    WANDB_CACHE_DIR = (Join-Path $WbCacheDir 'wandb')
    WANDB_CONFIG_DIR = (Join-Path $WbConfigDir 'wandb')
    OUTPUT_DIR = (Join-Path $WbProjectRoot 'output')
    LORA_DIR = (Join-Path $WbModelsDir 'loras')
    WEIGHTS_DIR = (Join-Path $WbModelsDir 'weights')
    RUNTIME_DIR = $WbRuntimeDir
}
$wbFlags = @('WB_PROJECT_ENV_LOADED', 'UV_PYTHON_INSTALL_REGISTRY', 'PYTHONUTF8')
$wbNotDirectories = $wbFlags + @('PIP_CONFIG_FILE', 'NPM_CONFIG_USERCONFIG')
# Check every writable destination before the first directory is created.
foreach ($name in $wbEnv.Keys) {
    if ($wbFlags -notcontains $name) { Assert-WbInside $wbEnv[$name] }
}
foreach ($name in $wbEnv.Keys) { Set-Item -Path "Env:$name" -Value $wbEnv[$name] }
foreach ($name in $wbEnv.Keys) {
    if ($wbNotDirectories -contains $name) { continue }
    New-Item -ItemType Directory -Force -Path $wbEnv[$name] | Out-Null
}
foreach ($file in @($env:PIP_CONFIG_FILE, $env:NPM_CONFIG_USERCONFIG)) {
    New-Item -ItemType Directory -Force -Path (Split-Path $file -Parent) | Out-Null
    if (-not (Test-Path -LiteralPath $file)) { New-Item -ItemType File -Path $file | Out-Null }
}

# Native tools (uv, Python with PYTHONUTF8, node) write UTF-8. Windows
# PowerShell 5.1 decodes their output in the OEM code page unless told
# otherwise, which turned a checkout path such as "wb smoke <e-acute>" into
# mojibake and made the containment check reject uv's own Python path.
try { [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false) } catch { }
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$wbVersions = Read-WbToolVersions
$wbArch = Get-WbArch
$script:WbNodeDir = Join-Path $WbToolsDir ("node-v{0}-win-{1}" -f $wbVersions['NODE_VERSION'], $wbArch.Node)
$script:WbUvExe = Join-Path $WbToolsDir 'uv\uv.exe'
foreach ($dir in @((Join-Path $WbRuntimeDir 'bin'), (Join-Path $WbToolsDir 'uv'), $WbNodeDir)) {
    $parts = $env:PATH -split ';'
    if ($parts -notcontains $dir) { $env:PATH = "$dir;$env:PATH" }
}
