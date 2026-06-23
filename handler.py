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
import random
import time
from urllib.parse import quote

import httpx

CHECK_TIMEOUT_S = 10.0


async def _request_with_retry(
    client, method, url, *, retries=3, base_delay=0.5, region="?", **kwargs
):
    # Retry com backoff exponencial pra falhas transitórias: erros de rede
    # (TransportError/TimeoutException) e respostas 5xx/429. NÃO faz retry em
    # 4xx que não seja 429 — erro do cliente não some repetindo.
    last_exc = None
    last_res = None
    for attempt in range(retries + 1):
        try:
            res = await client.request(method, url, **kwargs)
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            last_exc = exc
            last_res = None
            if attempt >= retries:
                print(
                    f"[probe {region}] {method} {url} network error after "
                    f"{attempt + 1} attempts: {exc!r}"
                )
                raise
            delay = base_delay * (2 ** attempt) + random.uniform(0, 0.1)
            print(
                f"[probe {region}] {method} {url} network error "
                f"(attempt {attempt + 1}/{retries + 1}): {exc!r} — "
                f"retrying in {delay:.2f}s"
            )
            await asyncio.sleep(delay)
            continue

        last_res = res
        last_exc = None
        if res.status_code >= 500 or res.status_code == 429:
            if attempt >= retries:
                print(
                    f"[probe {region}] {method} {url} returned "
                    f"{res.status_code} after {attempt + 1} attempts"
                )
                return res
            delay = base_delay * (2 ** attempt) + random.uniform(0, 0.1)
            print(
                f"[probe {region}] {method} {url} returned {res.status_code} "
                f"(attempt {attempt + 1}/{retries + 1}) — retrying in {delay:.2f}s"
            )
            await asyncio.sleep(delay)
            continue

        # Sucesso (2xx/3xx) ou 4xx não-429: não adianta repetir.
        return res

    # Inalcançável na prática, mas mantém o contrato de re-levantar/retornar.
    if last_exc is not None:
        raise last_exc
    return last_res


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
            jobs_res = await _request_with_retry(
                client,
                "GET",
                f"{env['BACKEND_URL']}/internal/probes/jobs?region={quote(env['REGION'])}",
                region=env["REGION"],
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
    res = await _request_with_retry(
        client,
        "POST",
        f"{env['BACKEND_URL']}/internal/probes/results",
        region=env["REGION"],
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
            "dns_ms": result.get("dns_ms"),
            "tcp_ms": result.get("tcp_ms"),
            "tls_ms": result.get("tls_ms"),
            "ttfb_ms": result.get("ttfb_ms"),
        },
    )
    if res.status_code >= 400:
        raise RuntimeError(
            f"POST /results returned {res.status_code} for {monitor['id']}"
        )


_NO_TIMINGS = {"dns_ms": None, "tcp_ms": None, "tls_ms": None, "ttfb_ms": None}


def _delta_ms(marks, a, b):
    if a in marks and b in marks:
        return int((marks[b] - marks[a]) * 1000)
    return None


def phase_timings(marks):
    # Breakdown via trace do httpcore. DNS fica embutido no connect_tcp (o
    # backend é quem separa DNS de TCP). Conexão reusada = sem marks de
    # connect/tls → fases ficam None e a UI degrada sozinha.
    anchor = (
        "connection.start_tls.complete"
        if "connection.start_tls.complete" in marks
        else "connection.connect_tcp.complete"
    )
    return {
        "dns_ms": None,
        "tcp_ms": _delta_ms(
            marks, "connection.connect_tcp.started", "connection.connect_tcp.complete"
        ),
        "tls_ms": _delta_ms(
            marks, "connection.start_tls.started", "connection.start_tls.complete"
        ),
        "ttfb_ms": _delta_ms(
            marks, anchor, "http11.receive_response_headers.complete"
        ),
    }


async def run_check(client, monitor):
    # Diferente do Worker da Cloudflare, a Lambda roda Python completo e tem
    # sockets — TCP poderia ser suportado aqui no futuro (asyncio.open_connection).
    # Por ora mantém o mesmo contrato: TCP fica só na região default.
    if monitor.get("monitor_type") == "tcp":
        return {"status": "down", "status_code": None, "latency_ms": 0, **_NO_TIMINGS}

    # Marca o timestamp de cada etapa de rede do httpcore pra montar o breakdown.
    marks = {}

    async def trace(name, info):
        if name not in marks:
            marks[name] = time.monotonic()

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
            extensions={"trace": trace},
        )
        latency = int((time.monotonic() - start) * 1000)
        status_code = response.status_code
        status = "up" if 200 <= status_code < 400 else "down"

        if status == "up" and monitor.get("keyword"):
            body = response.text
            if monitor["keyword"] not in body:
                status = "down"

        return {
            "status": status,
            "status_code": status_code,
            "latency_ms": latency,
            **phase_timings(marks),
        }
    except httpx.TimeoutException:
        latency = int((time.monotonic() - start) * 1000)
        return {
            "status": "timeout",
            "status_code": None,
            "latency_ms": latency,
            **phase_timings(marks),
        }
    except Exception:
        latency = int((time.monotonic() - start) * 1000)
        return {
            "status": "down",
            "status_code": None,
            "latency_ms": latency,
            **phase_timings(marks),
        }
