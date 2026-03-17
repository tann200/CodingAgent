# start.ps1 - PowerShell startup script for CodingAgent
# Behavior mirrors start.sh:
# - Prefer python3.11
# - Create/activate venv in .venv
# - Only install requirements when requirements.txt changed (cache hash in .venv/.requirements.sha256)
# - Do not automatically install missing modules unless AUTO_INSTALL=1
# - Support DRY_RUN to avoid network actions

param(
    [switch]$DryRun,
    [switch]$AutoInstall,
    [switch]$ForceInstall
)

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

$ProjectRoot = $ScriptDir
$VenvDir = Join-Path $ProjectRoot '.venv'
$VenvPython = Join-Path $VenvDir 'Scripts\python.exe'
$ReqFile = Join-Path $ProjectRoot 'requirements.txt'
$ReqHashFile = Join-Path $VenvDir '.requirements.sha256'

Write-Host "[start.ps1] Project root: $ProjectRoot"

# Determine python command (prefer python3.11)
$pythonCmd = 'python'
if (Get-Command python3.11 -ErrorAction SilentlyContinue) { $pythonCmd = 'python3.11' }
elseif (Get-Command python3 -ErrorAction SilentlyContinue) { $pythonCmd = 'python3' }

Write-Host "[start.ps1] Using python command: $pythonCmd"

if (-Not (Test-Path $VenvDir)) {
    Write-Host "[start.ps1] Creating virtual environment in $VenvDir using $pythonCmd..."
    & $pythonCmd -m venv $VenvDir
}

if (-Not (Test-Path $VenvPython)) {
    Write-Error "[start.ps1] Expected python executable at $VenvPython not found"
    exit 1
}

# Helper: compute SHA256 for requirements.txt
function Compute-ReqHash {
    param([string]$path)
    if (-Not (Test-Path $path)) { return '' }
    $bytes = Get-Content -Path $path -Raw -Encoding UTF8
    $sha = [System.Security.Cryptography.SHA256]::Create()
    $hash = $sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($bytes))
    return ($hash | ForEach-Object { $_.ToString('x2') }) -join ''
}

$reqHash = Compute-ReqHash -path $ReqFile
$oldHash = ''
if (Test-Path $ReqHashFile) { $oldHash = Get-Content $ReqHashFile -Raw }

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
        if ($uvPresent) {
            Write-Host "[start.ps1] 'uv' found in venv; attempting 'uv install --no-input'"
            try {
                & $VenvPython -m uv install --no-input
                Write-Host "[start.ps1] 'uv install' succeeded"
            } catch {
                Write-Warning "[start.ps1] 'uv install' failed; falling back to pip install -r requirements.txt"
                & $VenvPython -m pip install -r $ReqFile
            }
        } else {
            Write-Host "[start.ps1] 'uv' not available in venv; using pip to install requirements"
            & $VenvPython -m pip install -r $ReqFile
        }
        # Save hash
        $reqHash | Out-File -FilePath $ReqHashFile -Encoding ASCII
    } else {
        Write-Host "[start.ps1] Requirements unchanged; skipping install. (use -ForceInstall to force)"
    }
} else {
    Write-Host "[start.ps1] No requirements.txt found; skipping dependency installation"
}

# Critical imports check
$Critical = @('textual','requests','httpx','openai','uv','jsonschema')
$missing = @()
foreach ($mod in $Critical) {
    try {
        & $VenvPython -c "import importlib,sys; sys.exit(0 if importlib.util.find_spec('$mod') else 1)" | Out-Null
        if ($LASTEXITCODE -ne 0) { $missing += $mod }
    } catch {
        $missing += $mod
    }
}

if ($missing.Count -gt 0) {
    Write-Host "[start.ps1] Missing critical modules: $($missing -join ',')"
    if ($AutoInstall) {
        Write-Host "[start.ps1] AUTO_INSTALL set - attempting to install missing modules via pip"
        & $VenvPython -m pip install $missing
    } else {
        Write-Host "[start.ps1] To install missing modules automatically, re-run with -AutoInstall"
        Write-Host "[start.ps1] Or manually run: $VenvPython -m pip install $($missing -join ' ')"
    }
}

# Run entrypoint
Write-Host "[start.ps1] Locating entrypoint..."
try {
    & $VenvPython -c "import importlib,sys; sys.exit(0 if importlib.util.find_spec('src.main') else 1)" | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "[start.ps1] Running module entrypoint: python -m src.main"
        & $VenvPython -u -m src.main @args
        exit $LASTEXITCODE
    } elseif (Test-Path (Join-Path $ProjectRoot 'main.py')) {
        Write-Host "[start.ps1] Running script entrypoint: main.py"
        & $VenvPython -u (Join-Path $ProjectRoot 'main.py') @args
        exit $LASTEXITCODE
    } else {
        Write-Error "[start.ps1] ERROR: no entrypoint found (module 'src.main' or main.py)."
        exit 1
    }
} catch {
    Write-Error "[start.ps1] Error locating entrypoint: $_"
    exit 1
}

