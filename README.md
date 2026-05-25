# upstat-probe-lambda

Probe externo do Upstat em **AWS Lambda** — substitui os Cloudflare Workers
pra ter **localização geográfica garantida** por região AWS.

- `handler.py` — lógica do probe (igual ao worker: pega jobs → checa → reporta)
- `template.yaml` — SAM: Lambda `python3.12` + EventBridge cron de 1 min
- `requirements.txt` — única dep: `httpx` (HTTP client async)

## Pré-requisitos

- AWS CLI configurado (`aws configure`) com credenciais da conta
- SAM CLI instalado (`sam --version`)
- O `PROBE_SECRET` (mesmo valor do `PROBE_SECRET` no env do backend no Render —
  copie de lá, não versione aqui)

## Deploy

Cada região AWS é uma stack independente. Roda o build uma vez e deploya nas duas:

```bash
cd upstat-probe-lambda
sam build

# Europa — Irlanda
sam deploy \
  --region eu-west-1 \
  --stack-name upstat-probe-eu \
  --resolve-s3 \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides ProbeRegion=eu-west ProbeSecret=<SECRET>

# América do Sul — São Paulo
sam deploy \
  --region sa-east-1 \
  --stack-name upstat-probe-br \
  --resolve-s3 \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides ProbeRegion=sa-east ProbeSecret=<SECRET>
```

> `BackendUrl` tem default `https://api.upstat.online/api`. Pra sobrescrever,
> adicione `BackendUrl=...` no `--parameter-overrides`.

## Verificar

Logs em tempo real (mostra `checked=N errors=0` a cada minuto):

```bash
sam logs --stack-name upstat-probe-eu --region eu-west-1 --tail
```

Pra confirmar a geo real, a própria região AWS já garante a localização —
não precisa medir colo como no Cloudflare.

## ⚠️ IMPORTANTE — desligar os Cloudflare Workers ao migrar

Os workers antigos (`upstat-probe-eu` / `upstat-probe-br` na Cloudflare) escrevem
pings pras MESMAS regiões (`eu-west` / `sa-east`), mas saindo de Singapura. Se
deixar os dois rodando, vão **duplicar e conflitar** os dados. Ao confirmar que a
Lambda está reportando, **delete os workers da Cloudflare**:

```bash
cd ../upstat-probe-worker
npx wrangler delete --config wrangler.eu.toml
npx wrangler delete --config wrangler.br.toml
```

A região `us-east` continua sendo o probe local do Render — não muda nada.
