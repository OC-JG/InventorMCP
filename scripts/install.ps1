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
Write-Host "-- installing inventor_mcp (editable, with the inventor and dev extras)"
& $py -m pip install --quiet --upgrade pip
# Both extras, not a bare `-e .`. pywin32 lives in the `inventor` extra and is
# what the COM backend imports, and pytest lives in `dev`: installing neither
# produced a venv that could not reach Inventor and could not run the suite,
# reporting only "No module named pytest" -- which reads like a missing test
# dependency rather than a server that will not start.
& $py -m pip install --quiet -e "$root[inventor,dev]"
if ($LASTEXITCODE -ne 0) { throw "pip install failed. Run it again without --quiet to see why." }

& $py -c "import win32com.client" 2>$null
if ($LASTEXITCODE -eq 0) {
    Write-Host "-- pywin32 imports; the COM backend can reach Inventor"
} else {
    Write-Warning ("pywin32 did not import. Inventor's automation API is " +
        "unreachable without it, so the server will only offer the simulator. " +
        "Try: & '$py' -m pip install pywin32")
}

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
        # A second run of this script lands here because the name is taken,
        # which is the script having WORKED last time, not a failure.
        claude mcp get inventor *> $null
        if ($LASTEXITCODE -eq 0) {
            Write-Host "-- already registered"
        } else {
            Write-Warning "Registration failed; run it yourself:`n   $register"
        }
    }
} else {
    Write-Host ""
    Write-Host "To register with Claude Code, run:" -ForegroundColor Yellow
    Write-Host "   $register"
}

# -- 5. the Claude desktop app -----------------------------------------------
#
# Done here rather than left in the README, because the manual version is a
# merge and the merge is what goes wrong. Pasting the documented snippet into a
# config that already has settings in it produces two JSON objects end to end --
# valid-looking, parseable by eye, rejected by the app, and the only symptom is
# that the server is not there. That happened on the first machine this was
# installed on.
$desktopDir = Join-Path $env:APPDATA "Claude"
$desktopCfg = Join-Path $desktopDir "claude_desktop_config.json"
$entry = [pscustomobject]@{
    command = $py
    args    = @("-m", "inventor_mcp", "--backend", "auto")
}

if (-not (Test-Path $desktopDir)) {
    Write-Host "-- no Claude desktop app found ($desktopDir); skipping"
} else {
    $config = $null
    if (Test-Path $desktopCfg) {
        $raw = Get-Content $desktopCfg -Raw -Encoding utf8
        if ([string]::IsNullOrWhiteSpace($raw)) {
            # An empty file is not a broken one. ConvertFrom-Json throws on it in
            # Windows PowerShell and returns nothing in PowerShell 7, so without
            # this the two hosts disagree about whether to write anything.
            $config = [pscustomobject]@{}
        } else {
            try {
                $config = $raw | ConvertFrom-Json
            } catch {
                # Refuse rather than overwrite. Whatever is in there is somebody's
                # settings, and a file that does not parse is more likely to be a
                # half-finished edit than something to throw away.
                Write-Warning ("$desktopCfg is not valid JSON, so it has been left " +
                    "alone. Fix or delete it and re-run this script.`n   " +
                    $_.Exception.Message)
            }
            if ($config -isnot [psobject] -or $config -is [array]) {
                Write-Warning ("$desktopCfg parses, but its top level is not an " +
                    "object, so there is nowhere to put mcpServers. Left alone.")
                $config = $null
            }
            if ($config) { Copy-Item $desktopCfg "$desktopCfg.bak" -Force }
        }
    } else {
        $config = [pscustomobject]@{}
    }

    if ($config) {
        if (-not $config.PSObject.Properties['mcpServers']) {
            $config | Add-Member -NotePropertyName mcpServers -NotePropertyValue ([pscustomobject]@{})
        }
        if ($config.mcpServers.PSObject.Properties['inventor']) {
            $config.mcpServers.inventor = $entry
        } else {
            $config.mcpServers | Add-Member -NotePropertyName inventor -NotePropertyValue $entry
        }
        # WriteAllText rather than Set-Content: Windows PowerShell's utf8 writes
        # a byte-order mark, and a BOM in front of the opening brace is another
        # way to have a file that looks right and does not parse.
        $json = $config | ConvertTo-Json -Depth 24
        [System.IO.File]::WriteAllText($desktopCfg, $json, (New-Object System.Text.UTF8Encoding($false)))
        Write-Host "-- registered with the Claude desktop app"
        Write-Host "   $desktopCfg"
        Write-Host "   quit Claude from its tray icon -- not just the window -- and reopen it"
    }
}

# -- 6. prove it -------------------------------------------------------------
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
