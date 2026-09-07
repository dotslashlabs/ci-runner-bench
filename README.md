# ci-runner-bench

A benchmark that measures third-party GitHub Actions runners against
GitHub-hosted runners on the same commit, the same commands and the same vCPU
count. GitHub-hosted `ubuntu-24.04` is the baseline.

Runners under test:

| Vendor | Label |
| --- | --- |
| GitHub (baseline) | `ubuntu-24.04` |
| Blacksmith | `blacksmith-4vcpu-ubuntu-2404` |

Both runners use 4 vCPU. Namespace and WarpBuild are out of scope.

## How it works

One workflow run fans out across every runner, so all vendors face the same
time of day, registry load and network state. Only the runner label changes.

- `seed.yml` populates the warm caches. Run it once before collection.
- `arm-a.yml` is the drop-in path. Every runner uses `actions/cache`.
- `arm-b.yml` is the tuned path. Each vendor uses its own cache action.
- `burst.yml` starts 20 simultaneous jobs per runner to measure queue time.

The gap between arm A and arm B measures how much developer effort a vendor
needs before its published claims hold.

## Workloads

All workloads are public code pinned to a fixed commit in `workloads.lock`.

| ID | Workload |
| --- | --- |
| W1 | excalidraw: yarn install, typecheck, unit tests |
| W2 | ripgrep: Cargo release build and test |
| W4 | Multi-stage Docker build with a dependency layer and a churn layer |
| W5 | Short Python job, about 60 to 90 seconds, no network |
| T1 to T5 | sysbench CPU, fio disk, 500 MB download, 1 GB cache round trip |

## Analysis

```bash
export GITHUB_TOKEN=<token with actions:read>
export BENCH_REPO=dotslashlabs/ci-runner-bench
python3 collector/pull_jobs.py
python3 collector/analyze.py
cat results/tables/verdicts.md
```

Both scripts use the Python standard library only. The analysis reports medians,
95th percentiles and a bootstrap 95% confidence interval on every ratio. A
sample counts only if the job succeeded.

## Setup

See [SETUP.md](SETUP.md) for the runbook, the free-tier budget and the known
limits.

## Scope

This repository contains no private code, no credentials and no data from any
employer. Every input is open-source code named in `workloads.lock`.
