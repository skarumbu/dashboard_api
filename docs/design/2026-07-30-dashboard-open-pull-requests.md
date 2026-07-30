# ADR: Dashboard Open Pull Requests
**Date:** 2026-07-30  **Status:** Proposed  **PR:** [dashboard-api#2](https://github.com/skarumbu/dashboard_api/pull/2)

## Context
The dashboard's goal is to be a single page that answers "is everything okay across all my services" without having to check each repo individually. Today that's scoped narrowly to uptime/health and the latest CI run — it answers "are my APIs healthy" but not "do any of my APIs need something from me." Open pull requests are exactly that kind of pending action item: a PR sitting open is something the owner eventually has to look at, independent of whether the service itself is healthy.

Surfacing open PRs alongside health and CI status shifts the question the dashboard answers from "what's wrong with my APIs" to "is there anything across my APIs that needs action" — a broader, more useful framing for a single-pane-of-glass view.

## Decision
Open pull requests are fetched live, per app, on every `DashboardGetter` request — the same pattern already used for GitHub Actions data in this file (`fetch_github_run`). The fetch runs in parallel via the existing `ThreadPoolExecutor` alongside health/metrics/cost/Actions calls, and gracefully degrades to an empty result if `GITHUB_TOKEN` is missing or the API call fails.

This keeps a single mental model for "GitHub-derived signal" in this service: Actions and PRs are fetched identically, rather than one being cached and the other live for no reason other than implementation order. If GitHub API latency or rate limits ever become a real problem, both should move to the same caching strategy together — see Alternatives Considered.

### API call

`fetch_open_prs(repo)` in `github_checks.py` makes a single call:

```
GET https://api.github.com/repos/{owner}/{repo}/pulls?state=open&per_page=20
Accept: application/vnd.github+json
X-GitHub-Api-Version: 2022-11-28
Authorization: Bearer {GITHUB_TOKEN}
```

- `timeout=10` seconds, same as every other GitHub call in this module.
- `GITHUB_TOKEN` is a single shared classic/fine-grained PAT read from the Function App's environment — the same token, and the same `_HEADERS` base dict, already used by `fetch_github_run`. No per-repo tokens, no GitHub App/installation auth.
- Response items are trimmed to the fields the frontend needs: `number`, `title`, `user.login` (as `author`), `draft`, `created_at`, `html_url`. No pagination beyond the first page (`per_page=20`) — open PR counts are expected to stay small for these repos.
- On any failure (missing token, missing `repo`, non-2xx response, network exception) the function returns `[]` and logs via the module logger (`dashboard-api.github`) — identical graceful-degradation contract to `fetch_github_run`, just list-shaped instead of dict-shaped.

This is a direct structural copy of `fetch_github_run`, which calls `GET /repos/{repo}/actions/runs?per_page=10&exclude_pull_requests=true` (plus a conditional second call to `/actions/runs/{run_id}/jobs` for failed-job names when the latest run failed). `fetch_open_prs` only needs the one call — the pulls list endpoint already returns everything required per PR — so there's no equivalent second request.

## Alternatives Considered
- **In-memory cache with TTL** — the pattern `_get_cost()` already uses for Cost Management (1h TTL, manual `_cost_cache` dict). Would cut GitHub API calls to once per TTL window regardless of frontend poll frequency, at the cost of staleness. Not adopted now because GitHub Actions data — the precedent this feature follows — isn't cached either; caching only PRs would introduce two different staleness models for the same class of GitHub-derived data. Worth revisiting for both together if rate limits or latency become an actual issue.
- **Background timer job + durable cache** (e.g. a timer-triggered function writing PR data to Table Storage): fully decouples GitHub API call volume from viewer traffic. Rejected as over-engineering at the current scale — a handful of registered repos and effectively one viewer, nowhere near GitHub's 5,000 req/hr authenticated rate limit — and it adds real infrastructure (timer trigger, storage schema, staleness window) for a problem that doesn't exist yet.
- **Do nothing:** rejected — see Context.

## Consequences
**Positive:**  
- The dashboard now surfaces pending action items (open PRs), not just health/uptime signal — moving it closer to a true single-pane-of-glass for "what needs my attention."  
- Reuses the existing per-app `github_repo` field and parallel-fetch pattern established for GitHub Actions, so this generalizes cleanly to future GitHub-derived signals (e.g. open issues) without new plumbing.  

**Trade-offs:**  
- Additional GitHub API calls per app on every `DashboardGetter` request (one per app with `github_repo` set), running in parallel alongside existing calls.  
- Dependency on GitHub API availability and rate limits — degrades gracefully (empty result) rather than failing the whole response.  

## Relevant Code
- [`function_app.py`](https://github.com/skarumbu/dashboard_api/blob/master/function_app.py)  
- [`github_checks.py`](https://github.com/skarumbu/dashboard_api/blob/master/github_checks.py)
