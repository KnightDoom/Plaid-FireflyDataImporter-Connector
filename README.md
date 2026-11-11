# Plaid → Firefly III sync

This repository contains a small, scheduled Python worker that downloads
posted transactions from Plaid and imports them into Firefly III. It supports
two destinations:

- `direct` (default): create transactions through the Firefly III API.
- `importer`: render bank/account-specific CSV profiles and upload them to
  Firefly III Data Importer through `/autoupload`.

Kubernetes and Flux manifests are intentionally not part of the runnable
repository yet. The future deployment reference is preserved under
[`docs/kubernetes-reference/`](docs/kubernetes-reference/).

## Safety model

- Plaid `/transactions/sync` cursors are persisted in SQLite.
- State is stored at `/config/state.sqlite` by default.
- `/tmp` is the only temporary-write location inside the container.
- The image runs as non-root UID/GID `10001`.
- The worker defaults to `DRY_RUN=true`; set it to `false` only after checking
  the sandbox results.
- Pending transactions are skipped, and modified/removed imported transactions
  are flagged for manual reconciliation.
- Plaid access tokens, mappings, importer profiles, and Firefly credentials
  belong in runtime environment variables or a secret manager, never in Git.

## Local tests

```sh
python -m pip install -r requirements.txt
python -m unittest discover --start-directory tests --verbose
```

## Build and run with Docker Compose

Copy the example configuration, fill in the real values, and keep `DRY_RUN`
enabled for the first run:

```sh
cp .env.example .env
docker compose build
docker compose run --rm plaid-firefly-sync
```

The Compose service uses a named volume mounted at `/config`, so normal
rootless Docker users do not need to prepare a host directory or grant root
permissions. The volume contains only the SQLite sync state. The container is
read-only apart from `/config` and the 64 MiB `/tmp` tmpfs.

To run it weekly, invoke the one-shot service from the host's scheduler:

```cron
0 3 * * 0 cd /path/to/repository && docker compose run --rm plaid-firefly-sync
```

For a host-local bind mount instead of the named volume, replace the volume
with `./config:/config` and ensure that the host directory is writable by UID
`10001`.

## Configuration

The required direct-mode variables are `PLAID_CLIENT_ID`, `PLAID_SECRET`,
`PLAID_ITEMS_JSON`, `FIREFLY_URL`, and `FIREFLY_TOKEN`. `PLAID_ITEMS_JSON`
contains one or more existing Plaid Item access tokens and explicit mappings
from Plaid account IDs to Firefly account IDs. Use `PLAID_ENV=sandbox` while
testing.

Importer mode additionally requires `IMPORTER_URL`,
`IMPORTER_AUTO_IMPORT_SECRET`, and a complete `IMPORTER_PROFILES_JSON` mapping.
Each profile must contain the exported Data Importer JSON and exactly one CSV
`external_id` column. Profiles can differ for every bank and account type.

See [`.env.example`](.env.example) for a non-secret template. Do not commit
`.env`, access tokens, personal access tokens, importer secrets, or raw account
mappings.

## GHCR publishing

The workflow in
[`.github/workflows/plaid-firefly-image.yaml`](.github/workflows/plaid-firefly-image.yaml)
runs unit tests and builds the lean image from the repository root. Pull
requests build without publishing. A push to `main`, a manual run, or the
weekly scheduled run creates a UTC ISO-calendar Git tag and publishes to:

```text
ghcr.io/<owner>/<repository>
```

Release versions use `YYYY.WW.D`, where `WW` is the ISO week and `D` is the ISO
weekday (`1` through `7`). If another release already exists on that same day,
the workflow appends `.1`, `.2`, and so on. For example: `2026.46.3`, then
`2026.46.3.1`.

Published images receive both the calendar version and an immutable
`sha-<commit>` tag. Release builds also update `latest`; pin the SHA tag or
digest in scheduled deployments when reproducibility matters.

## Scope boundary

This repository does not implement Plaid Link onboarding, token exchange,
automatic bank mapping, or Kubernetes reconciliation. Those are separate
future concerns. The current scope is a tested, non-root container that can be
run as a weekly one-shot job by Docker Compose or a later Kubernetes CronJob.
