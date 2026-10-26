param(
    [string]$Server = "44.193.7.112",
    [string]$KeyPath = (Join-Path $env:USERPROFILE ".ssh\LightsailDefaultKey-us-east-1.pem")
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$pwaRoot = Join-Path $repoRoot "knowledge-web-pwa"
$stamp = Get-Date -Format "yyyyMMddHHmmss"
$tempRoot = Join-Path $env:TEMP "atlasai-release-$stamp"
$payloadRoot = Join-Path $tempRoot "payload"
$repoArchive = Join-Path $tempRoot "source.zip"
$releaseArchive = Join-Path $tempRoot "release.zip"
$remoteArchive = "/home/ubuntu/atlasai-deploy/release-$stamp.zip"

function Invoke-Native {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Program failed with exit code $LASTEXITCODE" }
}

try {
    if (!(Test-Path $KeyPath)) { throw "SSH key not found: $KeyPath" }
    if (!(Test-Path (Join-Path $pwaRoot "web-config.json"))) {
        throw "Missing knowledge-web-pwa/web-config.json; configure the public Auth0 SPA values first."
    }

    $branch = (git -C $repoRoot branch --show-current).Trim()
    if ($branch -ne "main") { throw "Deploy from main; current branch is '$branch'." }
    if ((git -C $repoRoot status --porcelain)) { throw "Commit or discard local changes before deployment." }
    $head = (git -C $repoRoot rev-parse HEAD).Trim()
    $remoteHead = (git -C $repoRoot ls-remote origin refs/heads/main).Split("`t")[0]
    if (!$remoteHead -or $remoteHead -ne $head) { throw "Push the current main commit to GitHub before deployment." }

    Push-Location $pwaRoot
    try {
        Invoke-Native "flutter" @("pub", "get")
        Invoke-Native "cmd.exe" @("/d", "/c", "npm ci")
        Invoke-Native "flutter" @("build", "web", "--release", "--no-web-resources-cdn", "--dart-define-from-file=web-config.json")
        Invoke-Native "cmd.exe" @("/d", "/c", "npm run pwa")
    }
    finally { Pop-Location }

    New-Item -ItemType Directory -Path $payloadRoot -Force | Out-Null
    Invoke-Native "git" @("-C", $repoRoot, "archive", "--format=zip", "--output=$repoArchive", "HEAD", "knowledge-management-demo")
    Expand-Archive -LiteralPath $repoArchive -DestinationPath $payloadRoot
    $staticRoot = Join-Path $payloadRoot "knowledge-web-pwa\build\web"
    New-Item -ItemType Directory -Path $staticRoot -Force | Out-Null
    Copy-Item -Path (Join-Path $pwaRoot "build\web\*") -Destination $staticRoot -Recurse -Force
    Compress-Archive -Path (Join-Path $payloadRoot "*") -DestinationPath $releaseArchive -CompressionLevel Optimal

    $ssh = (Get-Command ssh.exe -ErrorAction Stop).Source
    $scp = (Get-Command scp.exe -ErrorAction Stop).Source
    Invoke-Native $ssh @("-i", $KeyPath, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "ubuntu@$Server", "mkdir -p /home/ubuntu/atlasai-deploy")
    Invoke-Native $scp @("-i", $KeyPath, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", $releaseArchive, "ubuntu@${Server}:$remoteArchive")
    Invoke-Native $ssh @("-i", $KeyPath, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "ubuntu@$Server", "sudo /usr/local/sbin/atlasai-apply-release $remoteArchive")
    Write-Host "Deployment succeeded: commit $head"
}
finally {
    if (Test-Path $tempRoot) { Remove-Item -LiteralPath $tempRoot -Recurse -Force }
}
