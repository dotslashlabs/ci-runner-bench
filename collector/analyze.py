#!/usr/bin/env python3
"""Turn the collected CSVs into ratio tables and a claim verdict table.

Standard library only. GitHub-hosted runners are the baseline for every ratio.
A ratio above 1.0 means the vendor is faster or cheaper than GitHub.

Speed is measured on the "Run workload" step. Cost is measured on whole-job
duration, because that is what vendors bill. The gap between the two is
reported separately as setup overhead.

Usage:
  python3 collector/analyze.py
  python3 collector/analyze.py --raw results/raw --out results/tables
"""
from __future__ import annotations

import argparse
import csv
import os
import random
import statistics as st
import sys
from collections import defaultdict

BASELINE = "github"
VENDORS = ["blacksmith"]
BOOTSTRAP = 10_000
PAYLOAD_MB = 512

# USD per minute, 4 vCPU Linux x64. Fetched 2026-09-04. Re-check before
# publishing. GitHub is priced as a private repo on a larger runner, which is
# the buyer's situation. The public-repo view prices GitHub at zero.
PRICE_PER_MIN = {
    "github": 0.012,
    "blacksmith": 0.008,
    "namespace": 0.006,
    "warpbuild": 0.008,
}

# claim id -> (vendor, human claim, metric key, comparison, threshold)
# comparison: "ratio_ge" needs ratio >= threshold
#             "abs_le"   needs the absolute value <= threshold
#             "abs_ge"   needs the absolute value >= threshold
#             "report"   no threshold, record the number
CLAIMS = [
    ("B1", "blacksmith", "2x faster than GitHub's runners, monorepo warm",
     "speedup:w1:warm", "ratio_ge", 1.8),
    ("B1b", "blacksmith", "2x faster than GitHub's runners, Rust build",
     "speedup:w2:warm", "ratio_ge", 1.8),
    ("B1c", "blacksmith", "2x faster, monorepo cold",
     "speedup:w1:cold", "ratio_ge", 1.8),
    ("B2", "blacksmith", "4x faster cache downloads, 400MB/s",
     "cache_restore_mbps", "abs_ge", 350.0),
    ("B2r", "blacksmith", "4x faster cache downloads, ratio against GitHub",
     "speedup:cache_restore", "ratio_ge", 3.5),
    ("B3", "blacksmith", "2x to 40x faster Docker builds",
     "speedup:w4:warm", "ratio_ge", 2.0),
    ("B4", "blacksmith", "Runners boot in under 3 seconds, median",
     "queue_p50", "abs_le", 3.0),
    ("B4b", "blacksmith", "Runners boot quickly under load, 95th percentile",
     "queue_p95", "abs_le", 10.0),
    ("B5", "blacksmith", "67% total cost savings",
     "cost_ratio:w1:warm", "ratio_ge", 3.0),
    ("B6", "blacksmith", "Lightweight jobs may gain little or become slower",
     "speedup:w5:cold", "report", None),
    ("E1", "blacksmith", "End-to-end wait including queue, monorepo warm",
     "e2e_speedup:w1:warm", "ratio_ge", 1.8),
    ("E2", "blacksmith", "End-to-end wait including queue, Rust build",
     "e2e_speedup:w2:warm", "ratio_ge", 1.8),
    ("E3", "blacksmith", "End-to-end wait including queue, short job",
     "e2e_speedup:w5:cold", "report", None),
]

# Claims recorded for every vendor with no pass threshold.
REPORT_ONLY = [
    ("X1", "Lightweight jobs may gain little or become slower",
     "speedup:w5:cold"),
    ("X2", "Datawrapper reported 22% faster and 45% cheaper",
     "speedup_mean"),
]


def read_csv(path: str) -> list[dict]:
    if not os.path.exists(path):
        print(f"missing {path}", file=sys.stderr)
        return []
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


def as_float(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "", "None") else None
    except ValueError:
        return None


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(int(round(q * (len(ordered) - 1))), len(ordered) - 1)
    return ordered[idx]


def bootstrap_ratio_ci(base: list[float], vendor: list[float],
                       rounds: int = BOOTSTRAP) -> tuple[float, float] | None:
    """95% CI for median(base) / median(vendor)."""
    if len(base) < 3 or len(vendor) < 3:
        return None

    rng = random.Random(20260904)
    ratios = []
    for _ in range(rounds):
        b = st.median(rng.choices(base, k=len(base)))
        v = st.median(rng.choices(vendor, k=len(vendor)))
        if v > 0:
            ratios.append(b / v)

    if not ratios:
        return None
    return percentile(ratios, 0.025), percentile(ratios, 0.975)


def workload_step_seconds(steps: list[dict]) -> dict[str, float]:
    """Map job id to the duration of its "Run workload" step.

    Speed claims are measured on this step, not on the whole job. Checkout,
    toolchain setup and cache restore are fixed overheads that differ between
    runner images. On a short workload they can be half the job, which dilutes
    a real CPU difference. Cost still uses whole-job duration, because that is
    what every vendor bills.
    """
    out: dict[str, float] = {}
    for row in steps:
        if (row["step_name"] or "").strip() != "Run workload":
            continue
        seconds = as_float(row["step_duration_s"])
        if seconds is not None and row["step_conclusion"] == "success":
            out[row["job_id"]] = seconds
    return out


def paired_runs(jobs: list[dict]) -> set:
    """Run ids where the baseline and at least one vendor both succeeded.

    Vendors are activated at different times, so an unpaired slot would
    compare a GitHub evening measurement against a vendor night measurement.
    Restricting to paired slots keeps the same-wall-clock control intact.
    """
    seen: dict[str, set] = defaultdict(set)
    for row in jobs:
        if row["arm"] in ("a", "b") and row["conclusion"] == "success":
            seen[row["run_id"]].add(row["vendor"])

    return {r for r, v in seen.items()
            if BASELINE in v and len(v - {BASELINE}) > 0}


def gather(jobs: list[dict], steps: list[dict]) -> dict:
    """Build the metric store: metric key -> vendor -> list of samples."""
    store: dict[str, dict[str, list[float]]] = defaultdict(
        lambda: defaultdict(list))
    # pairs[metric key][run id][vendor] = value, for within-slot ratios.
    pairs: dict = defaultdict(lambda: defaultdict(dict))
    work_s = workload_step_seconds(steps)
    paired = paired_runs(jobs)
    store["unpaired"]["_"] = []

    for row in jobs:
        vendor, workload = row["vendor"], row["workload"]
        arm, cache_arm = row["arm"], row["cache_arm"]

        # Correctness gate. A faster runner that fails is not faster.
        if row["conclusion"] != "success":
            store["dropped"][vendor].append(1.0)
            continue

        duration = as_float(row["job_duration_s"])
        queue = as_float(row["queue_time_s"])

        if arm == "burst" and queue is not None:
            store["queue"][vendor].append(queue)
            continue

        if arm == "a" and queue is not None:
            store["queue_a"][vendor].append(queue)

        # Skip slots that ran only one runner. Those timings are real but not
        # comparable, because nothing controls for time of day.
        if arm in ("a", "b") and row["run_id"] not in paired:
            store["unpaired"]["_"].append(1.0)
            continue

        if duration is None:
            continue

        # Prefer the workload step. Fall back to the job when the step is
        # missing, for example on a job that failed before it ran.
        work = work_s.get(row["job_id"], duration)

        if arm == "a" and workload in ("w1", "w2", "w4", "w5"):
            key = f"dur:{workload}:{cache_arm}"
            store[key][vendor].append(work)
            store[f"job:{workload}:{cache_arm}"][vendor].append(duration)
            pairs[key][row["run_id"]][vendor] = work

            if queue is not None:
                e2e = queue + duration
                store[f"e2e:{workload}:{cache_arm}"][vendor].append(e2e)
                pairs[f"e2e:{workload}:{cache_arm}"][row["run_id"]][vendor] = e2e

            billable = -(-duration // 60)
            cost = billable * PRICE_PER_MIN.get(vendor, 0.0)
            store[f"cost:{workload}:{cache_arm}"][vendor].append(cost)
            pairs[f"cost:{workload}:{cache_arm}"][row["run_id"]][vendor] = cost

        if arm == "b" and workload in ("w1", "w2"):
            store[f"durB:{workload}:{cache_arm}"][vendor].append(work)

    for row in steps:
        name = (row["step_name"] or "").strip()
        vendor = row["vendor"]
        seconds = as_float(row["step_duration_s"])
        if seconds is None or row["step_conclusion"] != "success":
            continue

        if name == "Restore cache payload":
            pairs["cache_restore_s"][row["run_id"]][vendor] = seconds
            store["cache_restore_s"][vendor].append(seconds)
            if seconds > 0:
                store["cache_restore_mbps"][vendor].append(PAYLOAD_MB / seconds)
        elif name in ("Save cache payload",
                      "Post Seed the cache-throughput payload"):
            store["cache_save_s"][vendor].append(seconds)
            if seconds > 0:
                store["cache_save_mbps"][vendor].append(PAYLOAD_MB / seconds)
        elif name == "Pull large image":
            store["image_pull_s"][vendor].append(seconds)

    return store, pairs


def median_of(store: dict, key: str, vendor: str) -> float | None:
    values = store.get(key, {}).get(vendor, [])
    return st.median(values) if values else None


def per_slot_ratios(pairs: dict, key: str, vendor: str) -> list[float]:
    """GitHub divided by vendor, computed inside each slot then collected.

    Pairing inside a slot removes anything that moved both runners together,
    such as registry latency or time of day. It is the correct estimator here
    because every slot ran both runners on the same commit.
    """
    out = []
    for _run, byvendor in pairs.get(key, {}).items():
        base, vend = byvendor.get(BASELINE), byvendor.get(vendor)
        if base and vend and vend > 0:
            out.append(base / vend)
    return out


def bootstrap_ci(values: list[float],
                 rounds: int = BOOTSTRAP) -> tuple[float, float] | None:
    """95% CI for the median of a single sample."""
    if len(values) < 3:
        return None

    rng = random.Random(20260907)
    meds = [st.median(rng.choices(values, k=len(values)))
            for _ in range(rounds)]
    return percentile(meds, 0.025), percentile(meds, 0.975)


def resolve(store: dict, metric: str, vendor: str, pairs: dict | None = None) -> tuple:
    """Return (value, n, ci) for a metric key such as 'speedup:w1:warm'."""
    if metric.startswith("e2e_speedup:"):
        key = "e2e:" + metric.split("e2e_speedup:", 1)[1]
        if pairs is not None:
            ratios = per_slot_ratios(pairs, key, vendor)
            if ratios:
                return st.median(ratios), len(ratios), bootstrap_ci(ratios)
        return None, 0, None

    if metric.startswith("speedup:"):
        target = metric.split("speedup:", 1)[1]
        key = "cache_restore_s" if target == "cache_restore" else f"dur:{target}"

        if pairs is not None:
            ratios = per_slot_ratios(pairs, key, vendor)
            if ratios:
                return st.median(ratios), len(ratios), bootstrap_ci(ratios)

        base = store.get(key, {}).get(BASELINE, [])
        vend = store.get(key, {}).get(vendor, [])
        if not base or not vend:
            return None, 0, None
        return (st.median(base) / st.median(vend),
                len(vend),
                bootstrap_ratio_ci(base, vend))

    if metric.startswith("cost_ratio:"):
        target = metric.split("cost_ratio:", 1)[1]
        key = f"cost:{target}"

        if pairs is not None:
            ratios = per_slot_ratios(pairs, key, vendor)
            if ratios:
                return st.median(ratios), len(ratios), bootstrap_ci(ratios)

        base = store.get(key, {}).get(BASELINE, [])
        vend = store.get(key, {}).get(vendor, [])
        if not base or not vend:
            return None, 0, None
        vm = st.median(vend)
        return (st.median(base) / vm if vm > 0 else None,
                len(vend),
                bootstrap_ratio_ci(base, vend))

    if metric == "queue_p50":
        # Prefer the dedicated burst test. Fall back to slot queue times,
        # where each slot already starts about ten jobs per runner at once.
        values = (store.get("queue", {}).get(vendor, [])
                  or store.get("queue_a", {}).get(vendor, []))
        return percentile(values, 0.5), len(values), None

    if metric == "queue_p95":
        values = (store.get("queue", {}).get(vendor, [])
                  or store.get("queue_a", {}).get(vendor, []))
        return percentile(values, 0.95), len(values), None

    if metric == "speedup_mean":
        ratios = []
        for wl, arm in (("w1", "warm"), ("w2", "warm"),
                        ("w4", "warm"), ("w5", "cold")):
            b = median_of(store, f"dur:{wl}:{arm}", BASELINE)
            v = median_of(store, f"dur:{wl}:{arm}", vendor)
            if b and v:
                ratios.append(b / v)
        return (st.mean(ratios) if ratios else None, len(ratios), None)

    values = store.get(metric, {}).get(vendor, [])
    return (st.median(values) if values else None, len(values), None)


def verdict(comparison: str | None, value: float | None,
            threshold: float | None, ci: tuple | None) -> str:
    if value is None:
        return "No data"
    if comparison in (None, "report"):
        return "Reported"

    if comparison == "ratio_ge":
        if value < threshold:
            return "Not supported"
        return "Supported" if ci and ci[0] >= 1.0 else "Supported, wide CI"
    if comparison == "abs_le":
        return "Supported" if value <= threshold else "Not supported"
    if comparison == "abs_ge":
        return "Supported" if value >= threshold else "Not supported"
    return "Unknown check"


def fmt(value: float | None, digits: int = 2) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def write_verdicts(store: dict, out_dir: str, pairs: dict) -> None:
    lines = [
        "# Claim verdicts",
        "",
        "GitHub-hosted `ubuntu-24.04` is the baseline. A ratio above 1.0 means",
        "the vendor beats GitHub. Speed and cost ratios are computed inside",
        "each slot and then aggregated, so anything that moved both runners",
        "together cancels. CI is a 95% bootstrap interval, and n is the number",
        "of paired slots.",
        "",
        "| ID | Vendor | Claim | Metric | Measured | Threshold | 95% CI | n | Verdict |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]

    for cid, vendor, claim, metric, comparison, threshold in CLAIMS:
        value, n, ci = resolve(store, metric, vendor, pairs)
        ci_text = f"{fmt(ci[0])} to {fmt(ci[1])}" if ci else "n/a"
        lines.append(
            f"| {cid} | {vendor} | {claim} | `{metric}` | {fmt(value)} | "
            f"{fmt(threshold) if threshold else 'report'} | {ci_text} | {n} | "
            f"{verdict(comparison, value, threshold, ci)} |"
        )

    lines += ["", "## Report-only observations", "",
              "| ID | Observation | Metric | " +
              " | ".join(VENDORS) + " |",
              "| --- | --- | --- | " + " | ".join("---" for _ in VENDORS) + " |"]

    for cid, text, metric in REPORT_ONLY:
        cells = [fmt(resolve(store, metric, v, pairs)[0]) for v in VENDORS]
        lines.append(f"| {cid} | {text} | `{metric}` | " + " | ".join(cells) + " |")

    skipped = len(store.get("unpaired", {}).get("_", []))
    lines += ["", "## Sampling", "",
              f"Unpaired job rows skipped: {skipped}. A slot counts only when "
              "the baseline and a vendor both ran in it, so every ratio "
              "compares the same wall-clock window.",
              "", "## Reliability", "",
              "| Vendor | Failed or cancelled jobs |",
              "| --- | --- |"]
    for vendor in [BASELINE] + VENDORS:
        lines.append(f"| {vendor} | {len(store.get('dropped', {}).get(vendor, []))} |")

    path = os.path.join(out_dir, "verdicts.md")
    with open(path, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"wrote {path}")


def write_durations(store: dict, out_dir: str) -> None:
    lines = ["# Job duration by runner", "",
             "Median seconds, with the sample count in brackets.", "",
             "| Workload | Cache | " + " | ".join([BASELINE] + VENDORS) +
             " | best ratio |",
             "| --- | --- | " + " | ".join("---" for _ in range(len(VENDORS) + 2))
             + " |"]

    for workload in ("w1", "w2", "w4", "w5"):
        for arm in ("cold", "warm"):
            key = f"dur:{workload}:{arm}"
            if not store.get(key):
                continue

            cells, ratios = [], []
            base = median_of(store, key, BASELINE)
            for vendor in [BASELINE] + VENDORS:
                values = store[key].get(vendor, [])
                med = st.median(values) if values else None
                cells.append(f"{fmt(med, 1)} ({len(values)})")
                if vendor != BASELINE and base and med:
                    ratios.append(base / med)

            best = f"{max(ratios):.2f}x" if ratios else "n/a"
            lines.append(f"| {workload} | {arm} | " + " | ".join(cells) +
                         f" | {best} |")

    lines += ["", "# Setup overhead", "",
              "Whole job minus the workload step. A vendor with a heavier",
              "runner image pays this on every job regardless of CPU speed.", "",
              "| Workload | Cache | " + " | ".join([BASELINE] + VENDORS) + " |",
              "| --- | --- | " + " | ".join("---" for _ in range(len(VENDORS) + 1))
              + " |"]

    for workload in ("w1", "w2", "w4", "w5"):
        for arm in ("cold", "warm"):
            if not store.get(f"dur:{workload}:{arm}"):
                continue
            cells = []
            for vendor in [BASELINE] + VENDORS:
                job = median_of(store, f"job:{workload}:{arm}", vendor)
                work = median_of(store, f"dur:{workload}:{arm}", vendor)
                cells.append(f"{job - work:.0f}s" if job and work else "n/a")
            lines.append(f"| {workload} | {arm} | " + " | ".join(cells) + " |")

    lines += ["", "# End-to-end wait, queue included", "",
              "Median seconds from job creation to completion. This is what a",
              "developer or an agent actually waits for.", "",
              "| Workload | Cache | " + " | ".join([BASELINE] + VENDORS) +
              " | ratio |",
              "| --- | --- | " + " | ".join("---" for _ in range(len(VENDORS) + 2))
              + " |"]

    for workload in ("w1", "w2", "w4", "w5"):
        for arm in ("cold", "warm"):
            key = f"e2e:{workload}:{arm}"
            if not store.get(key):
                continue
            base = median_of(store, key, BASELINE)
            cells = []
            for vendor in [BASELINE] + VENDORS:
                cells.append(fmt(median_of(store, key, vendor), 1))
            v = median_of(store, key, VENDORS[0])
            ratio = f"{base / v:.2f}x" if base and v else "n/a"
            lines.append(f"| {workload} | {arm} | " + " | ".join(cells) +
                         f" | {ratio} |")

    # Break-even: the baseline job duration at which the vendor's execution
    # advantage exactly repays its extra provisioning latency.
    #   base_queue + d = vendor_queue + d / r   =>   d = dq / (1 - 1/r)
    gq = percentile(store.get("queue_a", {}).get(BASELINE, []), 0.5)
    lines += ["", "# Break-even job length", "",
              "Below this baseline job duration the extra provisioning latency",
              "outweighs the faster execution, so the vendor is a regression.", "",
              "| Vendor | queue p50 | queue penalty | exec speedup | break-even |",
              "| --- | --- | --- | --- | --- |"]

    for vendor in VENDORS:
        vq = percentile(store.get("queue_a", {}).get(vendor, []), 0.5)
        ratios = []
        for wl, arm in (("w1", "warm"), ("w2", "warm"), ("w4", "warm")):
            b = median_of(store, f"dur:{wl}:{arm}", BASELINE)
            v = median_of(store, f"dur:{wl}:{arm}", vendor)
            if b and v:
                ratios.append(b / v)

        r = st.median(ratios) if ratios else None
        if gq is None or vq is None or not r or r <= 1:
            lines.append(f"| {vendor} | {fmt(vq, 1)} | n/a | {fmt(r)} | n/a |")
            continue

        dq = vq - gq
        be = dq / (1 - 1 / r)
        lines.append(f"| {vendor} | {vq:.1f}s | {dq:+.1f}s | {r:.2f}x | "
                     f"{be:.0f}s |")

    lines += ["", "# Cache and boot metrics", "",
              "| Metric | " + " | ".join([BASELINE] + VENDORS) + " |",
              "| --- | " + " | ".join("---" for _ in range(len(VENDORS) + 1)) + " |"]

    for metric, digits in (("cache_restore_s", 1), ("cache_restore_mbps", 0),
                           ("cache_save_s", 1), ("cache_save_mbps", 0),
                           ("image_pull_s", 1)):
        cells = [fmt(median_of(store, metric, v), digits)
                 for v in [BASELINE] + VENDORS]
        lines.append(f"| {metric} | " + " | ".join(cells) + " |")

    queue_cells_50, queue_cells_95 = [], []
    for vendor in [BASELINE] + VENDORS:
        values = (store.get("queue", {}).get(vendor, [])
                  or store.get("queue_a", {}).get(vendor, []))
        queue_cells_50.append(fmt(percentile(values, 0.5), 1))
        queue_cells_95.append(fmt(percentile(values, 0.95), 1))
    lines.append("| queue_p50_s | " + " | ".join(queue_cells_50) + " |")
    lines.append("| queue_p95_s | " + " | ".join(queue_cells_95) + " |")

    lines += ["", "# Arm A versus arm B", "",
              "Vendor cache action against actions/cache on the same runner.",
              "A ratio above 1.0 means the vendor action is faster.", "",
              "| Vendor | Workload | arm A warm | arm B warm | ratio |",
              "| --- | --- | --- | --- | --- |"]

    for vendor in VENDORS:
        for workload in ("w1", "w2"):
            a = median_of(store, f"dur:{workload}:warm", vendor)
            b = median_of(store, f"durB:{workload}:warm", vendor)
            ratio = f"{a / b:.2f}" if a and b else "n/a"
            lines.append(f"| {vendor} | {workload} | {fmt(a, 1)} | "
                         f"{fmt(b, 1)} | {ratio} |")

    path = os.path.join(out_dir, "durations.md")
    with open(path, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"wrote {path}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", default="results/raw")
    ap.add_argument("--out", default="results/tables")
    args = ap.parse_args()

    jobs = read_csv(os.path.join(args.raw, "jobs.csv"))
    steps = read_csv(os.path.join(args.raw, "steps.csv"))
    if not jobs:
        print("no job rows, run pull_jobs.py first", file=sys.stderr)
        return 1

    os.makedirs(args.out, exist_ok=True)
    store, pairs = gather(jobs, steps)

    write_verdicts(store, args.out, pairs)
    write_durations(store, args.out)
    print(f"analysed {len(jobs)} jobs and {len(steps)} steps")
    return 0


if __name__ == "__main__":
    sys.exit(main())
