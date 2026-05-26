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
    from urllib.parse import urlparse
    if not health_url or not urlparse(health_url).hostname:
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
| where Log_s has '"event": "error"'
| extend parsed = parse_json(Log_s)
| where tostring(parsed.service) == "{service}"
| project
    timestamp = TimeGenerated,
    endpoint = tostring(parsed.path),
    message = tostring(parsed.error)
| order by timestamp desc
| limit 20
""".format(service=service_name)

        timespan = timedelta(hours=window_hours)
        m_result = client.query_workspace(workspace_id, metrics_query, timespan=timespan)
        e_result = client.query_workspace(workspace_id, errors_query, timespan=timedelta(days=30))

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
                error_row = {
                    "timestamp": row[0].isoformat() if hasattr(row[0], "isoformat") else str(row[0]),
                    "service": service_name,
                    "endpoint": row[1],
                    "message": row[2],
                    "error_type": row.get("Properties_error_type") or row.get("error_type"),
                    "status": row.get("Properties_status") or row.get("status"),
                }
                error_rows.append({k: v for k, v in error_row.items() if v is not None})

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
                error_row = {
                    "timestamp": row.get("Timestamp", row.get("RowKey", "")).split("T")[0],
                    "service": "digits",
                    "endpoint": ep,
                    "message": row.get("error", ""),
                    "error_type": row.get("Properties_error_type") or row.get("error_type"),
                    "status": row.get("Properties_status") or row.get("status"),
                }
                error_rows.append({k: v for k, v in error_row.items() if v is not None})

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
| where Cloud_RoleName =~ "{service}"
| project
    timestamp = TimeGenerated,
    endpoint = OperationName,
    message = OuterMessage
| order by timestamp desc
| limit 20
""".format(service=service_name)

        timespan = timedelta(hours=window_hours)
        m_result = client.query_workspace(workspace_id, metrics_query, timespan=timespan)
        e_result = client.query_workspace(workspace_id, errors_query, timespan=timedelta(days=30))

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
                error_row = {
                    "timestamp": row[0].isoformat() if hasattr(row[0], "isoformat") else str(row[0]),
                    "service": service_name,
                    "endpoint": row[1],
                    "message": row[2],
                    "error_type": row.get("Properties_error_type") or row.get("error_type"),
                    "status": row.get("Properties_status") or row.get("status"),
                }
                error_rows.append({k: v for k, v in error_row.items() if v is not None})

        return metrics_rows, error_rows
    except Exception as e:
        logger.error(f"Log Analytics (AppRequests) query failed for {service_name}: {e}")
        return [], []


def _fetch_execution_logs(arm_token: str, resource_id: str, execution_name: str) -> str | None:
    """Fetch the tail of a Container App Job execution's console output via the log-stream API.

    Works for fast-failing containers that exit before logs reach Log Analytics.
    Returns the most relevant error lines joined into one string, or None on failure.
    """
    if not execution_name:
        return None
    try:
        auth_url = (
            f"https://management.azure.com{resource_id}"
            f"/executions/{execution_name}/getAuthToken?api-version=2024-03-01"
        )
        auth_resp = requests.post(
            auth_url,
            headers={"Authorization": f"Bearer {arm_token}"},
            timeout=10,
        )
        if not auth_resp.ok:
            return None
        auth_data = auth_resp.json()
        log_endpoint = auth_data.get("logStreamEndpoint", "")
        log_token = auth_data.get("token", "")
        if not log_endpoint or not log_token:
            return None

        log_resp = requests.get(
            f"{log_endpoint}?follow=false&tailLines=50",
            headers={"Authorization": f"Bearer {log_token}"},
            timeout=10,
        )
        if not log_resp.ok:
            return None

        lines = [l.strip() for l in log_resp.text.splitlines() if l.strip()]
        error_lines = [
            l for l in lines
            if any(kw in l.lower() for kw in ("error", "exception", "fail", "fatal"))
        ]
        candidates = error_lines if error_lines else lines[-5:]
        return " | ".join(candidates[:5]) if candidates else None
    except Exception as e:
        logger.debug(f"_fetch_execution_logs failed for {execution_name}: {e}")
        return None


def check_job_health(resource_id: str, workspace_id: str) -> tuple[dict, list]:
    """Returns (health_status, error_rows) for a Container App Job.

    Health is derived from the most recent execution status.
    Errors are plain-text logs from the most recent execution.
    """
    health = {"status": "unknown", "error": "No executions found"}
    errors = []

    if not resource_id:
        return {"status": "unknown", "error": "resource_id not configured"}, []

    try:
        credential = ManagedIdentityCredential()
        token = credential.get_token("https://management.azure.com/.default").token
        url = f"https://management.azure.com{resource_id}/executions?api-version=2023-05-01&$top=1"
        resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=15)
        resp.raise_for_status()
        executions = resp.json().get("value", [])
        if executions:
            latest = executions[0]
            execution_name = latest.get("name", "")
            props = latest.get("properties", {})
            raw_status = props.get("status", "unknown")
            start_time = props.get("startTime", "")
            health = {
                "status": "up" if raw_status == "Succeeded" else "down" if raw_status == "Failed" else "unknown",
                "last_run": start_time,
                "last_run_status": raw_status,
            }
            if raw_status == "Failed":
                job_name = resource_id.rstrip("/").split("/")[-1]
                log_message = _fetch_execution_logs(token, resource_id, execution_name)
                errors.append({
                    "timestamp": start_time,
                    "service": job_name,
                    "endpoint": "",
                    "message": log_message or "Job execution failed — logs unavailable (container may have exited before log shipping completed)",
                })
    except Exception as e:
        logger.error(f"Job execution check failed for {resource_id}: {e}")
        health = {"status": "unknown", "error": str(e)}

    if workspace_id:
        try:
            credential = ManagedIdentityCredential()
            client = LogsQueryClient(credential)
            job_name = resource_id.rstrip("/").split("/")[-1]
            errors_query = """
ContainerAppConsoleLogs_CL
| where ContainerAppName_s startswith "{job}"
| where Log_s has_any ("error", "Error", "ERROR", "exception", "Exception", "failed", "Failed")
| project timestamp = TimeGenerated, endpoint = "", message = Log_s
| order by timestamp desc
| limit 20
""".format(job=job_name)
            result = client.query_workspace(workspace_id, errors_query, timespan=timedelta(days=30))
            if result.status == LogsQueryStatus.SUCCESS and result.tables:
                for row in result.tables[0].rows:
                    error_row = {
                        "timestamp": row[0].isoformat() if hasattr(row[0], "isoformat") else str(row[0]),
                        "service": job_name,
                        "endpoint": row[1],
                        "message": row[2],
                        "error_type": row.get("Properties_error_type") or row.get("error_type"),
                        "status": row.get("Properties_status") or row.get("status"),
                    }
                    errors.append({k: v for k, v in error_row.items() if v is not None})
        except Exception as e:
            logger.error(f"Job log query failed for {resource_id}: {e}")

    return health, errors


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

    if app_type == "ContainerAppJob":
        resource_id = app.get("resource_id", "")
        health, errors = check_job_health(resource_id, workspace_id)
        return health, [], errors

    health = check_http_health(health_url)

    if workspace_id:
        if app_type == "FunctionApp":
            metrics, errors = query_log_analytics_function_app(name, workspace_id)
        else:
            metrics, errors = query_log_analytics(name, workspace_id)
    else:
        metrics, errors = [], []

    return health, metrics, errors
