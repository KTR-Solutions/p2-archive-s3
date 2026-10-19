# Install the P2 archive S3 service as a WinSW service, same layout as the gateway connector.
param(
    [Parameter(Mandatory = $true)][string]$ShareRoot,
    [Parameter(Mandatory = $true)][string]$AccessKey,
    [Parameter(Mandatory = $true)][string]$SecretKey,
    [string]$InstallDir = "$env:ProgramData\higent\p2-archive-s3",
    [string]$AppDir = "C:\UNI-Programme\UNI-ExcellentP2",
    [int]$Port = 9000,
    [string]$Region = "us-east-1",
    [string]$ListenHost = "127.0.0.1"
)

$ErrorActionPreference = "Stop"

$pwshCmd = Get-Command pwsh -ErrorAction SilentlyContinue
if (-not $pwshCmd) {
    throw "PowerShell 7 (pwsh) is required. Windows PowerShell 5.1 cannot load the .NET 8 archive assemblies."
}

New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
foreach ($name in @("server.py", "archive.py", "sigv4.py", "worker.ps1", "requirements.txt")) {
    Copy-Item -Force (Join-Path $PSScriptRoot $name) (Join-Path $InstallDir $name)
}

$venv = Join-Path $InstallDir ".venv"
if (-not (Test-Path (Join-Path $venv "Scripts\python.exe"))) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3 -m venv $venv
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        & python -m venv $venv
    } else {
        throw "Python 3 is required to create the service virtualenv."
    }
}

function Escape-Xml([string]$Value) {
    return ($Value -replace '&', '&amp;' -replace '<', '&lt;' -replace '>', '&gt;' -replace '"', '&quot;')
}

$pythonExe = Join-Path $venv "Scripts\python.exe"
$serverPy = Join-Path $InstallDir "server.py"
$xmlPath = Join-Path $InstallDir "HigentP2Archive.xml"
$xml = @"
<service>
  <id>HigentP2Archive</id>
  <name>Higent P2 Archive</name>
  <description>S3 endpoint for Excellent archive files. GetObject and PutObject call ReadEncodedFile and WriteEncodedFile.</description>
  <executable>$(Escape-Xml $pythonExe)</executable>
  <arguments>"$(Escape-Xml $serverPy)"</arguments>
  <workingdirectory>$(Escape-Xml $InstallDir)</workingdirectory>
  <env name="ARCHIVE_SHARE_ROOT" value="$(Escape-Xml $ShareRoot)"/>
  <env name="ARCHIVE_APP_DIR" value="$(Escape-Xml $AppDir)"/>
  <env name="ARCHIVE_S3_ACCESS_KEY" value="$(Escape-Xml $AccessKey)"/>
  <env name="ARCHIVE_S3_SECRET_KEY" value="$(Escape-Xml $SecretKey)"/>
  <env name="ARCHIVE_S3_REGION" value="$(Escape-Xml $Region)"/>
  <env name="ARCHIVE_LISTEN_HOST" value="$(Escape-Xml $ListenHost)"/>
  <env name="ARCHIVE_LISTEN_PORT" value="$Port"/>
  <env name="ARCHIVE_PWSH" value="$(Escape-Xml $pwshCmd.Source)"/>
  <env name="PYTHONUTF8" value="1"/>
  <env name="PYTHONIOENCODING" value="utf-8"/>
  <startmode>Automatic</startmode>
  <delayedAutoStart>true</delayedAutoStart>
  <onfailure action="restart" delay="10 sec"/>
  <onfailure action="restart" delay="30 sec"/>
  <onfailure action="restart" delay="60 sec"/>
  <resetfailure>1 hour</resetfailure>
  <logpath>$(Escape-Xml (Join-Path $InstallDir "logs"))</logpath>
  <log mode="roll-by-size">
    <sizeThreshold>10485760</sizeThreshold>
    <keepFiles>5</keepFiles>
  </log>
  <stoptimeout>15 sec</stoptimeout>
</service>
"@
Set-Content -Path $xmlPath -Value $xml -Encoding UTF8
New-Item -ItemType Directory -Force -Path (Join-Path $InstallDir "logs") | Out-Null

Write-Host "Installed files in $InstallDir"
Write-Host "Service definition: $xmlPath"
Write-Host ""
Write-Host "Copy the WinSW executable you used for the gateway connector to:"
Write-Host "  $InstallDir\HigentP2Archive.exe"
Write-Host "Then, from an elevated PowerShell:"
Write-Host "  $InstallDir\HigentP2Archive.exe install"
Write-Host "  $InstallDir\HigentP2Archive.exe start"
Write-Host ""
Write-Host "On the Gateway allowlist, add the address this host dials, for example 127.0.0.1:$Port"
Write-Host "In Higent, set the S3 endpoint to http://127.0.0.1:$Port and select that Gateway."
