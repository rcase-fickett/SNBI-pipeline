# SNBI Review App - Setup & Launch
# Checks for Python, installs packages, prompts for one-time configuration, then starts the app.
# Run again with -Reconfigure to update saved settings.

param(
    [switch]$Reconfigure,
    [switch]$Silent      # set by launch.vbs — suppresses output and skips prompts
)

$appDir = Split-Path -Parent $MyInvocation.MyCommand.Path

if (-not $Silent) {
    Write-Host ""
    Write-Host "  ================================================" -ForegroundColor Cyan
    Write-Host "           SNBI Review App" -ForegroundColor Cyan
    Write-Host "  ================================================" -ForegroundColor Cyan
    Write-Host ""
}

# In Silent mode, skip all setup prompts — env vars must already be set
if ($Silent) {
    $savedKey     = [Environment]::GetEnvironmentVariable("ANTHROPIC_API_KEY", "User")
    $savedBridges = [Environment]::GetEnvironmentVariable("SNBI_BRIDGES_ROOT", "User")
    $env:ANTHROPIC_API_KEY = $savedKey
    $env:SNBI_BRIDGES_ROOT = $savedBridges

    $pythonCmd = $null
    foreach ($cmd in @("py", "python", "python3")) {
        try {
            $ver = & $cmd --version 2>&1
            if ($ver -match "Python 3") { $pythonCmd = $cmd; break }
        } catch {}
    }
    if (-not $pythonCmd) { exit 1 }

    Set-Location $appDir
    # Open browser after 3 seconds (gives the server time to start)
    Start-Job -ScriptBlock { Start-Sleep 3; Start-Process "http://localhost:5000" } | Out-Null
    & $pythonCmd app.py --port 5000
    exit
}

# -- Step 0: Check for Python ---------------------------------------------
Write-Host "  Checking for Python..." -ForegroundColor White

$pythonCmd = $null
foreach ($cmd in @("py", "python", "python3")) {
    try {
        $ver = & $cmd --version 2>&1
        if ($ver -match "Python 3") {
            $pythonCmd = $cmd
            break
        }
    } catch {}
}

if (-not $pythonCmd) {
    Write-Host ""
    Write-Host "  Python is not installed on this computer." -ForegroundColor Red
    Write-Host ""
    Write-Host "  To install Python:" -ForegroundColor Yellow
    Write-Host "    1. Open a web browser and go to:  https://www.python.org/downloads/"
    Write-Host "    2. Click the big yellow 'Download Python 3.x.x' button"
    Write-Host "    3. Run the downloaded installer"
    Write-Host "    4. IMPORTANT: On the first screen, check the box that says"
    Write-Host "       'Add Python to PATH'  (it is unchecked by default)"
    Write-Host "    5. Click 'Install Now' and wait for it to finish"
    Write-Host "    6. Close this window, then double-click SNBI Review.bat again"
    Write-Host ""
    Read-Host "  Press Enter to exit"
    exit 1
}

$pyVersion = & $pythonCmd --version 2>&1
Write-Host "  Found: $pyVersion" -ForegroundColor Green
Write-Host ""

# -- Step 0b: Install required packages if missing ------------------------
Write-Host "  Checking required packages..." -ForegroundColor White

$packagesOk = $true
foreach ($pkg in @("flask", "anthropic", "pypdfium2")) {
    $check = & $pythonCmd -c "import $pkg" 2>&1
    if ($LASTEXITCODE -ne 0) {
        $packagesOk = $false
        break
    }
}

if (-not $packagesOk) {
    Write-Host "  Installing required packages (this only happens once)..." -ForegroundColor Yellow
    & $pythonCmd -m pip install -r "$appDir\requirements.txt"
    if ($LASTEXITCODE -ne 0) {
        Write-Host ""
        Write-Host "  Package installation failed." -ForegroundColor Red
        Write-Host "  Try running this window as Administrator, or contact Reed." -ForegroundColor Red
        Read-Host "  Press Enter to exit"
        exit 1
    }
    Write-Host "  Packages installed." -ForegroundColor Green
} else {
    Write-Host "  Packages OK." -ForegroundColor Green
}

Write-Host ""

# -- Steps 1 & 2: First-time configuration --------------------------------
$savedKey     = [Environment]::GetEnvironmentVariable("ANTHROPIC_API_KEY", "User")
$savedBridges = [Environment]::GetEnvironmentVariable("SNBI_BRIDGES_ROOT", "User")

$needsSetup = $Reconfigure -or (-not $savedKey) -or (-not $savedBridges)

if ($needsSetup) {
    Write-Host "  First-time setup (you will only need to do this once)" -ForegroundColor Yellow
    Write-Host ""

    # Step 1: Anthropic API key
    if (-not $savedKey -or $Reconfigure) {
        Write-Host "  STEP 1 - Anthropic API Key" -ForegroundColor White
        Write-Host "  -------------------------------------------------"
        Write-Host "  This key lets the app call Claude to read bridge plans."
        Write-Host "  Your key looks like:  sk-ant-api03-XXXXXXX..."
        Write-Host ""
        Write-Host "  If you don't have one, ask Reed - he can find it at:"
        Write-Host "  console.anthropic.com  ->  API Keys"
        Write-Host ""

        $apiKey = ""
        while (-not $apiKey) {
            $apiKey = (Read-Host "  Paste the API key and press Enter").Trim()
            if (-not $apiKey.StartsWith("sk-ant-")) {
                Write-Host "  That doesn't look right - keys start with sk-ant-" -ForegroundColor Red
                $apiKey = ""
            }
        }

        [Environment]::SetEnvironmentVariable("ANTHROPIC_API_KEY", $apiKey, "User")
        $env:ANTHROPIC_API_KEY = $apiKey
        Write-Host "  API key saved." -ForegroundColor Green
        Write-Host ""
    } else {
        $env:ANTHROPIC_API_KEY = $savedKey
        Write-Host "  STEP 1 - API key already saved." -ForegroundColor Green
        Write-Host ""
    }

    # Step 2: Bridges folder
    if (-not $savedBridges -or $Reconfigure) {
        Write-Host "  STEP 2 - Bridge Files Folder" -ForegroundColor White
        Write-Host "  -------------------------------------------------"
        Write-Host "  This is the '1 Bridges' folder inside the SNBI project on OneDrive."
        Write-Host ""
        Write-Host "  How to find the path:"
        Write-Host "    1. Open File Explorer"
        Write-Host "    2. Navigate to:  OneDrive - Fickett Structural Solutions"
        Write-Host "                       > 25071 ODOT SNBIT..."
        Write-Host "                         > 3) Plans, Specs, Photos"
        Write-Host "                           > 1 Bridges"
        Write-Host "    3. Click the address bar at the top of the window"
        Write-Host "    4. The full path will be highlighted - press Ctrl+C to copy it"
        Write-Host "    5. Come back here and paste it below"
        Write-Host ""

        $bridgesPath = ""
        while (-not $bridgesPath) {
            $bridgesPath = (Read-Host "  Paste the path and press Enter").Trim().Trim('"')
            if (-not (Test-Path $bridgesPath)) {
                Write-Host "  That folder was not found - check the path and try again" -ForegroundColor Red
                $bridgesPath = ""
            }
        }

        [Environment]::SetEnvironmentVariable("SNBI_BRIDGES_ROOT", $bridgesPath, "User")
        $env:SNBI_BRIDGES_ROOT = $bridgesPath
        Write-Host "  Bridge folder saved." -ForegroundColor Green
        Write-Host ""
    } else {
        $env:SNBI_BRIDGES_ROOT = $savedBridges
        Write-Host "  STEP 2 - Bridge folder already saved." -ForegroundColor Green
        Write-Host ""
    }

    Write-Host "  ================================================" -ForegroundColor Green
    Write-Host "  Setup complete!  Starting app..." -ForegroundColor Green
    Write-Host "  ================================================" -ForegroundColor Green
    Write-Host ""

} else {
    $env:ANTHROPIC_API_KEY = $savedKey
    $env:SNBI_BRIDGES_ROOT = $savedBridges
    Write-Host "  Configuration OK.  Starting app..." -ForegroundColor Green
    Write-Host ""
}

# -- Start the app --------------------------------------------------------
Set-Location $appDir
Write-Host "  Opening browser..." -ForegroundColor Green
Start-Job -ScriptBlock { Start-Sleep 3; Start-Process "http://localhost:5000" } | Out-Null
& $pythonCmd app.py --port 5000
