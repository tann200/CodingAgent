# DEPRECATED: use `scripts/run.sh` or `uv run codingagent` instead.
# start.ps1 - PowerShell startup script for CodingAgent
# Behavior mirrors start.sh:
# - Prefer python3.11
# - Create/activate venv in .venv
# - Only install requirements when requirements.txt changed (cache hash in .venv/.requirements.sha256)
# - Do not automatically install missing modules unless AUTO_INSTALL=1
# - Support DRY_RUN to avoid network actions
# - Support ENABLE_TUI environment variable
# - Check GitHub Copilot authentication state
#
# Environment variables (same as start.sh):
#   ENABLE_TUI=0      # Disable TUI (default: 1)
#   AUTO_INSTALL=1    # Auto-install missing modules
#   FORCE_INSTALL=1   # Force reinstall requirements
#   DRY_RUN=1        # Test without network actions
#
# Usage:
#   .\start.ps1                    # Normal start
#   .\start.ps1 -DryRun             # Test without network
#   .\start.ps1 -AutoInstall        # Auto-install missing modules
#   .\start.ps1 -ForceInstall       # Force reinstall requirements

param(
    [switch]$DryRun,
    [switch]$AutoInstall,
    [switch]$ForceInstall
)

$ErrorActionPreference = 'Stop'  # Equivalent to set -euo pipefail

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

$PROJECT_ROOT = $ScriptDir
$VENV_DIR = Join-Path $PROJECT_ROOT '.venv'
$VENV_PYTHON = Join-Path $VENV_DIR 'Scripts\python.exe'
$REQ_FILE = Join-Path $PROJECT_ROOT 'requirements.txt'
$REQ_HASH_FILE = Join-Path $VENV_DIR '.requirements.sha256'

# Check ENABLE_TUI (default: 1)
$ENABLE_TUI = if ($env:ENABLE_TUI) { $env:ENABLE_TUI } else { '1' }
Write-Host "[start.ps1] ENABLE_TUI=$ENABLE_TUI"
Write-Host "[start.ps1] Project root: $PROJECT_ROOT"

# Determine python command (prefer python3.11)
$PYTHON_CMD = 'python'
if (Get-Command python3.11 -ErrorAction SilentlyContinue) { $PYTHON_CMD = 'python3.11' }
elseif (Get-Command python3 -ErrorAction SilentlyContinue) { $PYTHON_CMD = 'python3' }

Write-Host "[start.ps1] Using python command: $PYTHON_CMD"
Write-Host "[start.ps1] DRY_RUN=$DryRun AUTO_INSTALL=$AutoInstall FORCE_INSTALL=$ForceInstall"

# Determine python command (prefer python3.11)
$pythonCmd = 'python'
if (Get-Command python3.11 -ErrorAction SilentlyContinue) { $pythonCmd = 'python3.11' }
elseif (Get-Command python3 -ErrorAction SilentlyContinue) { $pythonCmd = 'python3' }

Write-Host "[start.ps1] Using python command: $pythonCmd"

if (-not (Test-Path $VENV_DIR)) {
    Write-Host "[start.ps1] Creating virtual environment in $VENV_DIR using $PYTHON_CMD..."
    & $PYTHON_CMD -m venv $VENV_DIR
}

if (-not (Test-Path $VENV_PYTHON)) {
    Write-Error "[start.ps1] Expected python executable at $VENV_PYTHON not found"
    exit 1
}

# Helper: compute SHA256 for requirements.txt
function Compute-ReqHash {
    param([string]$path)
    if (-not (Test-Path $path)) { return '' }
    $bytes = Get-Content -Path $path -Raw -Encoding UTF8
    $sha = [System.Security.Cryptography.SHA256]::Create()
    $hash = $sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($bytes))
    return ($hash | ForEach-Object { $_.ToString('x2') }) -join ''
}

$REQ_HASH = Compute-ReqHash -path $REQ_FILE
$OLD_HASH = ''
if (Test-Path $REQ_HASH_FILE) { 
    $OLD_HASH = (Get-Content $REQ_HASH_FILE -Raw).Trim() 
}

if (Test-Path $ReqFile) {
    Write-Host "[start.ps1] requirements detected at $ReqFile"
    if ($ForceInstall -or ($reqHash -ne $oldHash)) {
        Write-Host "[start.ps1] Requirements changed or ForceInstall set; installing dependencies..."
        # Attempt to run 'uv' if available, otherwise pip install -r
        try {
            & $VenvPython -c "import importlib; import sys; sys.exit(0 if importlib.util.find_spec('uv') else 1)"
            $uvPresent = $LASTEXITCODE -eq 0
        } catch {
            $uvPresent = $false
        }
        if (-not $uvPresent) {
            Write-Host "[start.ps1] 'uv' not found in venv; installing 'uv' via pip"
            & $VenvPython -m pip install --upgrade uv
            # Re-evaluate uv presence
            & $VenvPython -c "import importlib,sys; sys.exit(0 if importlib.util.find_spec('uv') else 1)" | Out-Null
            $uvPresent = $LASTEXITCODE -eq 0
        }
        if ($uvPresent) {
            Write-Host "[start.ps1] 'uv' found in venv; attempting 'uv install --no-input'"
            try {
                & $VenvPython -m uv install --no-input
                Write-Host "[start.ps1] 'uv install' succeeded"
            } catch {
                Write-Warning "[start.ps1] 'uv install' failed; attempting fallback 'pip install -r requirements.txt'"
                try {
                    & $VenvPython -m pip install -r $ReqFile
                    Write-Host "[start.ps1] pip install -r requirements.txt succeeded as fallback"
                } catch {
                    if ($ForceInstall) {
                        Write-Error "[start.ps1] FORCE_INSTALL set and installs failed; aborting"; exit 1
                    } else {
                        Write-Warning "[start.ps1] Install failures suppressed (not ForceInstall). Continuing startup."
                    }
                }
            }
        } else {
            Write-Error "[start.ps1] 'uv' unavailable and pip install failed; aborting"; exit 1
        }
        # Save hash
        $reqHash | Out-File -FilePath $ReqHashFile -Encoding ASCII
    } else {
        Write-Host "[start.ps1] Requirements unchanged; skipping install. (use -ForceInstall to force)"
    }
} else {
    Write-Host "[start.ps1] No requirements.txt found; skipping dependency installation"
}

# Critical imports check - match start.sh imports
$CRITICAL_IMPORTS = @('textual','requests','httpx','openai','uv','langgraph','langchain_core')
$MISSING = @()
foreach ($mod in $CRITICAL_IMPORTS) {
    try {
        & $VENV_PYTHON -c "import importlib,sys; sys.exit(0 if importlib.util.find_spec('$mod') else 1)" | Out-Null
        if ($LASTEXITCODE -ne 0) { $MISSING += $mod }
    } catch {
        $MISSING += $mod
    }
}

if ($MISSING.Count -gt 0) {
    Write-Host "[start.ps1] Missing critical modules: $($MISSING -join ', ')"
    if ($AutoInstall) {
        Write-Host "[start.ps1] AUTO_INSTALL set - attempting to install missing modules via pip"
        & $VENV_PYTHON -m pip install $MISSING
    } else {
        Write-Host "[start.ps1] To install missing modules automatically, re-run with -AutoInstall"
        Write-Host "[start.ps1] Or manually run: $VENV_PYTHON -m pip install $($MISSING -join ' ')"
    }
}

# Check provider authentication state (best-effort — never blocks startup)
Write-Host "[start.ps1] Checking provider auth state..."
$COPILOT_AUTH_OK = $false
$PREFS_PATH = Join-Path $env:USERPROFILE ".config\codingagent\prefs.json"
if (Test-Path $PREFS_PATH) {
    try {
        $prefs = Get-Content $PREFS_PATH -Raw | ConvertFrom-Json
        $token = $prefs.providers.github_copilot.github_token
        if ($token) { $COPILOT_AUTH_OK = $true }
    } catch { }
}

if (-not $COPILOT_AUTH_OK) {
    Write-Host ""
    Write-Host "  ┌─────────────────────────────────────────────────────────────────────┐"
    Write-Host "  │  GitHub Copilot: not yet authenticated                              │"
    Write-Host "  │                                                                     │"
    Write-Host "  │  No OAuth token found.  The TUI will start normally — you can       │"
    Write-Host "  │  connect GitHub Copilot from inside the TUI:                        │"
    Write-Host "  │    Settings (ctrl+s)  →  API Keys  →  Login with GitHub Copilot    │"
    Write-Host "  │                                                                     │"
    Write-Host "  │  Or run the CLI login in another terminal and restart:              │"
    Write-Host "  │    python scripts/github_copilot_login.py                          │"
    Write-Host "  └─────────────────────────────────────────────────────────────────────┘"
    Write-Host ""
} else {
    Write-Host "[start.ps1] GitHub Copilot: authenticated OK"
}

# Dry run support
if ($DryRun) {
    Write-Host "[start.ps1] DRY_RUN enabled: skipping package installation and app exec."
    Write-Host "[start.ps1] Would run: $VENV_PYTHON -u -m src.main"
    exit 0
}

# Run entrypoint
Write-Host "[start.ps1] Locating entrypoint..."
try {
    & $VENV_PYTHON -c "import importlib,sys; sys.exit(0 if importlib.util.find_spec('src.main') else 1)" | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "[start.ps1] Running module entrypoint: python -m src.main"
        & $VENV_PYTHON -u -m src.main @args
        exit $LASTEXITCODE
    } elseif (Test-Path (Join-Path $PROJECT_ROOT 'main.py')) {
        Write-Host "[start.ps1] Running script entrypoint: main.py"
        & $VENV_PYTHON -u (Join-Path $PROJECT_ROOT 'main.py') @args
        exit $LASTEXITCODE
    } else {
        Write-Error "[start.ps1] ERROR: no entrypoint found (module 'src.main' or main.py)."
        exit 1
    }
} catch {
    Write-Error "[start.ps1] Error locating entrypoint: $_"
    exit 1
}
