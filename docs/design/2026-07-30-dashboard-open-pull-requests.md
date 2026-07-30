# ADR: Dashboard Open Pull Requests
**Date:** 2026-07-30  **Status:** Proposed  **PR:** [dashboard-api#3](https://github.com/skarumbu/dashboard_api/pull/3)

## Context
The dashboard API lacked visibility into open pull requests, which are critical for tracking repository activity and development progress. This feature integrates the GitHub Pulls API to fetch and include open PR metadata (e.g., title, author, creation date) in the dashboard response, enhancing user insights.

## Decision
The API will fetch open pull requests from GitHub repositories and include them in the dashboard response. This approach ensures actionable insights for users while gracefully handling API errors or missing tokens to maintain reliability.

## Alternatives Considered
- **Do nothing:** Rejected because users lack visibility into repository activity, reducing the dashboard's utility.
- **Use a webhook-based approach:** Rejected due to increased complexity and maintenance overhead compared to polling the GitHub API.

## Consequences
**Positive:**  
- Enhanced visibility into repository activity for users.  
- Improved user experience with actionable insights into open pull requests.  

**Trade-offs:**  
- Increased API call overhead to GitHub, potentially impacting performance.  
- Dependency on GitHub API availability and rate limits.  

## Relevant Code
- [`function_app.py`](https://github.com/skarumbu/dashboard_api/blob/main/function_app.py)  
- [`github_checks.py`](https://github.com/skarumbu/dashboard_api/blob/main/github_checks.py)
