param([string]$Python = '.venv-win\Scripts\python.exe', [string]$ModelEnv = '')
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (!(Test-Path -LiteralPath '.env')) { Copy-Item -LiteralPath '.env.example' -Destination '.env' }
if (!(Test-Path -LiteralPath $Python)) {
    python -m venv .venv-win
    if ($LASTEXITCODE -ne 0) { throw 'Could not create virtual environment.' }
    & '.venv-win\Scripts\python.exe' -m pip install --no-compile -r requirements.lock.txt
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
    $Python = '.venv-win\Scripts\python.exe'
}
docker compose up -d --wait --wait-timeout 150
if ($LASTEXITCODE -ne 0) { throw 'Demo Neo4j startup failed. Docker Compose must be available in this shell.' }
if ($ModelEnv) { & $Python -m demo --model-env $ModelEnv } else { & $Python -m demo }
exit $LASTEXITCODE
