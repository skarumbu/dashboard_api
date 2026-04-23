import logging
import os

import requests
from azure.identity import OnBehalfOfCredential

from registry import get_app

logger = logging.getLogger("dashboard-api.discovery")

AZURE_TENANT_ID = os.environ.get("AZURE_TENANT_ID", "")
AZURE_CLIENT_ID = os.environ.get("AZURE_CLIENT_ID", "")
AZURE_CLIENT_SECRET = os.environ.get("AZURE_CLIENT_SECRET", "")

MONITORED_TYPES = {
    "microsoft.web/sites",
    "microsoft.app/containerapps",
    "microsoft.apimanagement/service",
    "microsoft.web/staticsites",
    "microsoft.app/jobs",
}

RESOURCE_GRAPH_URL = "https://management.azure.com/providers/Microsoft.ResourceGraph/resources?api-version=2021-03-01"


def discover_resources(user_token: str) -> list[dict]:
    if not all([AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET]):
        raise RuntimeError("OBO credentials not configured (AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET)")

    credential = OnBehalfOfCredential(
        tenant_id=AZURE_TENANT_ID,
        client_id=AZURE_CLIENT_ID,
        client_secret=AZURE_CLIENT_SECRET,
        user_assertion=user_token,
    )
    arm_token = credential.get_token("https://management.azure.com/.default").token

    type_filter = " or ".join(f'type =~ "{t}"' for t in MONITORED_TYPES)
    query = f"Resources | where {type_filter} | project id, name, type, location, resourceGroup, subscriptionId | order by name asc"

    resp = requests.post(
        RESOURCE_GRAPH_URL,
        json={"query": query},
        headers={"Authorization": f"Bearer {arm_token}", "Content-Type": "application/json"},
        timeout=15,
    )
    resp.raise_for_status()

    rows = resp.json().get("data", {}).get("rows", [])
    columns = [c["name"] for c in resp.json().get("data", {}).get("columns", [])]

    resources = []
    for row in rows:
        item = dict(zip(columns, row))
        already_registered = get_app(item.get("id", "")) is not None
        resources.append({
            "id": item.get("id", ""),
            "name": item.get("name", ""),
            "type": item.get("type", ""),
            "location": item.get("location", ""),
            "resource_group": item.get("resourceGroup", ""),
            "subscription_id": item.get("subscriptionId", ""),
            "already_registered": already_registered,
        })
    return resources
