import azure.functions as func
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import requests
from azure.data.tables import TableServiceClient
from azure.identity import ManagedIdentityCredential
from azure.monitor.query import LogsQueryClient, LogsQueryStatus
from azure.core.exceptions import HttpResponseError

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

logger = logging.getLogger("dashboard-api")

LOG_ANALYTICS_WORKSPACE_ID = os.environ.get("LOG_ANALYTICS_WORKSPACE_ID", "")
DIGITS_METRICS_CONNECTION_STRING = os.environ.get("DIGITS_METRICS_CONNECTION_STRING", "")
MOMENTUM_FINDER_URL = os.environ.get("MOMENTUM_FINDER_URL", "")
TRAIL_FINDER_URL = os.environ.get("TRAIL_FINDER_URL", "")
AZURE_SUBSCRIPTION_ID = os.environ.get("AZURE_SUBSCRIPTION_ID", "")
AZURE_RESOURCE_GROUP = os.environ.get("AZURE_RESOURCE_GROUP", "")

# Module-level cost cache
_cost_cache: dict = {}


def _check_health(name: str, url: str) -> dict:
    if not url:
        return {"status": "unknown", "error": "URL not configured"}
    try:
        start = time.time()
        resp = requests.get(f"{url}/health", timeout=45)
        latency_ms = round((time.time() - start) * 1000)
        result = {"status": "up" if resp.status_code == 200 else "down", "latency_ms": latency_ms}
        if latency_ms > 5000:
            result["cold_start"] = True
        return result
    except Exception as e:
        return {"status": "down", "error": str(e)}


def _query_log_analytics() -> tuple[list, list]:
    """Returns (metrics_rows, error_rows)"""
    if not LOG_ANALYTICS_WORKSPACE_ID:
        return [], []
    try:
        credential = ManagedIdentityCredential()
        client = LogsQueryClient(credential)
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=24)

        metrics_query = """
ContainerAppConsoleLogs_CL
| where TimeGenerated between (datetime({start}) .. datetime({end}))
| where Log_s has '"event": "request"'
| extend parsed = parse_json(Log_s)
| where parsed.service in ("momentum-finder", "trail-finder")
| summarize
    request_count = count(),
    avg_latency_ms = round(avg(toreal(parsed.duration_ms)), 1),
    errors_24h = countif(toreal(parsed.status) >= 500)
  by service = tostring(parsed.service), endpoint = tostring(parsed.path)
| order by service asc, request_count desc
""".format(start=start.isoformat(), end=end.isoformat())

        errors_query = """
ContainerAppConsoleLogs_CL
| where TimeGenerated between (datetime({start}) .. datetime({end}))
| where Log_s has '"event": "error"'
| extend parsed = parse_json(Log_s)
| where parsed.service in ("momentum-finder", "trail-finder")
| project
    timestamp = TimeGenerated,
    service = tostring(parsed.service),
    endpoint = tostring(parsed.path),
    message = tostring(parsed.error)
| order by timestamp desc
| limit 50
""".format(start=start.isoformat(), end=end.isoformat())

        metrics_result = client.query_workspace(LOG_ANALYTICS_WORKSPACE_ID, metrics_query, timespan=timedelta(hours=24))
        errors_result = client.query_workspace(LOG_ANALYTICS_WORKSPACE_ID, errors_query, timespan=timedelta(hours=24))

        metrics_rows = []
        if metrics_result.status == LogsQueryStatus.SUCCESS:
            for row in metrics_result.tables[0].rows:
                metrics_rows.append({
                    "service": row[0],
                    "endpoint": row[1],
                    "requests_24h": int(row[2]),
                    "avg_latency_ms": float(row[3]),
                    "errors_24h": int(row[4]),
                })

        error_rows = []
        if errors_result.status == LogsQueryStatus.SUCCESS:
            for row in errors_result.tables[0].rows:
                error_rows.append({
                    "timestamp": row[0].isoformat() if hasattr(row[0], "isoformat") else str(row[0]),
                    "service": row[1],
                    "endpoint": row[2],
                    "message": row[3],
                })

        return metrics_rows, error_rows
    except Exception as e:
        logger.error(f"Log Analytics query failed: {e}")
        return [], []


def _query_digits_metrics() -> tuple[list, list]:
    """Returns (metrics_rows, error_rows) from Digits Table Storage."""
    if not DIGITS_METRICS_CONNECTION_STRING:
        return [], []
    try:
        table_service = TableServiceClient.from_connection_string(DIGITS_METRICS_CONNECTION_STRING)
        table_client = table_service.get_table_client("metrics")
        cutoff = (datetime.utcnow() - timedelta(hours=24)).isoformat()

        rows = list(table_client.query_entities(
            f"PartitionKey eq 'metrics' and RowKey ge '{cutoff}'"
        ))

        from collections import defaultdict
        by_endpoint: dict = defaultdict(lambda: {"requests_24h": 0, "total_ms": 0.0, "errors_24h": 0})
        error_rows = []

        for row in rows:
            ep = row.get("endpoint", "unknown")
            by_endpoint[ep]["requests_24h"] += 1
            by_endpoint[ep]["total_ms"] += float(row.get("duration_ms", 0))
            if row.get("status", 200) >= 500:
                by_endpoint[ep]["errors_24h"] += 1
                error_rows.append({
                    "timestamp": row.get("Timestamp", row.get("RowKey", "")).split("T")[0],
                    "service": "digits",
                    "endpoint": ep,
                    "message": row.get("error", ""),
                })

        metrics_rows = []
        for ep, data in by_endpoint.items():
            avg = round(data["total_ms"] / data["requests_24h"], 1) if data["requests_24h"] > 0 else 0.0
            metrics_rows.append({
                "service": "digits",
                "endpoint": ep,
                "requests_24h": data["requests_24h"],
                "avg_latency_ms": avg,
                "errors_24h": data["errors_24h"],
            })

        return metrics_rows, error_rows
    except Exception as e:
        logger.error(f"Digits metrics query failed: {e}")
        return [], []


def _get_cost() -> dict:
    global _cost_cache
    now = datetime.utcnow()
    if _cost_cache.get("fetched_at") and (now - _cost_cache["fetched_at"]) < timedelta(hours=1):
        return _cost_cache["data"]

    if not AZURE_SUBSCRIPTION_ID or not AZURE_RESOURCE_GROUP:
        return {"error": "Not configured"}

    try:
        credential = ManagedIdentityCredential()
        token = credential.get_token("https://management.azure.com/.default").token
        url = (
            f"https://management.azure.com/subscriptions/{AZURE_SUBSCRIPTION_ID}"
            f"/resourceGroups/{AZURE_RESOURCE_GROUP}"
            f"/providers/Microsoft.CostManagement/query?api-version=2023-11-01"
        )
        body = {
            "type": "ActualCost",
            "timeframe": "MonthToDate",
            "dataset": {
                "granularity": "None",
                "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
                "grouping": [{"type": "Dimension", "name": "ServiceName"}],
            },
        }
        resp = requests.post(url, json=body, headers={"Authorization": f"Bearer {token}"}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        rows = data.get("properties", {}).get("rows", [])
        by_service = [{"service": r[1], "cost": round(r[0], 4)} for r in rows if r[0] > 0]
        total = round(sum(r[0] for r in rows), 4)
        currency = data.get("properties", {}).get("columns", [{}])[0].get("name", "USD")
        result = {"currency": "USD", "month_to_date": total, "by_service": by_service}
        _cost_cache = {"data": result, "fetched_at": now}
        return result
    except Exception as e:
        logger.error(f"Cost Management query failed: {e}")
        return {"error": str(e)}


@app.route(route="DashboardGetter", methods=["GET"])
def DashboardGetter(req: func.HttpRequest) -> func.HttpResponse:
    with ThreadPoolExecutor(max_workers=5) as executor:
        f_momentum_health = executor.submit(_check_health, "momentum-finder", MOMENTUM_FINDER_URL)
        f_trail_health = executor.submit(_check_health, "trail-finder", TRAIL_FINDER_URL)
        f_la = executor.submit(_query_log_analytics)
        f_digits = executor.submit(_query_digits_metrics)
        f_cost = executor.submit(_get_cost)

        momentum_health = f_momentum_health.result()
        trail_health = f_trail_health.result()
        la_metrics, la_errors = f_la.result()
        digits_metrics, digits_errors = f_digits.result()
        cost = f_cost.result()

    # Group metrics by service
    metrics_by_service: dict = {"momentum-finder": [], "trail-finder": [], "digits": []}
    for row in la_metrics:
        svc = row.pop("service", "unknown")
        metrics_by_service.setdefault(svc, []).append(row)
    for row in digits_metrics:
        row.pop("service", None)
        metrics_by_service["digits"].append(row)

    all_errors = sorted(la_errors + digits_errors, key=lambda x: x.get("timestamp", ""), reverse=True)

    payload = {
        "health": {
            "digits": {"status": "up"},
            "momentum-finder": momentum_health,
            "trail-finder": trail_health,
        },
        "metrics": metrics_by_service,
        "errors": all_errors[:50],
        "cost": cost,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "note": "Log Analytics data has ~5 min ingestion delay",
    }

    return func.HttpResponse(
        json.dumps(payload),
        mimetype="application/json",
        headers={"Access-Control-Allow-Origin": "*"},
    )


@app.route(route="health", methods=["GET"])
def health(req: func.HttpRequest) -> func.HttpResponse:
    return func.HttpResponse(json.dumps({"status": "ok"}), mimetype="application/json")
