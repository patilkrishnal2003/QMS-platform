# Runs all tests: closed-loop guard, each Python service (own venv), and web.
. (Join-Path $PSScriptRoot 'common.ps1')
$failed = @()

function Ensure-Venv($name) {
    $dir = Join-Path $Root $name
    $py = Join-Path $dir '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $py)) {
        & python -m venv (Join-Path $dir '.venv')
        & $py -m pip install --quiet -r (Join-Path $dir 'requirements.txt')
    }
    return $py
}

foreach ($name in $Services.Keys) {
    $py = Ensure-Venv $name
    Push-Location -LiteralPath (Join-Path $Root $name)
    & $py -m pytest -q
    if ($LASTEXITCODE -ne 0) { $failed += $name }
    Pop-Location
}

# Root-level closed-loop guard (stdlib only) - run with the gateway venv's pytest.
$gpy = Join-Path $Root 'gateway\.venv\Scripts\python.exe'
Push-Location -LiteralPath $Root
& $gpy -m pytest -q --rootdir . tests
if ($LASTEXITCODE -ne 0) { $failed += 'closed-loop' }
Pop-Location

$webDir = Join-Path $Root 'web'
if (-not (Test-Path -LiteralPath (Join-Path $webDir 'node_modules'))) {
    Push-Location -LiteralPath $webDir; npm install --no-audit --no-fund; Pop-Location
}
Push-Location -LiteralPath $webDir
npm test
if ($LASTEXITCODE -ne 0) { $failed += 'web' }
Pop-Location

if ($failed.Count) { Write-Host "FAILED: $($failed -join ', ')"; exit 1 }
Write-Host 'ALL TESTS PASSED'
