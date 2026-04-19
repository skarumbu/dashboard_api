import base64
import json

import azure.functions as func


def require_auth(req: func.HttpRequest) -> tuple[str, str, str]:
    """Decode EasyAuth principal header. Returns (oid, email, display_name)."""
    principal_b64 = req.headers.get("X-MS-CLIENT-PRINCIPAL")
    if not principal_b64:
        raise ValueError("Unauthenticated")
    principal = json.loads(base64.b64decode(principal_b64 + "=="))
    claims = {c["typ"]: c["val"] for c in principal.get("claims", [])}
    oid = claims.get("http://schemas.microsoft.com/identity/claims/objectidentifier", claims.get("oid", ""))
    email = claims.get("preferred_username", claims.get("upn", claims.get("email", "")))
    name = claims.get("name", email)
    return oid, email, name


def get_user_token(req: func.HttpRequest) -> str:
    """Extract the user's ARM-scoped access token injected by EasyAuth token store."""
    token = req.headers.get("X-MS-TOKEN-AAD-ACCESS-TOKEN", "")
    if not token:
        raise ValueError("User token unavailable — ensure EasyAuth token store is enabled")
    return token
