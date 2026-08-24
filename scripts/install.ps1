<#
One command from a fresh clone to a working internal tool, on the machine that
has Inventor.

    git clone --recurse-submodules https://github.com/OC-JG/InventorMCP.git
    cd InventorMCP
    powershell -ExecutionPolicy Bypass -File scripts\install.ps1

What it does, in order, saying so as it goes:

  1. pulls the DFM analyser submodule if the clone was made without it
  2. makes .venv and installs this package into it
  3. checks for Node, which the analyser needs (the analysis is JavaScript,
     run through the DFM tool's own modules -- no npm install required)
  4. registers the server with Claude Code, if the claude CLI is on PATH;
     otherwise prints the one command to run

Safe to re-run: every step is idempotent.
#>

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

Write-Host "== InventorMCP install ==" -ForegroundColor Cyan
Write-Host "   in $root"

# -- 1. the analyser ---------------------------------------------------------
if (-not (Test-Path "$root\dfm\src\rules\engine.js")) {
    Write-Host "-- fetching the DFM analyser submodule"
    git submodule update --init --depth 1 dfm
    if (-not (Test-Path "$root\dfm\src\rules\engine.js")) {
        Write-Warning ("The DFM submodule did not arrive. The modelling tools " +
            "work without it; the manufacturability tools will say what is " +
            "missing. Fix with: git submodule update --init dfm")
    }
} else {
    Write-Host "-- DFM analyser present (dfm\)"
}

# -- 2. the environment ------------------------------------------------------
if (-not (Test-Path "$root\.venv")) {
    Write-Host "-- creating .venv"
    python -m venv "$root\.venv"
}
$py = "$root\.venv\Scripts\python.exe"
Write-Host "-- installing inventor_mcp (editable)"
& $py -m pip install --quiet --upgrade pip
& $py -m pip install --quiet -e "$root"

# -- 3. node -----------------------------------------------------------------
$node = Get-Command node -ErrorAction SilentlyContinue
if ($node) {
    Write-Host "-- node found: $((node --version))"
} else {
    Write-Warning ("Node was not found. The DFM analysis is JavaScript run " +
        "headlessly; install Node 18+ from https://nodejs.org (no npm install " +
        "is needed afterwards). Everything except the DFM tools works without it.")
}

# -- 4. Claude ---------------------------------------------------------------
$register = "claude mcp add inventor -- `"$py`" -m inventor_mcp"
$claude = Get-Command claude -ErrorAction SilentlyContinue
if ($claude) {
    Write-Host "-- registering with Claude Code"
    claude mcp add inventor -- "$py" -m inventor_mcp
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Registration failed; run it yourself:`n   $register"
    }
} else {
    Write-Host ""
    Write-Host "To register with Claude Code, run:" -ForegroundColor Yellow
    Write-Host "   $register"
}

# -- 5. prove it -------------------------------------------------------------
Write-Host "-- checking the pieces line up"
& $py -c @"
from inventor_mcp.dfm.runner import find_dfm_root, DfmUnavailable
try:
    print('   analyser:', find_dfm_root())
except DfmUnavailable as exc:
    print('   analyser: not found --', exc.hint)
from inventor_mcp.server import create_server
create_server('mock')
print('   server:   builds')
"@

Write-Host ""
Write-Host "Done. In Claude Code, 'connect' finds Inventor when it is running." -ForegroundColor Green
Write-Host "The DFM browser tool is the same checkout: open dfm\dfm-tool.html."
