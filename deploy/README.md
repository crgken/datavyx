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

## Candidate OpenAI migration

Use `openai-model.env.example` to create `openai-model.env` in this directory.
The candidate file is ignored by Git. Put the OpenAI API key in `LLM_API_KEY`.
The selected model is `gpt-6-luna`; `LLM_REASONING_EFFORT=none` is required for
its function calling through the current Chat Completions integration.
See the [official model documentation](https://developers.openai.com/api/docs/models/gpt-6-luna).
Existing embedding credentials and endpoints are independent of this LLM change.

Validate the candidate without changing the running service:

```bash
cd knowledge-management-demo
.venv/bin/python scripts/verify_openai.py --candidate-env ../deploy/openai-model.env --connection-only
.venv/bin/python scripts/verify_openai.py --candidate-env ../deploy/openai-model.env
```

The full check requires disposable Neo4j instances on loopback ports 17694 and
17696. It deliberately does not read production database settings. It covers real
model calls, mobile request polling, automatic recording, object browsing,
idempotent resubmission and authorization before replacing existing values. It does not establish that browser/Auth0
login or background semantic indexing has passed. Keep acceptance reports in the
ignored `knowledge-management-demo/artifacts/` directory. Use `--active-model-env`
when the test needs existing embedding configuration, without copying its LLM
settings into the candidate.

Commit and push the implementation through Git before deploying it. Run the
connection check from AWS as well as the isolated backend check. After both pass,
merge the tested commit into `main`, push it, and use `deploy-lightsail.ps1`.
The deployment script intentionally preserves the active `model.env`: changing
the running provider requires a separate, backed-up update of its three `LLM_*`
fields plus `LLM_REASONING_EFFORT`, followed by an API restart. Retain the previous
model configuration for rollback and verify the protected API after restart.

Failure logs contain stages, exception types, HTTP statuses, hashed request
references, elapsed time and stack locations. They omit exception text, input
messages, request bodies and credentials.

For a retry investigation, temporarily set `KD_REQUEST_DIAGNOSTICS=1` in the
backend `.env` and restart the API. `mobile_request_diagnostic` records the
validated operation, presence/value of the retry flag, returned task status and
hashed request/conversation identifiers. It excludes input text and credentials.
Set the flag back to `0` and restart after collecting the reproduction. Run
`scripts/verify_mobile_retry.py` against the dedicated loopback test database on
port 17694 to verify failure, explicit retry, successful replay and payload
conflict behavior without real model calls.

## Batched index outbox verification

Index enqueueing prefetches the affected records, facts, source links and proposals,
then writes both index lanes with one `UNWIND` statement in the canonical mutation
transaction. The transaction timeout remains 30 seconds. On the dedicated local
test database, run `scripts/verify_index_batch.py` to compare batched snapshots and
hashes with single-record snapshots and verify confirmation, withdrawal, replay,
incoming relationships and atomic rollback.

Before deploying an index-storage change, unpack the committed candidate into an
independent server staging directory and run its
`scripts/verify_pending_save_rollback.py --env /path/to/backend/.env` with the installed
runtime. Every transaction in that verifier is rolled back. It uses the real
workspace permissions, checks that canonical/outbox counts and revision remain
unchanged, and requires the save to finish below 25 seconds. When the diagnostic
draft has expired, `--refresh-expiry-in-rollback` changes expiry only inside the
rolled-back transaction. It never renews the production draft persistently.

## Automatic recording policy version 2

Ordinary additions are effective immediately. Estimated assertions retain their
qualifiers and do not enter precise statistics. Identity ambiguity is clarified
before recording; replacements, withdrawals, archival and classification changes
require explicit authorization. `confirmed`/`review_status` are compatibility and
eligibility fields, not evidence of human review. `recorded_by`, `recorded_at` and
mutation `authorization_mode` capture the actual action.

Run `scripts/verify_automatic_recording.py` on disposable Neo4j port 17694.
For a committed candidate staged independently on the server:

```bash
python scripts/migrate_recording_policy.py --env /path/to/backend/.env --report /protected/path/preview.json
python scripts/migrate_recording_policy.py --env /path/to/backend/.env --rollback-check --report /protected/path/rollback.json
```

Before applying: quiesce requests, stop the API, and back up both Neo4j data
volumes with the database containers stopped. Preserve the current source commit
and protected environment/model files separately. Deploy the clean, pushed main
commit with `KD_RECORDING_MAINTENANCE=1`; reads remain available. Run the migration
with `--apply`, verify effective counts, source links and receipts, then remove
maintenance mode and restart. Do not replay source chats. Safe stored pending
records activate with original IDs and provenance; risky records become isolated
unexecuted operation drafts, old approvals are revoked, and index heads refresh.
The migration refuses missing ownership/source context and cross-batch staging
dependencies that require resolution. Reports contain counts and opaque refs.

The migration is idempotent per dataset and transactional. Before reopening
writes, a failed cutover can restore the backed-up volumes and previous committed
release. After new writes are accepted, use a forward fix or reconcile those
writes before restoring an older backup.
