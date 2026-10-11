# Content Studio — self-hosting setup for Windows 10/11 (Latitude 5420).
# Run in an ELEVATED PowerShell:  Set-ExecutionPolicy -Scope Process Bypass; .\setup-windows.ps1
# Re-runnable (updates code + deps). Contains no secrets: put your .env at $AppDir\backend\.env yourself.
# What it changes on this PC: installs Python/Git/FFmpeg/Node/Tailscale via winget, registers a startup task
# "ContentStudio", and (unless -KeepPowerSettings) stops the PC from sleeping while plugged in / lid closed.
param(
  [string]$AppDir = "$env:USERPROFILE\content-studio",
  [string]$RepoBe = "https://github.com/KahaanKapasi/contentstudio-be.git",
  [string]$RepoFe = "https://github.com/KahaanKapasi/studiodecontent.git",
  [int]$Port = 8000,
  [switch]$KeepPowerSettings
)
$ErrorActionPreference = "Stop"

function Have($cmd) { [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }
function WingetInstall($id) { winget install --id $id -e --silent --accept-package-agreements --accept-source-agreements }

Write-Host "==> Tools (winget)"
if (-not (Have winget)) { throw "winget not found. Update 'App Installer' from the Microsoft Store, then re-run." }
if (-not (Have python)) { WingetInstall "Python.Python.3.12" }
if (-not (Have git))    { WingetInstall "Git.Git" }
if (-not (Have ffmpeg)) { WingetInstall "Gyan.FFmpeg" }
if (-not (Have node))   { WingetInstall "OpenJS.NodeJS.LTS" }
if (-not (Have tailscale)) { WingetInstall "Tailscale.Tailscale" }
# winget installs update PATH for NEW shells only
$env:Path = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [Environment]::GetEnvironmentVariable("Path","User")

Write-Host "==> Code ($AppDir)"
New-Item -ItemType Directory -Force $AppDir | Out-Null
foreach ($pair in @(@("backend",$RepoBe), @("frontend",$RepoFe))) {
  $dir = Join-Path $AppDir $pair[0]
  if (Test-Path (Join-Path $dir ".git")) { git -C $dir pull --ff-only } else { git clone $pair[1] $dir }
}

Write-Host "==> Python environment"
Set-Location "$AppDir\backend"
python -m venv venv
& .\venv\Scripts\python.exe -m pip install --upgrade pip
& .\venv\Scripts\python.exe -m pip install -r requirements.txt

if (-not (Test-Path ".env")) {
  Write-Host "!! $AppDir\backend\.env is missing. Copy your .env there, then re-run this script." -ForegroundColor Yellow
  exit 1
}

Write-Host "==> Frontend build"
Set-Location "$AppDir\frontend"
npm ci
$env:VITE_API_BASE_URL = "same-origin"
npm run build
$envFile = "$AppDir\backend\.env"
if (-not (Select-String -Path $envFile -Pattern "^FRONTEND_DIST=" -Quiet)) {
  Add-Content $envFile "`nFRONTEND_DIST=$AppDir\frontend\dist"
}

Write-Host "==> Startup task 'ContentStudio'"
$exe = "$AppDir\backend\venv\Scripts\uvicorn.exe"
$action = New-ScheduledTaskAction -Execute $exe -Argument "app.main:app --host 127.0.0.1 --port $Port" -WorkingDirectory "$AppDir\backend"
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
  -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
# S4U = runs at boot without a logged-in session and without storing your password
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U -RunLevel Limited
Register-ScheduledTask -TaskName "ContentStudio" -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
Stop-ScheduledTask -TaskName "ContentStudio" -ErrorAction SilentlyContinue
Start-ScheduledTask -TaskName "ContentStudio"

if (-not $KeepPowerSettings) {
  Write-Host "==> Power: never sleep on AC, ignore lid close on AC (re-run with -KeepPowerSettings to skip)"
  powercfg /change standby-timeout-ac 0
  powercfg /change hibernate-timeout-ac 0
  powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0
  powercfg /setactive SCHEME_CURRENT
}

Start-Sleep -Seconds 6
try { (Invoke-WebRequest "http://127.0.0.1:$Port/api/health" -UseBasicParsing).Content; Write-Host "API is up on 127.0.0.1:$Port" -ForegroundColor Green }
catch { Write-Host "API did not answer yet. Check: Get-ScheduledTaskInfo ContentStudio ; run uvicorn manually from $AppDir\backend to see errors." -ForegroundColor Yellow }

Write-Host @"

Next (once): make it reachable from your phone/Mac WITHOUT exposing it to the internet:
  tailscale up                      # sign in with the same account on your phone + Mac
  tailscale serve --bg $Port        # private HTTPS URL inside your tailnet
  tailscale serve status            # shows https://<this-pc>.<tailnet>.ts.net
Open that URL on any device with Tailscale. Update later by re-running this script.
"@
