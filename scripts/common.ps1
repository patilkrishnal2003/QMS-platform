$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$RunDir = Join-Path $Root '.run'

# name -> default port (overridable from .env / .env.example)
$Services = [ordered]@{
    'gateway'   = @{ Port = 8000; Var = 'GATEWAY_PORT' }
    'library'   = @{ Port = 8001; Var = 'LIBRARY_PORT' }
    'knowledge' = @{ Port = 8002; Var = 'KNOWLEDGE_PORT' }
    'mock-eqms' = @{ Port = 8003; Var = 'MOCK_EQMS_PORT' }
    'workflow'  = @{ Port = 8004; Var = 'WORKFLOW_PORT' }
}
$WebPort = 5173

function Get-EnvMap {
    $map = @{}
    foreach ($f in @('.env.example', '.env')) {
        $p = Join-Path $Root $f
        if (Test-Path -LiteralPath $p) {
            foreach ($line in Get-Content -LiteralPath $p) {
                if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$') { $map[$Matches[1]] = $Matches[2].Trim() }
            }
        }
    }
    return $map
}
