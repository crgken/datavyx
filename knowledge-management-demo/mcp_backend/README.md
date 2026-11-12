# Deterministic knowledge MCP backend

Development branch: `codex/mcp-neo4j`. Current unit status is in
[the progress record](../../docs/50-运行记录/MCP改造开发进度-2026-11-12.md).
This package never imports `demo` or initializes its Agent, approval or Graphiti workers.

## Environment

Use Python 3.12 and an isolated environment, then install only
`requirements-mcp.lock.txt`. Do not install the old demo requirements into it.
The lock contains a Windows marker for `pywin32`; Linux does not install it.

Provide `MCP_*` environment variables described by `../mcp-config.env.example`.
Real settings and signing secrets are ignored. No model keys are required or read.
Configuration validation does not connect to a database:

```powershell
.venv-mcp\Scripts\python.exe -m mcp_backend --check-config
.venv-mcp\Scripts\python.exe -m unittest discover -s tests_mcp/unit -v
```

The transport serving unit is not complete yet; a default run must not be
interpreted as a working server until U11 and its tests pass.

Real database tests use the existing isolated loopback port 17694 only. Inside
WSL, verify/start that container and keep WSL running while invoking Windows tests:

```bash
cd /mnt/e/porject/AtlasAI/knowledge-management-demo
python3 scripts/start_mcp_test_db.py
.venv-mcp/Scripts/python.exe -m unittest discover -s tests_mcp/integration -v
```

The test repository refuses production 17693 and the legacy semantic port 17695.
Fixtures contain synthetic data only. Cleanup is bound to the test instance and
the exact generated `mcp-test-*` datasets.

## Authentication integration requirements

Reuse the existing Auth0 tenant/subjects, not its SPA credentials or API keys.
Configure a dedicated MCP API resource and `knowledge:read` / `knowledge:record`
permissions; the canonical resource URL and intended token audience must match
in production. Allowlist the actual ChatGPT OAuth client, and copy its actual
callback URI rather than inventing a fixed one.

Auth0 documents the **Resource Parameter Compatibility Profile** tenant setting
for MCP clients which send RFC 8707 `resource` rather than Auth0's `audience`:
[Auth0 guidance](https://support.auth0.com/center/s/article/mcp-audience-error-with-auth0).
The owner has been asked to verify this setting. Do not change the tenant's
global default audience to work around it, disable audience checks, or accept
userinfo tokens as knowledge API tokens.

The package verifies RS256 signatures, issuer, audience, expiry, client allowlist,
scope and organization; each operation rechecks current workspace membership and
known token revocations. `workspace_id` is only an authorized selector, never a
permission grant or persistent server-side session switch.

Offline signature and database membership tests do not prove the actual Auth0
callback or real ChatGPT connection. Those remain separate U11/U16 acceptance.
