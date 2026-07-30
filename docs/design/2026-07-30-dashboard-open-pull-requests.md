# ADR: Dashboard Open Pull Requests
**Date:** 2026-07-30  **Status:** Proposed  **PR:** [dashboard-api#2](https://github.com/skarumbu/dashboard_api/pull/2)

## Context
The dashboard API lacked visibility into repository activity, specifically open pull requests, which are critical for tracking development progress. This feature introduces integration with the GitHub Pulls API to include open PRs in the API response, enhancing user insights into repository activity.

## Decision
The API will fetch and include open pull requests from GitHub repositories in the dashboard response. This decision leverages the GitHub Pulls API to provide key PR metadata (e.g., title, author, creation date) while ensuring graceful degradation in case of API errors or missing tokens.

## Alternatives Considered
- **Do nothing:** Rejected because users lack visibility into repository activity, reducing the dashboard's utility.
- **Use a webhook-based approach:** Rejected due to increased complexity and maintenance overhead compared to polling the GitHub API.
- **Limit PR metadata:** Rejected as it would reduce the feature's usefulness for users tracking PR details.

## Consequences
**Positive:**  
- Enhanced visibility into repository activity for users.  
- Improved user experience with actionable insights into open pull requests.  

**Trade-offs:**  
- Increased API call overhead to GitHub, potentially impacting performance.  
- Dependency on GitHub API availability and rate limits.  

## Relevant Code
- [`function_app.py`](https://github.com/skarumbu/dashboard_api/blob/master/function_app.py)  
- [`github_checks.py`](https://github.com/skarumbu/dashboard_api/blob/master/github_checks.py)
