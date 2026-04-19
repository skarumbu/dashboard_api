import logging
import os
import urllib.parse
from datetime import datetime, timezone

from azure.data.tables import TableServiceClient
from azure.core.exceptions import ResourceNotFoundError

logger = logging.getLogger("dashboard-api.registry")

REGISTRY_TABLE_CONNECTION_STRING = os.environ.get("REGISTRY_TABLE_CONNECTION_STRING", "")
TABLE_NAME = "registeredapps"
VALID_TYPES = {"ContainerApp", "FunctionApp", "APIM", "custom"}


def _get_table_client():
    if not REGISTRY_TABLE_CONNECTION_STRING:
        raise RuntimeError("REGISTRY_TABLE_CONNECTION_STRING is not configured")
    svc = TableServiceClient.from_connection_string(REGISTRY_TABLE_CONNECTION_STRING)
    return svc.get_table_client(TABLE_NAME)


def _resource_id_to_row_key(resource_id: str) -> str:
    return urllib.parse.quote(resource_id, safe="")


def list_apps() -> list[dict]:
    try:
        client = _get_table_client()
        entities = client.query_entities("PartitionKey eq 'apps'")
        result = []
        for e in entities:
            result.append(_entity_to_dict(e))
        return result
    except Exception as exc:
        logger.error(f"registry list_apps failed: {exc}")
        return []


def get_app(resource_id: str) -> dict | None:
    try:
        client = _get_table_client()
        row_key = _resource_id_to_row_key(resource_id)
        entity = client.get_entity(partition_key="apps", row_key=row_key)
        return _entity_to_dict(entity)
    except ResourceNotFoundError:
        return None
    except Exception as exc:
        logger.error(f"registry get_app failed: {exc}")
        return None


def upsert_app(data: dict, added_by: str = "") -> dict:
    required = ("resource_id", "name", "type")
    for field in required:
        if not data.get(field):
            raise ValueError(f"Missing required field: {field}")
    if data["type"] not in VALID_TYPES:
        raise ValueError(f"type must be one of: {', '.join(sorted(VALID_TYPES))}")

    client = _get_table_client()
    row_key = _resource_id_to_row_key(data["resource_id"])

    try:
        existing = client.get_entity(partition_key="apps", row_key=row_key)
        raise ValueError("App already registered")
    except ResourceNotFoundError:
        pass

    entity = {
        "PartitionKey": "apps",
        "RowKey": row_key,
        "resource_id": data["resource_id"],
        "name": data["name"],
        "type": data["type"],
        "health_url": data.get("health_url", ""),
        "log_workspace_id": data.get("log_workspace_id", ""),
        "enabled": True,
        "added_by": added_by,
        "added_at": datetime.now(timezone.utc).isoformat(),
    }
    client.create_entity(entity)
    return _entity_to_dict(entity)


def delete_app(name: str) -> bool:
    try:
        client = _get_table_client()
        entities = list(client.query_entities(f"PartitionKey eq 'apps' and name eq '{name}'"))
        if not entities:
            return False
        for e in entities:
            client.delete_entity(partition_key="apps", row_key=e["RowKey"])
        return True
    except Exception as exc:
        logger.error(f"registry delete_app failed: {exc}")
        return False


def _entity_to_dict(e) -> dict:
    return {
        "name": e.get("name", ""),
        "type": e.get("type", ""),
        "resource_id": e.get("resource_id", ""),
        "health_url": e.get("health_url", ""),
        "log_workspace_id": e.get("log_workspace_id", ""),
        "enabled": e.get("enabled", True),
        "added_by": e.get("added_by", ""),
        "added_at": e.get("added_at", ""),
    }
