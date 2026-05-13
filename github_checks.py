import logging
import os

import requests

logger = logging.getLogger("dashboard-api.github")

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


def fetch_github_run(repo: str) -> dict:
    """Return the latest non-PR workflow run for `repo` (format: owner/repo).

    Returns an empty dict if GITHUB_TOKEN is unset, the repo has no runs,
    or any API error occurs — callers degrade gracefully.
    """
    if not GITHUB_TOKEN or not repo:
        return {}

    headers = {**_HEADERS, "Authorization": f"Bearer {GITHUB_TOKEN}"}

    try:
        resp = requests.get(
            f"https://api.github.com/repos/{repo}/actions/runs",
            headers=headers,
            params={"per_page": 5, "exclude_pull_requests": "true"},
            timeout=10,
        )
        if not resp.ok:
            logger.warning(f"GitHub Actions API {resp.status_code} for {repo}")
            return {}

        runs = resp.json().get("workflow_runs", [])
        if not runs:
            return {}

        run = runs[0]
        result = {
            "repo": repo,
            "run_id": run["id"],
            "workflow_name": run["name"],
            "status": run["status"],        # queued | in_progress | completed
            "conclusion": run["conclusion"], # success | failure | cancelled | skipped | None
            "branch": run["head_branch"],
            "created_at": run["created_at"],
            "html_url": run["html_url"],
        }

        if run["conclusion"] == "failure":
            jobs_resp = requests.get(
                f"https://api.github.com/repos/{repo}/actions/runs/{run['id']}/jobs",
                headers=headers,
                timeout=10,
            )
            if jobs_resp.ok:
                failed = [
                    j["name"]
                    for j in jobs_resp.json().get("jobs", [])
                    if j.get("conclusion") == "failure"
                ]
                result["failed_jobs"] = failed

        return result

    except Exception as exc:
        logger.error(f"fetch_github_run({repo}) failed: {exc}")
        return {}
