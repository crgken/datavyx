# Lightsail SSH deployment

The Windows PowerShell entry point builds the Flutter PWA locally, packages the committed backend source and generated web assets, uploads them over SSH, then asks the server helper to update the app and restart its API.

## One-time server setup

From the repository root, install the server helper:

```powershell
$key = "$env:USERPROFILE\.ssh\LightsailDefaultKey-us-east-1.pem"
ssh -i $key ubuntu@44.193.7.112 "mkdir -p /home/ubuntu/atlasai-deploy"
scp -i $key .\deploy\atlasai-apply-release.sh ubuntu@44.193.7.112:/home/ubuntu/atlasai-deploy/atlasai-apply-release.sh
ssh -i $key ubuntu@44.193.7.112 "sudo install -o root -g root -m 0755 /home/ubuntu/atlasai-deploy/atlasai-apply-release.sh /usr/local/sbin/atlasai-apply-release"
```

## Deploy

Install Flutter, Node.js, and npm locally; configure `knowledge-web-pwa/web-config.json`; commit and push the desired code to `main`. Then run:

```powershell
.\deploy\deploy-lightsail.ps1
```

The script requires a clean `main` branch matching `origin/main`. Server `.env`, `model.env`, the Python virtual environment, and Neo4j Docker volumes are kept in place. The Lightsail private key stays on the local computer and is never included in the repository or release archive.
