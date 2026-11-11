# Stops every process tree recorded in .run\pids.json. Needs nothing but that file.
. (Join-Path $PSScriptRoot 'common.ps1')
$file = Join-Path $RunDir 'pids.json'
if (-not (Test-Path -LiteralPath $file)) { Write-Host 'No .run\pids.json - nothing to stop.'; exit 0 }
$rec = Get-Content -LiteralPath $file -Raw | ConvertFrom-Json
foreach ($p in @($rec.pids)) {
    & taskkill.exe /PID $p /T /F *>$null   # /T also kills uvicorn/node children
    Write-Host "stopped pid $p"
}
Remove-Item -LiteralPath $file -Force
Write-Host 'Services stopped.'
