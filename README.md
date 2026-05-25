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

Each AWS region is an independent stack. Build once, deploy twice:

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
```

> `BackendUrl` defaults to `https://api.upstat.online/api`. To override, add
> `BackendUrl=...` to `--parameter-overrides`.

## Verify

Tail the logs (you should see `checked=N errors=0` every minute):

```bash
sam logs --stack-name upstat-probe-eu --region eu-west-1 --tail
```

To confirm geographic accuracy, the AWS region itself is the guarantee — no
need to measure colo like we did with Cloudflare.

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
