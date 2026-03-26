param(
    [string]$PythonVersion = "3.11",
    [string]$VenvDir = "venv",
    [switch]$BaseOnly,
    [switch]$SkipNodeDeps,
    [switch]$SkipPlaywright,
    [switch]$SkipWhatsAppBridge,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = [System.IO.Path]::GetFullPath((Join-Path $ScriptDir ".."))
$PyprojectPath = Join-Path $RepoRoot "pyproject.toml"
$PackageJsonPath = Join-Path $RepoRoot "package.json"
$WhatsAppBridgeDir = Join-Path $RepoRoot "scripts\whatsapp-bridge"
$WhatsAppBridgePackageJson = Join-Path $WhatsAppBridgeDir "package.json"
$VenvPath = Join-Path $RepoRoot $VenvDir
$VenvPython = Join-Path $VenvPath "Scripts\python.exe"

function Write-Info {
    param([string]$Message)
    Write-Host "[info] $Message" -ForegroundColor Cyan
}

function Write-Success {
    param([string]$Message)
    Write-Host "[ok]   $Message" -ForegroundColor Green
}

function Write-Warn {
    param([string]$Message)
    Write-Host "[warn] $Message" -ForegroundColor Yellow
}

function Write-Err {
    param([string]$Message)
    Write-Host "[err]  $Message" -ForegroundColor Red
}

function Format-Command {
    param(
        [string]$FilePath,
        [string[]]$Arguments
    )

    $parts = @($FilePath) + @($Arguments)
    return ($parts | ForEach-Object {
        if ($_ -match '\s') { '"' + $_ + '"' } else { $_ }
    }) -join " "
}

function Invoke-Step {
    param(
        [string]$FilePath,
        [string[]]$Arguments,
        [string]$WorkingDirectory = $RepoRoot
    )

    $display = Format-Command -FilePath $FilePath -Arguments $Arguments
    Write-Info $display
    if ($DryRun) {
        return
    }

    Push-Location $WorkingDirectory
    try {
        & $FilePath @Arguments
        if ($LASTEXITCODE -ne 0) {
            throw "Command failed with exit code ${LASTEXITCODE}: $display"
        }
    } finally {
        Pop-Location
    }
}

function Get-CommandPath {
    param([string]$Name)

    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }
    return $null
}

function Get-UvPath {
    $candidates = @(
        (Get-CommandPath "uv"),
        "$env:USERPROFILE\.local\bin\uv.exe",
        "$env:USERPROFILE\.cargo\bin\uv.exe"
    ) | Where-Object { $_ }

    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) {
            return $candidate
        }
    }

    return $null
}

function Refresh-UserPath {
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $combined = @($userPath, $machinePath) | Where-Object { $_ } | Select-Object -Unique
    if ($combined.Count -gt 0) {
        $env:Path = $combined -join ";"
    }
}

function Ensure-Uv {
    $uvPath = Get-UvPath
    if ($uvPath) {
        if (-not $DryRun) {
            $version = & $uvPath --version
            Write-Success "uv found ($version)"
        } else {
            Write-Success "uv would be used from $uvPath"
        }
        return $uvPath
    }

    Write-Warn "uv was not found. Installing it with Astral's Windows installer."
    if ($DryRun) {
        Write-Info 'powershell -ExecutionPolicy Bypass -NoProfile -Command "irm https://astral.sh/uv/install.ps1 | iex"'
        return "uv"
    }

    powershell -ExecutionPolicy Bypass -NoProfile -Command "irm https://astral.sh/uv/install.ps1 | iex"
    Refresh-UserPath

    $uvPath = Get-UvPath
    if (-not $uvPath) {
        throw "uv installation finished but uv.exe is still not available."
    }

    $version = & $uvPath --version
    Write-Success "uv installed ($version)"
    return $uvPath
}

function Ensure-Python {
    param([string]$UvPath)

    if ($DryRun) {
        Write-Info "Would ensure Python $PythonVersion is installed via uv."
        return
    }

    $found = $null
    try {
        $found = & $UvPath python find $PythonVersion 2>$null
    } catch {
        $found = $null
    }

    if ($found) {
        $version = & $found --version 2>$null
        Write-Success "Python ready ($version)"
        return
    }

    Invoke-Step -FilePath $UvPath -Arguments @("python", "install", $PythonVersion)
    $found = & $UvPath python find $PythonVersion 2>$null
    if (-not $found) {
        throw "uv could not locate Python $PythonVersion after installation."
    }
    $version = & $found --version 2>$null
    Write-Success "Python ready ($version)"
}

function Ensure-RepoRoot {
    if (-not (Test-Path $PyprojectPath)) {
        throw "pyproject.toml was not found. Run this script from the Hermes repo checkout."
    }
}

function Maybe-InstallNodeDeps {
    if ($SkipNodeDeps) {
        Write-Info "Skipping Node.js dependency bootstrap."
        return
    }

    $npmPath = Get-CommandPath "npm"
    if (-not $npmPath) {
        Write-Warn "Node.js/npm is not available. Skipping browser and bridge npm installs."
        Write-Info "Install Node.js LTS later if you want browser tooling and WhatsApp bridge support."
        return
    }

    if (Test-Path $PackageJsonPath) {
        Invoke-Step -FilePath $npmPath -Arguments @("install", "--silent")
        Write-Success "Root npm dependencies installed"
    }

    if ((-not $SkipPlaywright) -and (Test-Path $PackageJsonPath)) {
        $npxPath = Get-CommandPath "npx"
        if ($npxPath) {
            Invoke-Step -FilePath $npxPath -Arguments @("playwright", "install", "chromium")
            Write-Success "Playwright Chromium installed"
        } else {
            Write-Warn "npx was not found. Skipping Playwright browser install."
        }
    } elseif (-not $SkipPlaywright) {
        Write-Warn "package.json was not found. Skipping Playwright install."
    }

    if (-not $SkipWhatsAppBridge -and (Test-Path $WhatsAppBridgePackageJson)) {
        Invoke-Step -FilePath $npmPath -Arguments @("install", "--silent") -WorkingDirectory $WhatsAppBridgeDir
        Write-Success "WhatsApp bridge dependencies installed"
    }
}

function Show-NextSteps {
    $packageSpec = if ($BaseOnly) { "." } else { ".[all,dev]" }
    Write-Host ""
    Write-Host "Windows bootstrap complete for this Hermes checkout." -ForegroundColor Green
    Write-Host ""
    Write-Host "Repo: $RepoRoot" -ForegroundColor Gray
    Write-Host "Python extras: $packageSpec" -ForegroundColor Gray
    Write-Host ""
    Write-Host "Next steps:" -ForegroundColor Cyan
    Write-Host "  PowerShell: .\$VenvDir\Scripts\Activate.ps1" -ForegroundColor Gray
    Write-Host "  cmd.exe:    $VenvDir\Scripts\activate.bat" -ForegroundColor Gray
    Write-Host "  Start CLI:  hermes" -ForegroundColor Gray
    Write-Host "  Setup:      hermes setup" -ForegroundColor Gray
    Write-Host ""
}

Ensure-RepoRoot
Write-Info "Bootstrapping Hermes for native Windows development from the current checkout."
Write-Info "Repo root: $RepoRoot"

$uvPath = Ensure-Uv
Ensure-Python -UvPath $uvPath

Invoke-Step -FilePath $uvPath -Arguments @("venv", $VenvDir, "--python", $PythonVersion)

$packageSpec = if ($BaseOnly) { "." } else { ".[all,dev]" }
Invoke-Step -FilePath $uvPath -Arguments @("pip", "install", "--python", $VenvPython, "-U", "pip")
Invoke-Step -FilePath $uvPath -Arguments @("pip", "install", "--python", $VenvPython, "-e", $packageSpec)
Write-Success "Python dependencies installed"

Maybe-InstallNodeDeps
Show-NextSteps
