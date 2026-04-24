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
    """Extract the user's access token for OBO.

    Prefers the EasyAuth token store header; falls back to the Authorization
    bearer token sent by the MSAL frontend, which is a valid OBO assertion.
    """
    token = req.headers.get("X-MS-TOKEN-AAD-ACCESS-TOKEN", "")
    if token:
        return token
    auth_header = req.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:]
    raise ValueError("User token unavailable — no EasyAuth token store header or Authorization bearer token found")
