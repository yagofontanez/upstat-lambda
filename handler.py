# Probe externo do Upstat rodando em AWS Lambda (geo real por região).
# Acionado por EventBridge Scheduler (rate de 1 min). Mesma lógica do
# Cloudflare Worker (upstat-probe-worker), mas com localização garantida:
# deploy em eu-west-1 (Irlanda) e sa-east-1 (São Paulo).
#
# Env vars (configuradas por região no deploy):
#   REGION        — rótulo lógico que casa com o catálogo `regions` do backend
#   BACKEND_URL   — https://api.upstat.online/api
#   PROBE_SECRET  — mesmo valor do backend (header x-probe-secret)

import asyncio
import json
import os
import time
from urllib.parse import quote

import httpx

CHECK_TIMEOUT_S = 10.0


def lambda_handler(event, context):
    env = {
        "REGION": os.environ["REGION"],
        "BACKEND_URL": os.environ["BACKEND_URL"],
        "PROBE_SECRET": os.environ["PROBE_SECRET"],
    }
    result = asyncio.run(run_probe(env))
    return {"statusCode": 200, "body": json.dumps(result)}


async def run_probe(env):
    start = time.monotonic()
    checked = 0
    errors = 0

    async with httpx.AsyncClient(timeout=CHECK_TIMEOUT_S) as client:
        try:
            jobs_res = await client.get(
                f"{env['BACKEND_URL']}/internal/probes/jobs?region={quote(env['REGION'])}",
                headers={"x-probe-secret": env["PROBE_SECRET"]},
            )
            if jobs_res.status_code >= 400:
                print(f"[probe {env['REGION']}] jobs fetch failed: {jobs_res.status_code}")
                return {"region": env["REGION"], "checked": 0, "errors": 1}

            monitors = jobs_res.json().get("monitors", [])
            if not monitors:
                return {"region": env["REGION"], "checked": 0, "errors": 0}

            results = await asyncio.gather(
                *(check_and_report(client, m, env) for m in monitors),
                return_exceptions=True,
            )
            for r in results:
                if isinstance(r, Exception):
                    errors += 1
                else:
                    checked += 1
        except Exception as err:
            print(f"[probe {env['REGION']}] fatal: {err!r}")
            errors += 1

    elapsed_ms = int((time.monotonic() - start) * 1000)
    print(f"[probe {env['REGION']}] done in {elapsed_ms}ms — checked={checked} errors={errors}")
    return {"region": env["REGION"], "checked": checked, "errors": errors}


async def check_and_report(client, monitor, env):
    result = await run_check(client, monitor)
    res = await client.post(
        f"{env['BACKEND_URL']}/internal/probes/results",
        headers={
            "Content-Type": "application/json",
            "x-probe-secret": env["PROBE_SECRET"],
        },
        json={
            "monitor_id": monitor["id"],
            "region_code": env["REGION"],
            "status": result["status"],
            "status_code": result["status_code"],
            "latency_ms": result["latency_ms"],
        },
    )
    if res.status_code >= 400:
        raise RuntimeError(
            f"POST /results returned {res.status_code} for {monitor['id']}"
        )


async def run_check(client, monitor):
    # Diferente do Worker da Cloudflare, a Lambda roda Python completo e tem
    # sockets — TCP poderia ser suportado aqui no futuro (asyncio.open_connection).
    # Por ora mantém o mesmo contrato: TCP fica só na região default.
    if monitor.get("monitor_type") == "tcp":
        return {"status": "down", "status_code": None, "latency_ms": 0}

    start = time.monotonic()
    method = monitor.get("http_method") or "GET"
    has_body = method in ("POST", "PUT", "PATCH")

    headers = {"User-Agent": "UpStat-Monitor/1.0"}
    headers.update(monitor.get("request_headers") or {})

    try:
        response = await client.request(
            method,
            monitor["url"],
            headers=headers,
            content=monitor.get("request_body") if has_body else None,
            follow_redirects=False,
            timeout=CHECK_TIMEOUT_S,
        )
        latency = int((time.monotonic() - start) * 1000)
        status_code = response.status_code
        status = "up" if 200 <= status_code < 400 else "down"

        if status == "up" and monitor.get("keyword"):
            body = response.text
            if monitor["keyword"] not in body:
                status = "down"

        return {"status": status, "status_code": status_code, "latency_ms": latency}
    except httpx.TimeoutException:
        latency = int((time.monotonic() - start) * 1000)
        return {"status": "timeout", "status_code": None, "latency_ms": latency}
    except Exception:
        latency = int((time.monotonic() - start) * 1000)
        return {"status": "down", "status_code": None, "latency_ms": latency}
