import logging
import os

import requests
from azure.identity import OnBehalfOfCredential

from registry import list_apps
from azure_metadata import get_tenant_id

logger = logging.getLogger("dashboard-api.discovery")

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
    tenant_id = get_tenant_id()
    if not all([tenant_id, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET]):
        raise RuntimeError("OBO credentials not configured (AZURE_TENANT_ID or IMDS, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET)")

    credential = OnBehalfOfCredential(
        tenant_id=tenant_id,
        client_id=AZURE_CLIENT_ID,
        client_secret=AZURE_CLIENT_SECRET,
        user_assertion=user_token,
    )
    arm_token = credential.get_token("https://management.azure.com/.default").token

    type_filter = " or ".join(f'type =~ "{t}"' for t in MONITORED_TYPES)
    query = f"""
Resources
| where {type_filter}
| extend healthUrl = case(
    type =~ "microsoft.app/containerapps", strcat("https://", tostring(properties.configuration.ingress.fqdn)),
    type =~ "microsoft.web/sites",         strcat("https://", tostring(properties.defaultHostName)),
    type =~ "microsoft.web/staticsites",   strcat("https://", tostring(properties.defaultHostname)),
    "")
| project id, name, type, location, resourceGroup, subscriptionId, healthUrl
| order by name asc
"""

    resp = requests.post(
        RESOURCE_GRAPH_URL,
        json={"query": query, "options": {"resultFormat": "objectArray"}},
        headers={"Authorization": f"Bearer {arm_token}", "Content-Type": "application/json"},
        timeout=15,
    )
    resp.raise_for_status()

    registered_apps = list_apps()
    registered_ids = {app["resource_id"].lower() for app in registered_apps}
    registered_names = {app["name"].lower() for app in registered_apps}

    resources = []
    for item in resp.json().get("data", []):
        already_registered = (
            item.get("id", "").lower() in registered_ids
            or item.get("name", "").lower() in registered_names
        )
        resources.append({
            "id": item.get("id", ""),
            "name": item.get("name", ""),
            "type": item.get("type", ""),
            "location": item.get("location", ""),
            "resource_group": item.get("resourceGroup", ""),
            "subscription_id": item.get("subscriptionId", ""),
            "health_url": item.get("healthUrl", ""),
            "already_registered": already_registered,
        })
    return resources
