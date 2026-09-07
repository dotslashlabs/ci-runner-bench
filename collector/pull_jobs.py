#!/usr/bin/env python3
"""Pull job and step timings from the GitHub Actions API into a CSV.

Standard library only, so no virtual environment is needed.

Environment:
  GITHUB_TOKEN  a token with actions:read on the benchmark repository
  BENCH_REPO    owner/name of the benchmark repository

Usage:
  GITHUB_TOKEN=... BENCH_REPO=myorg/ci-runner-bench python3 collector/pull_jobs.py
  python3 collector/pull_jobs.py --since 2026-09-05T00:00:00Z
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

API = "https://api.github.com"
WORKFLOWS = ("arm-a.yml", "arm-b.yml", "burst.yml", "seed.yml")

JOB_FIELDS = [
    "workflow", "run_id", "run_attempt", "job_id", "job_name",
    "arm", "vendor", "workload", "cache_arm",
    "status", "conclusion",
    "created_at", "started_at", "completed_at",
    "queue_time_s", "job_duration_s", "billable_min",
    "runner_name", "runner_group",
]

STEP_FIELDS = [
    "run_id", "job_id", "vendor", "workload", "cache_arm",
    "step_number", "step_name", "step_conclusion", "step_duration_s",
]


def request(url: str, token: str, retries: int = 4) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "ci-runner-bench-collector",
        },
    )

    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as err:
            if err.code in (403, 429) and attempt < retries - 1:
                wait = 2 ** (attempt + 3)
                print(f"  rate limited, waiting {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            raise
        except urllib.error.URLError:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise

    raise RuntimeError(f"gave up on {url}")


def paginate(url: str, token: str, key: str):
    page = 1
    while True:
        sep = "&" if "?" in url else "?"
        data = request(f"{url}{sep}per_page=100&page={page}", token)
        items = data.get(key, [])
        if not items:
            return
        yield from items
        if len(items) < 100:
            return
        page += 1


def parse_ts(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=dt.timezone.utc
    )


def seconds(a: str | None, b: str | None) -> float | None:
    """Seconds from a to b, or None when either timestamp is missing."""
    start, end = parse_ts(a), parse_ts(b)
    if start is None or end is None:
        return None
    return round((end - start).total_seconds(), 1)


def split_job_name(name: str) -> tuple[str, str, str, str]:
    """Job names are 'arm|vendor|workload|cache_arm'. Anything else is unknown."""
    parts = name.split("|")
    if len(parts) == 4:
        return parts[0], parts[1], parts[2], parts[3]
    if len(parts) == 3:
        return parts[0], parts[1], parts[2], "na"
    return "unknown", "unknown", "unknown", "na"


def collect(repo: str, token: str, since: dt.datetime | None):
    jobs_out: list[dict] = []
    steps_out: list[dict] = []

    for wf in WORKFLOWS:
        url = f"{API}/repos/{repo}/actions/workflows/{wf}/runs"
        try:
            runs = list(paginate(url, token, "workflow_runs"))
        except urllib.error.HTTPError as err:
            if err.code == 404:
                print(f"{wf}: not found, skipping", file=sys.stderr)
                continue
            raise

        print(f"{wf}: {len(runs)} runs", file=sys.stderr)

        for run in runs:
            created = parse_ts(run["created_at"])
            if since and created and created < since:
                continue

            jurl = f"{API}/repos/{repo}/actions/runs/{run['id']}/jobs"
            for job in paginate(jurl, token, "jobs"):
                arm, vendor, workload, cache_arm = split_job_name(job["name"])
                duration = seconds(job.get("started_at"), job.get("completed_at"))

                jobs_out.append({
                    "workflow": wf,
                    "run_id": run["id"],
                    "run_attempt": run.get("run_attempt", 1),
                    "job_id": job["id"],
                    "job_name": job["name"],
                    "arm": arm,
                    "vendor": vendor,
                    "workload": workload,
                    "cache_arm": cache_arm,
                    "status": job.get("status"),
                    "conclusion": job.get("conclusion"),
                    "created_at": job.get("created_at"),
                    "started_at": job.get("started_at"),
                    "completed_at": job.get("completed_at"),
                    "queue_time_s": seconds(job.get("created_at"),
                                            job.get("started_at")),
                    "job_duration_s": duration,
                    "billable_min": (-(-duration // 60) if duration else None),
                    "runner_name": job.get("runner_name"),
                    "runner_group": job.get("runner_group_name"),
                })

                for step in job.get("steps") or []:
                    steps_out.append({
                        "run_id": run["id"],
                        "job_id": job["id"],
                        "vendor": vendor,
                        "workload": workload,
                        "cache_arm": cache_arm,
                        "step_number": step.get("number"),
                        "step_name": step.get("name"),
                        "step_conclusion": step.get("conclusion"),
                        "step_duration_s": seconds(step.get("started_at"),
                                                   step.get("completed_at")),
                    })

    return jobs_out, steps_out


def write_csv(path: str, rows: list[dict], fields: list[str]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {len(rows)} rows to {path}", file=sys.stderr)


def resolve_token() -> str | None:
    """Token from the environment, else from the gh CLI.

    Falling back to `gh auth token` keeps the credential out of shell history
    and out of any command line.
    """
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        return token

    try:
        out = subprocess.run(["gh", "auth", "token"], capture_output=True,
                             text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None

    return out.stdout.strip() or None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=os.environ.get("BENCH_REPO"))
    ap.add_argument("--since", help="ISO instant, e.g. 2026-09-05T00:00:00Z")
    ap.add_argument("--out-dir", default="results/raw")
    args = ap.parse_args()

    token = resolve_token()
    if not token:
        print("No token. Set GITHUB_TOKEN or run: gh auth login",
              file=sys.stderr)
        return 2
    if not args.repo:
        print("pass --repo or set BENCH_REPO", file=sys.stderr)
        return 2

    since = parse_ts(args.since) if args.since else None
    jobs, steps = collect(args.repo, token, since)

    write_csv(os.path.join(args.out_dir, "jobs.csv"), jobs, JOB_FIELDS)
    write_csv(os.path.join(args.out_dir, "steps.csv"), steps, STEP_FIELDS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
