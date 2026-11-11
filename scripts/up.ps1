# Starts all services as detached processes (not PowerShell jobs), so they keep
# running after this shell closes. Launcher PIDs are recorded in .run\pids.json;
# down.ps1 kills each PID's whole process tree, using only that file.
. (Join-Path $PSScriptRoot 'common.ps1')
$envMap = Get-EnvMap
$LogDir = Join-Path $RunDir 'logs'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

if (Test-Path -LiteralPath (Join-Path $RunDir 'pids.json')) {
    Write-Host 'pids.json already exists - run scripts\down.ps1 first.'
    exit 1
}

$started = @()   # @{ name; pid; port }

function Start-Detached($name, $file, $argList, $dir, $port) {
    $p = Start-Process -FilePath $file -ArgumentList $argList -WorkingDirectory $dir `
        -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $LogDir "$name.out.log") `
        -RedirectStandardError (Join-Path $LogDir "$name.err.log")
    $script:started += @{ name = $name; pid = $p.Id; port = [int]$port }
    Write-Host "[$name] pid $($p.Id) -> http://127.0.0.1:$port"
}

foreach ($name in $Services.Keys) {
    $dir = Join-Path $Root $name
    $venv = Join-Path $dir '.venv'
    $py = Join-Path $venv 'Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $py)) {
        Write-Host "[$name] creating venv"
        & python -m venv $venv
        & $py -m pip install --quiet -r (Join-Path $dir 'requirements.txt')
    }
    $port = if ($envMap[$Services[$name].Var]) { $envMap[$Services[$name].Var] } else { $Services[$name].Port }
    Start-Detached $name $py @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', "$port") $dir $port
}

$webDir = Join-Path $Root 'web'
if (-not (Test-Path -LiteralPath (Join-Path $webDir 'node_modules'))) {
    Write-Host '[web] npm install'
    Push-Location -LiteralPath $webDir; npm install --no-audit --no-fund; Pop-Location
}
$webPort = if ($envMap['WEB_PORT']) { $envMap['WEB_PORT'] } else { $WebPort }
$npm = (Get-Command npm.cmd).Source
Start-Detached 'web' $npm @('run', 'dev', '--', '--host', '127.0.0.1', '--port', "$webPort") $webDir $webPort

# Record PIDs immediately (before readiness polling) so down.ps1 always works.
@{ pids = @($started | ForEach-Object { $_.pid }); services = @($started | ForEach-Object { @{ name = $_.name; pid = $_.pid; port = $_.port } }) } |
    ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $RunDir 'pids.json')

# Wait for each /health to answer.
$bad = 0
foreach ($s in $started) {
    $ok = $false
    foreach ($i in 1..30) {
        try { Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:$($s.port)/health" -TimeoutSec 2 | Out-Null; $ok = $true; break } catch { Start-Sleep -Milliseconds 500 }
    }
    if ($ok) { Write-Host "[$($s.name)] healthy" } else { Write-Host "[$($s.name)] NOT healthy - see .run\logs\$($s.name).err.log"; $bad++ }
}
if ($bad) { exit 1 }
Write-Host 'All services up. Stop with scripts\down.ps1'
