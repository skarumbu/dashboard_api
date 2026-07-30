import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import requests
import azure.functions as func
from azure.identity import ManagedIdentityCredential

from auth import require_auth, get_user_token
from registry import list_apps, upsert_app, update_app, delete_app
from discovery import discover_resources
from health_checks import check_app_health
from github_checks import fetch_github_run, fetch_open_prs
from azure_metadata import get_subscription_id, get_resource_group
from shared_logging import get_logger, log_request

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

logger = get_logger("dashboard-api")

_cost_cache: dict = {}


def _get_cost() -> dict:
    global _cost_cache
    now = datetime.utcnow()
    if _cost_cache.get("fetched_at") and (now - _cost_cache["fetched_at"]) < timedelta(hours=1):
        return _cost_cache["data"]

    subscription_id = get_subscription_id()
    resource_group = get_resource_group()

    if not subscription_id or not resource_group:
        return {"error": "Not configured"}

    try:
        credential = ManagedIdentityCredential()
        token = credential.get_token("https://management.azure.com/.default").token
        url = (
            f"https://management.azure.com/subscriptions/{subscription_id}"
            f"/resourceGroups/{resource_group}"
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
        result = {"currency": "USD", "month_to_date": total, "by_service": by_service}
        _cost_cache = {"data": result, "fetched_at": now}
        return result
    except Exception as e:
        logger.error(f"Cost Management query failed: {e}")
        return {"error": str(e)}


def _unauthorized(message: str = "Unauthorized") -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps({"error": message}),
        status_code=401,
        mimetype="application/json",
        headers={"Access-Control-Allow-Origin": "*"},
    )


def _json_response(data: dict, status_code: int = 200) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps(data),
        status_code=status_code,
        mimetype="application/json",
        headers={"Access-Control-Allow-Origin": "*"},
    )


_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, PATCH, DELETE, OPTIONS",
    "Access-Control-Allow-Headers": "Authorization, Content-Type",
    "Access-Control-Max-Age": "86400",
}


@app.route(route="{*path}", methods=["OPTIONS"])
@log_request(logger)
def cors_preflight(req: func.HttpRequest) -> func.HttpResponse:
    return func.HttpResponse(status_code=204, headers=_CORS_HEADERS)


@app.route(route="health", methods=["GET"])
@log_request(logger)
def health(req: func.HttpRequest) -> func.HttpResponse:
    return _json_response({"status": "ok"})


@app.route(route="DashboardGetter", methods=["GET"])
@log_request(logger)
def DashboardGetter(req: func.HttpRequest) -> func.HttpResponse:
    try:
        require_auth(req)
    except ValueError:
        return _unauthorized()

    apps = list_apps()
    enabled_apps = [a for a in apps if a.get("enabled", True)]

    health_by_service: dict = {}
    metrics_by_service: dict = {}
    all_errors: list = []
    github_actions_by_service: dict = {}
    open_prs_by_service: dict = {}

    def _fetch(app_config: dict):
        return app_config["name"], check_app_health(app_config)

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(_fetch, a): a for a in enabled_apps}
        cost_future = executor.submit(_get_cost)
        github_futures = {
            executor.submit(fetch_github_run, a["github_repo"]): a["name"]
            for a in enabled_apps
            if a.get("github_repo")
        }
        pr_futures = {
            executor.submit(fetch_open_prs, a["github_repo"]): a["name"]
            for a in enabled_apps
            if a.get("github_repo")
        }

        for future in as_completed(futures):
            name, (h, metrics, errors) = future.result()
            health_by_service[name] = h
            metrics_by_service[name] = metrics
            for e in errors:
                e.setdefault("service", name)
            all_errors.extend(errors)

        cost = cost_future.result()

        for future in as_completed(github_futures):
            svc_name = github_futures[future]
            result = future.result()
            if result:  # skip empty dicts (no token, no runs, API error)
                github_actions_by_service[svc_name] = result

        for future in as_completed(pr_futures):
            svc_name = pr_futures[future]
            result = future.result()
            if result:  # skip empty lists (no token, no open PRs, API error)
                open_prs_by_service[svc_name] = result

    all_errors.sort(key=lambda x: x.get("timestamp", ""), reverse=True)

    payload = {
        "health": health_by_service,
        "metrics": metrics_by_service,
        "errors": all_errors[:50],
        "cost": cost,
        "github_actions": github_actions_by_service,
        "open_prs": open_prs_by_service,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "note": "Log Analytics data has ~5 min ingestion delay",
    }
    return _json_response(payload)


@app.route(route="discover", methods=["GET"])
@log_request(logger)
def discover(req: func.HttpRequest) -> func.HttpResponse:
    try:
        _, email, _ = require_auth(req)
        user_token = get_user_token(req)
    except ValueError as e:
        return _unauthorized(str(e))

    try:
        resources = discover_resources(user_token)
    except Exception as e:
        logger.error(f"discover_resources failed: {e}")
        return _json_response({"error": "ARM discovery failed", "detail": str(e)}, status_code=502)

    return _json_response({
        "resources": resources,
        "user": email,
        "discovered_at": datetime.now(timezone.utc).isoformat(),
    })


@app.route(route="apps", methods=["GET"])
@log_request(logger)
def get_apps(req: func.HttpRequest) -> func.HttpResponse:
    try:
        require_auth(req)
    except ValueError:
        return _unauthorized()

    return _json_response({"apps": list_apps()})


@app.route(route="apps", methods=["POST"])
@log_request(logger)
def register_app(req: func.HttpRequest) -> func.HttpResponse:
    try:
        _, email, _ = require_auth(req)
    except ValueError:
        return _unauthorized()

    try:
        body = req.get_json()
    except Exception:
        return _json_response({"error": "Invalid JSON body"}, status_code=400)

    try:
        entity = upsert_app(body, added_by=email)
    except ValueError as e:
        status = 409 if "already registered" in str(e) else 400
        return _json_response({"error": str(e)}, status_code=status)
    except Exception as e:
        logger.error(f"register_app failed: {e}")
        return _json_response({"error": "Failed to register app"}, status_code=500)

    return _json_response(entity, status_code=201)


@app.route(route="apps/{name}", methods=["PATCH"])
@log_request(logger)
def patch_app(req: func.HttpRequest) -> func.HttpResponse:
    try:
        require_auth(req)
    except ValueError:
        return _unauthorized()

    name = req.route_params.get("name", "")
    if not name:
        return _json_response({"error": "App name required"}, status_code=400)

    try:
        body = req.get_json()
    except Exception:
        return _json_response({"error": "Invalid JSON body"}, status_code=400)

    try:
        updated = update_app(name, body)
    except ValueError as e:
        return _json_response({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.error(f"patch_app failed: {e}")
        return _json_response({"error": "Failed to update app"}, status_code=500)

    if updated is None:
        return _json_response({"error": "App not found"}, status_code=404)

    return _json_response(updated)


@app.route(route="apps/{name}", methods=["DELETE"])
@log_request(logger)
def remove_app(req: func.HttpRequest) -> func.HttpResponse:
    try:
        require_auth(req)
    except ValueError:
        return _unauthorized()

    name = req.route_params.get("name", "")
    if not name:
        return _json_response({"error": "App name required"}, status_code=400)

    found = delete_app(name)
    if not found:
        return _json_response({"error": "App not found"}, status_code=404)

    return func.HttpResponse(status_code=204, headers={"Access-Control-Allow-Origin": "*"})
