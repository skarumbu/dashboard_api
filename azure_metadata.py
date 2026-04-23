import logging
import os

import requests

logger = logging.getLogger("dashboard-api.metadata")

_IMDS_URL = "http://169.254.169.254/metadata/instance/compute"
_cache: dict = {}


def _fetch() -> dict:
    if _cache:
        return _cache
    try:
        resp = requests.get(
            _IMDS_URL,
            params={"api-version": "2021-02-01", "format": "json"},
            headers={"Metadata": "true"},
            timeout=2,
        )
        resp.raise_for_status()
        _cache.update(resp.json())
    except Exception as e:
        logger.warning(f"IMDS lookup failed (expected in local dev): {e}")
    return _cache


def get_subscription_id() -> str:
    return _fetch().get("subscriptionId") or os.environ.get("AZURE_SUBSCRIPTION_ID", "")


def get_resource_group() -> str:
    return _fetch().get("resourceGroupName") or os.environ.get("AZURE_RESOURCE_GROUP", "")


def get_tenant_id() -> str:
    return _fetch().get("tenantId") or os.environ.get("AZURE_TENANT_ID", "")
