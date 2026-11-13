# Deterministic knowledge MCP backend

Development branch: `codex/mcp-neo4j`. Current unit status is in
[the progress record](../../docs/50-运行记录/MCP改造开发进度-2026-11-12.md).
The approved mutation extension and its actual status are in
[M00–M09 progress](../../docs/50-运行记录/MCP修改删除开发进度-2026-11-12.md).
Eleven public tools include versioned corrections, recoverable deletion/restoration
and authorized history. Permanent erasure is deferred: it is neither discoverable
nor callable, even with a cached tool name or an old purge scope. Historical erasure
guards and internal recovery code remain. There is no approval tool.
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

After loading the explicit MCP settings, apply schema changes explicitly with
`python -m mcp_backend --migrate`, then serve with `python -m mcp_backend`.
The HTTP service only listens on loopback behind the HTTPS gateway; it does not
automatically migrate or start the legacy API. `/health/live` and `/health/ready`
are distinct from authenticated MCP functionality. Actual unit acceptance is
recorded in the progress file; local protocol tests do not prove ChatGPT access.

Real database tests use the existing isolated loopback port 17694 only. Inside
WSL, verify/start that container and keep WSL running while invoking Windows tests:

```bash
cd /mnt/e/porject/AtlasAI/knowledge-management-demo
python3 scripts/start_mcp_test_db.py
.venv-mcp/Scripts/python.exe -m unittest discover -s tests_mcp/integration -v
.venv-mcp/Scripts/python.exe -m unittest discover -s tests_mcp/protocol -v
```

The test repository refuses production 17693 and the legacy semantic port 17695.
Fixtures contain synthetic data only. Cleanup is bound to the test instance and
the exact generated `mcp-test-*` datasets.

## Authentication integration requirements

Reuse the existing Auth0 tenant/subjects, not its SPA credentials or API keys.
Configure a dedicated MCP API resource and `knowledge:read` / `knowledge:record`
plus update/delete/restore/history/bulk permissions (each with `knowledge:`);
the canonical resource URL and intended token audience must match
in production. Allowlist the actual ChatGPT OAuth client, and copy its actual
callback URI rather than inventing a fixed one.

Auth0 documents the **Resource Parameter Compatibility Profile** tenant setting
for MCP clients which send RFC 8707 `resource` rather than Auth0's `audience`:
[Auth0 guidance](https://support.auth0.com/center/s/article/mcp-audience-error-with-auth0).
The owner confirmed this setting is enabled on 2026-11-12. Do not change the tenant's
global default audience to work around it, disable audience checks, or accept
userinfo tokens as knowledge API tokens.

The package verifies RS256 signatures, issuer, audience, expiry, client allowlist,
scope and organization; each operation rechecks current workspace membership and
known token revocations. `workspace_id` is only an authorized selector, never a
permission grant or persistent server-side session switch.

Offline signature and database membership tests do not prove the actual Auth0
callback or real ChatGPT connection. Those remain separate U11/U16 acceptance.

## Self-service OAuth clients and personal spaces

`MCP_AUTH_CLIENT_POLICY=allowlist` is the backward-compatible default and requires
explicit comma-separated `MCP_AUTH_CLIENT_IDS`. To admit all clients authorized by
the configured issuer for this API, explicitly set:

```text
MCP_AUTH_CLIENT_POLICY=authenticated
```

In this mode `MCP_AUTH_CLIENT_IDS` may be empty; any IDs left in that setting do
not restrict admission. `*` is rejected in either mode. Unknown policy values fail
configuration validation. This policy accepts both pre-registered and dynamically
registered OAuth clients; it does not identify a client as Claude or certify its
software. It trusts only the configured issuer's signed API access token.
Signature, issuer, audience/resource, expiry, user subject, organization (when
configured), delegated scopes, revocation and current workspace-role checks
remain mandatory. Machine subjects and client-credentials grants are rejected.
No Auth0 Management API credentials or shared client secret are needed by this
backend or distributed to users.

Auth0 must separately enable DCR, make the intended login connection available
to third-party apps, and grant only the selected user-delegated API scopes to
new third-party clients. Client-credentials/M2M access is not needed. Preserve the
existing issuer, resource audience and Resource Parameter Compatibility Profile;
do not change the tenant default audience as a workaround. For older tenants,
check strict DCR mode and default third-party client grants before registering
new clients. These are tenant-wide identity configuration changes, not performed
by changing this backend environment variable. See the
[Auth0 DCR setup](https://auth0.com/blog/secure-csharp-mcp-server-with-auth0/),
[API access policies](https://auth0.com/blog/developers-guide-api-access-policies-auth0/)
and [strict-mode guidance](https://support.auth0.com/center/s/article/enable-strict-dcr-mode-for-tenant).

Users add the MCP URL in their client's connector settings and complete their own
OAuth login/consent. The client handles registration and credentials; users need
no shared secret. Claude supports DCR and Streamable HTTP; see
[Claude's connector guide](https://support.anthropic.com/en/articles/11503834-building-custom-connectors-via-remote-mcp-servers).

On the first authorized tool call without `workspace_id`, a user with no existing
workspace membership receives a personal workspace and owner membership in one
transaction. Its ID derives from the trusted `(issuer, sub)`, not email, client ID
or a caller-provided identity. Repeated/concurrent first calls reuse the space;
the same Auth0 identity on different clients uses the same space. New users do
not join existing shared spaces. Explicit workspace IDs still require membership;
disabled membership cannot be reenabled by reconnecting. Existing memberships
are preserved. No schema migration or frontend build is needed for this change.

Enable this policy in an isolated candidate first and verify two real OAuth users,
refresh/reconnect and cross-space rejection before a separately authorized
production rollout. Local signed-token tests cannot prove Auth0 DCR or client UI
compatibility. To revert client admission, restore `allowlist` with the approved
IDs and restart; preserve all newly created spaces and records.

## Resource and diagnostic boundaries

Input JSON is limited to 256 KiB, individual structured results to 512 KiB,
pages to 100 rows, full-space object/fact/source directories to the configured
bound (default 10,000), and source scan text to 16 Mi characters. Oversized
results fail explicitly; evidence and aggregates are never silently truncated.
Eight bounded service workers use Neo4j transactions limited to 20 seconds;
the production HTTP listener caps concurrency at 64. These are first-release
limits, not a claim of unlimited graph scalability.

Tool diagnostics contain a generated trace ID, fixed tool name, error code,
duration and exception class, without source text, request bodies, tokens,
passwords or raw exception messages. Schema failures also expose at most eight
schema-owned field paths and error codes plus the expected schema version;
unknown input keys are redacted, and values/context/exception messages are omitted.
Access logs are disabled. Unexpected save
errors require receipt lookup; a reconnect must retain the request ID/body.
See [deterministic semantics](SEMANTICS.md) for population, conflict and unit rules.
