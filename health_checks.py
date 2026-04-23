import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests
from azure.core.exceptions import HttpResponseError
from azure.data.tables import TableServiceClient
from azure.identity import ManagedIdentityCredential
from azure.monitor.query import LogsQueryClient, LogsQueryStatus

logger = logging.getLogger("dashboard-api.health")

DIGITS_METRICS_CONNECTION_STRING = os.environ.get("DIGITS_METRICS_CONNECTION_STRING", "")


def check_http_health(health_url: str) -> dict:
    if not health_url:
        return {"status": "unknown", "error": "health_url not configured"}
    try:
        start = time.time()
        resp = requests.get(health_url if health_url.endswith("/health") else f"{health_url}/health", timeout=45)
        latency_ms = round((time.time() - start) * 1000)
        result = {"status": "up" if resp.status_code == 200 else "down", "latency_ms": latency_ms}
        if latency_ms > 5000:
            result["cold_start"] = True
        return result
    except Exception as e:
        return {"status": "down", "error": str(e)}


def query_log_analytics(service_name: str, workspace_id: str, window_hours: int = 24) -> tuple[list, list]:
    """Returns (metrics_rows, error_rows) for a single service from Log Analytics."""
    if not workspace_id:
        return [], []
    try:
        credential = ManagedIdentityCredential()
        client = LogsQueryClient(credential)
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=window_hours)

        metrics_query = """
ContainerAppConsoleLogs_CL
| where TimeGenerated between (datetime({start}) .. datetime({end}))
| where Log_s has '"event": "request"'
| extend parsed = parse_json(Log_s)
| where tostring(parsed.service) == "{service}"
| summarize
    request_count = count(),
    avg_latency_ms = round(avg(toreal(parsed.duration_ms)), 1),
    errors = countif(toreal(parsed.status) >= 500)
  by endpoint = tostring(parsed.path)
| order by request_count desc
""".format(start=start.isoformat(), end=end.isoformat(), service=service_name)

        errors_query = """
ContainerAppConsoleLogs_CL
| where TimeGenerated between (datetime({start}) .. datetime({end}))
| where Log_s has '"event": "error"'
| extend parsed = parse_json(Log_s)
| where tostring(parsed.service) == "{service}"
| project
    timestamp = TimeGenerated,
    endpoint = tostring(parsed.path),
    message = tostring(parsed.error)
| order by timestamp desc
| limit 20
""".format(start=start.isoformat(), end=end.isoformat(), service=service_name)

        timespan = timedelta(hours=window_hours)
        m_result = client.query_workspace(workspace_id, metrics_query, timespan=timespan)
        e_result = client.query_workspace(workspace_id, errors_query, timespan=timespan)

        metrics_rows = []
        if m_result.status == LogsQueryStatus.SUCCESS and m_result.tables:
            for row in m_result.tables[0].rows:
                metrics_rows.append({
                    "endpoint": row[0],
                    "requests_24h": int(row[1]),
                    "avg_latency_ms": float(row[2]),
                    "errors_24h": int(row[3]),
                })

        error_rows = []
        if e_result.status == LogsQueryStatus.SUCCESS and e_result.tables:
            for row in e_result.tables[0].rows:
                error_rows.append({
                    "timestamp": row[0].isoformat() if hasattr(row[0], "isoformat") else str(row[0]),
                    "service": service_name,
                    "endpoint": row[1],
                    "message": row[2],
                })

        return metrics_rows, error_rows
    except Exception as e:
        logger.error(f"Log Analytics query failed for {service_name}: {e}")
        return [], []


def query_digits_metrics() -> tuple[list, list]:
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
                "endpoint": ep,
                "requests_24h": data["requests_24h"],
                "avg_latency_ms": avg,
                "errors_24h": data["errors_24h"],
            })

        return metrics_rows, error_rows
    except Exception as e:
        logger.error(f"Digits metrics query failed: {e}")
        return [], []


def query_log_analytics_function_app(service_name: str, workspace_id: str, window_hours: int = 24) -> tuple[list, list]:
    """Returns (metrics_rows, error_rows) for a Function App via AppRequests/AppExceptions tables.

    Requires the Function App to have Application Insights connected to the same workspace.
    Cloud_RoleName must match the registered app name.
    """
    if not workspace_id:
        return [], []
    try:
        credential = ManagedIdentityCredential()
        client = LogsQueryClient(credential)
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=window_hours)

        metrics_query = """
AppRequests
| where TimeGenerated between (datetime({start}) .. datetime({end}))
| where Cloud_RoleName =~ "{service}"
| summarize
    request_count = count(),
    avg_latency_ms = round(avg(DurationMs), 1),
    errors = countif(toint(ResultCode) >= 500)
  by endpoint = Name
| order by request_count desc
""".format(start=start.isoformat(), end=end.isoformat(), service=service_name)

        errors_query = """
AppExceptions
| where TimeGenerated between (datetime({start}) .. datetime({end}))
| where Cloud_RoleName =~ "{service}"
| project
    timestamp = TimeGenerated,
    endpoint = OperationName,
    message = OuterMessage
| order by timestamp desc
| limit 20
""".format(start=start.isoformat(), end=end.isoformat(), service=service_name)

        timespan = timedelta(hours=window_hours)
        m_result = client.query_workspace(workspace_id, metrics_query, timespan=timespan)
        e_result = client.query_workspace(workspace_id, errors_query, timespan=timespan)

        metrics_rows = []
        if m_result.status == LogsQueryStatus.SUCCESS and m_result.tables:
            for row in m_result.tables[0].rows:
                metrics_rows.append({
                    "endpoint": row[0],
                    "requests_24h": int(row[1]),
                    "avg_latency_ms": float(row[2]),
                    "errors_24h": int(row[3]),
                })

        error_rows = []
        if e_result.status == LogsQueryStatus.SUCCESS and e_result.tables:
            for row in e_result.tables[0].rows:
                error_rows.append({
                    "timestamp": row[0].isoformat() if hasattr(row[0], "isoformat") else str(row[0]),
                    "service": service_name,
                    "endpoint": row[1],
                    "message": row[2],
                })

        return metrics_rows, error_rows
    except Exception as e:
        logger.error(f"Log Analytics (AppRequests) query failed for {service_name}: {e}")
        return [], []


def check_app_health(app: dict) -> tuple[dict, list, list]:
    """
    Returns (health_status, metrics_rows, error_rows) for a registered app.
    Dispatches on app["type"].
    """
    app_type = app.get("type", "custom")
    name = app.get("name", "unknown")
    health_url = app.get("health_url", "")
    workspace_id = app.get("log_workspace_id", "")

    if app_type == "custom" and name == "digits":
        metrics, errors = query_digits_metrics()
        return {"status": "up"}, metrics, errors

    health = check_http_health(health_url)

    if workspace_id:
        if app_type == "FunctionApp":
            metrics, errors = query_log_analytics_function_app(name, workspace_id)
        else:
            metrics, errors = query_log_analytics(name, workspace_id)
    else:
        metrics, errors = [], []

    return health, metrics, errors
