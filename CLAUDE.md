# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Dashboard API is an Azure Functions (Python 3.11) backend that aggregates health and metrics data from registered services and exposes them as HTTP endpoints for a personal dashboard website. Users authenticate via Azure Entra ID (EasyAuth), and new APIs can be registered for monitoring without code changes.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run locally (requires Azure Functions Core Tools v4)
func start

# Deploy to Azure (CI/CD does this automatically on push to master)
func azure functionapp publish <DASHBOARD_API_APP_NAME> --python
```

There is no test suite or linter configured.

## Architecture

**Module layout:**
- `function_app.py` — route handlers only; delegates to other modules
- `auth.py` — EasyAuth principal header parsing (`require_auth`, `get_user_token`)
- `registry.py` — CRUD for the `registeredapps` Table Storage table (which apps to monitor)
- `discovery.py` — Azure Resource Graph queries using On-Behalf-Of (OBO) credential
- `health_checks.py` — parameterized health checks and Log Analytics / Table Storage queries

**Authentication:** Azure Functions App Service Authentication (EasyAuth) validates tokens before requests reach Python code. EasyAuth injects `X-MS-CLIENT-PRINCIPAL` (user identity) and `X-MS-TOKEN-AAD-ACCESS-TOKEN` (user's ARM token). Only `/api/health` is anonymous; all other endpoints call `require_auth(req)`.

**Two credential types in use:**
- `ManagedIdentityCredential` — used for Log Analytics, Table Storage (your own resources)
- `OnBehalfOfCredential` — used for ARM/Resource Graph discovery (scoped to the authenticated user's Azure RBAC)

**HTTP endpoints:**
| Endpoint | Auth | Purpose |
|---|---|---|
| `GET /api/health` | None | Liveness check |
| `GET /api/DashboardGetter` | Required | Aggregated health + metrics for all registered apps |
| `GET /api/discover` | Required | List Azure resources the user has RBAC access to |
| `GET /api/apps` | Required | List apps in the monitoring registry |
| `POST /api/apps` | Required | Register a new app for monitoring |
| `DELETE /api/apps/{name}` | Required | Remove an app from monitoring |

**Adding a new API to monitor (no code changes):**
1. Deploy the Azure resource
2. Call `GET /api/discover` → see it listed with `already_registered: false`
3. Call `POST /api/apps` with `{resource_id, name, type, health_url, log_workspace_id}`
4. It appears in `DashboardGetter` immediately

**`POST /api/apps` body:**
```json
{
  "resource_id": "/subscriptions/.../containerApps/my-api",
  "name": "my-api",
  "type": "ContainerApp",
  "health_url": "https://my-api.example.com",
  "log_workspace_id": "optional-workspace-guid"
}
```
Valid types: `ContainerApp`, `FunctionApp`, `APIM`, `custom`

**Data sources (parallel, ThreadPoolExecutor):**
- HTTP `/health` check per registered app with a `health_url`
- Azure Log Analytics (parameterized per app using `log_workspace_id`)
- Azure Table Storage for `digits`-type apps (`DIGITS_METRICS_CONNECTION_STRING`)
- Azure Cost Management API (1-hour module-level cache)

**Error handling:** Each data fetcher degrades gracefully — missing config or failed requests return partial data rather than crashing the whole response.

## Environment Variables

Required at runtime (Azure Function App settings):
- `REGISTRY_TABLE_CONNECTION_STRING` — Table Storage for the app registry
- `DIGITS_METRICS_CONNECTION_STRING` — Table Storage for digits metrics
- `AZURE_SUBSCRIPTION_ID`, `AZURE_RESOURCE_GROUP` — Cost Management queries
- `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET` — OBO credential for ARM discovery

Required in GitHub Actions secrets for deployment:
- `AZURE_CREDENTIALS`, `DASHBOARD_API_APP_NAME`, `MY_WEBSITE_DISPATCH_TOKEN`

## Azure Setup (one-time)

1. **App Registration** in Entra ID named `dashboard-api`:
   - Delegated permissions: `Azure Service Management` → `user_impersonation`, `Microsoft Graph` → `User.Read`
   - Expose API: App ID URI `api://dashboard-api`, scope `access_as_user`
   - Create a client secret

2. **EasyAuth** on the Function App:
   - Identity provider: Microsoft, using the App Registration above
   - Issuer: `https://login.microsoftonline.com/<tenant-id>/v2.0`
   - Allowed audiences: `api://dashboard-api`
   - Unauthenticated requests: HTTP 401
   - Token store: Enabled

3. **Table Storage**: Create table `registeredapps` in a storage account; grant Managed Identity `Storage Table Data Contributor` role.

## CI/CD

`.github/workflows/deploy.yml` triggers on push to `master`:
1. Deploys with `func azure functionapp publish` (func CLI, not zip deploy)
2. After deploy, commits deployment metadata to the `my-website` repo via `MY_WEBSITE_DISPATCH_TOKEN`

The workflow clears `WEBSITE_RUN_FROM_PACKAGE` before deploying — required for the func CLI deployment method.
