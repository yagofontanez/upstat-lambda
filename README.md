# upstat-probe-lambda

UpStat's external probe running on **AWS Lambda** — replaces the older
Cloudflare Workers probes to give us **guaranteed geographic location** per
AWS region.

Each region is its own SAM stack: a Python 3.12 Lambda triggered every minute
by EventBridge that fetches monitor jobs from the UpStat backend, runs the
checks, and reports the results.

- `handler.py` — probe logic (same contract as the worker: fetch jobs → check → report)
- `template.yaml` — SAM: `python3.12` Lambda + EventBridge 1-minute schedule
- `requirements.txt` — single dependency: `httpx` (async HTTP client)

## Prerequisites

- AWS CLI configured (`aws configure`) with valid credentials
- SAM CLI installed (`sam --version`)
- The `PROBE_SECRET` (same value as `PROBE_SECRET` on the backend's Render env
  — copy it from there, do not commit it)

## Deploy

Each AWS region is an independent stack. Build once, deploy per region:

```bash
cd upstat-probe-lambda
sam build

# Europe — Ireland
sam deploy \
  --region eu-west-1 \
  --stack-name upstat-probe-eu \
  --resolve-s3 \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides ProbeRegion=eu-west ProbeSecret=<SECRET>

# South America — São Paulo
sam deploy \
  --region sa-east-1 \
  --stack-name upstat-probe-br \
  --resolve-s3 \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides ProbeRegion=sa-east ProbeSecret=<SECRET>

# Asia Pacific — Singapore
sam deploy \
  --region ap-southeast-1 \
  --stack-name upstat-probe-sg \
  --resolve-s3 \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides ProbeRegion=ap-southeast ProbeSecret=<SECRET>

# US West — Oregon
sam deploy \
  --region us-west-2 \
  --stack-name upstat-probe-usw \
  --resolve-s3 \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides ProbeRegion=us-west ProbeSecret=<SECRET>
```

> The `region_code` strings (`ap-southeast`, `us-west`) must exist in the
> backend's `regions` catalog. They ship `is_active=false` — flip them to
> `true` only **after** the stack is confirmed reporting (see "Go live" below).

> `BackendUrl` defaults to `https://api.upstat.online/api`. To override, add
> `BackendUrl=...` to `--parameter-overrides`.

## Verify

Tail the logs (you should see `checked=N errors=0` every minute):

```bash
sam logs --stack-name upstat-probe-eu --region eu-west-1 --tail
```

To confirm geographic accuracy, the AWS region itself is the guarantee — no
need to measure colo like we did with Cloudflare.

## Go live (activate a new region)

New PoPs (`ap-southeast`, `us-west`) are seeded `is_active=false`, so they are
invisible in the app until you flip them. Once the stack is confirmed reporting
(`checked=N errors=0` in the logs and pings landing with the right
`region_code`), activate it in the backend Postgres:

```sql
UPDATE regions SET is_active = true WHERE code IN ('ap-southeast', 'us-west');
```

Only after this do they appear in the region selector and on the latency map.
To pull a region back offline, set `is_active = false` again — existing pings
stay, but it stops being selectable.

## ⚠️ IMPORTANT — turn off the Cloudflare Workers when migrating

The old workers (`upstat-probe-eu` / `upstat-probe-br` on Cloudflare) write
pings for the **same regions** (`eu-west` / `sa-east`), but originating from
Singapore. If both run at the same time, they will **duplicate and conflict**
data. Once the Lambda is confirmed reporting, **delete the Cloudflare
workers**:

```bash
cd ../upstat-probe-worker
npx wrangler delete --config wrangler.eu.toml
npx wrangler delete --config wrangler.br.toml
```

The `us-east` region keeps using the local probe on Render — nothing changes
there.
