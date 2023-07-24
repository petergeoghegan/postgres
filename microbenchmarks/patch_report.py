#!/usr/bin/env python3
"""
Unified patch vs master performance report across all benchmark suites.

Runs all benchmark modes (or a subset), then produces a single report
showing per-suite details, overall summary, and cross-suite top
improvements/regressions.

Usage:
    ./patch_report.py                          # run all modes, then report
    ./patch_report.py --cached                 # run only cached suites
    ./patch_report.py --uncached               # run only uncached suites
    ./patch_report.py --report-only            # skip benchmarks, report existing results
    ./patch_report.py --reuse-master           # force reuse of old master results
    ./patch_report.py --force-fresh-master     # force re-running master
    ./patch_report.py --no-color               # disable ANSI colors
    ./patch_report.py --patch-direct           # unrecognized args are forwarded
    ./patch_report.py --delay-pgdata --io_method worker   # ... to prefetch_benchmark.py
"""

import argparse
import json
import math
import os
import re
import subprocess
import sys
from datetime import datetime

from prefetch_benchmark import classify_ratio
from benchmark_suites import BENCHMARK_SUITES

# (suite, qid) pairs flagged index_only in their suite definition.  A pure
# index-only scan never touches the heap, so run_query() skips heap eviction for
# it and it behaves IDENTICALLY cached vs uncached -- the two per-mode results are
# the same query measured twice.  We use this to collapse those duplicates in the
# cross-suite rankings (see _dedup_index_only).
INDEX_ONLY_KEYS = {
    (s["mode_prefix"], qid)
    for s in BENCHMARK_SUITES
    for qid, e in s["queries"].items()
    if e.get("index_only")
}

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BENCHMARK = os.path.join(SCRIPT_DIR, "prefetch_benchmark.py")
RESULTS_DIR = os.path.join(SCRIPT_DIR, "prefetch_results")
MASTER_SOURCE_DIR = "/mnt/nvme/postgresql/master/source"

BASELINE_SOURCE_DIRS = {
    "master": "/mnt/nvme/postgresql/master/source",
    "rel18":  "/mnt/nvme/postgresql/REL_18_STABLE/source",
}

# Forwarded prefetch_benchmark.py flags that do NOT change how the baseline
# server is run or measured.  Smart (hash-based) master reuse stays valid with
# these; any other forwarded flag (--delay-pgdata, --direct-io, --io_method,
# --instrument, ...) could make old baseline results incomparable, so smart
# mode re-runs the baseline instead of reusing.  --patch-direct qualifies:
# the baseline keeps buffered I/O, only the testbranch goes direct.
REUSE_SAFE_PASSTHROUGH = {
    "--patch-direct",
    "--terse",
    "--no-confirm", "--no-reconfirm",
    "--skip-load",
    "--no-kill-interferers",
    "--median",
}


def _discover_suites():
    """Query prefetch_benchmark.py --list-modes to build ALL_MODES and VARIANT_CONFIG.

    This makes prefetch_benchmark.py the single source of truth for the set of
    benchmark suites.  Adding a new suite there automatically exposes it here.
    """
    raw = subprocess.check_output(
        [sys.executable, BENCHMARK, "--list-modes"], text=True)
    suites = json.loads(raw)

    all_modes = []
    variant_config = {}
    for s in suites:
        prefix = s["mode_prefix"]
        cli_flag = s["cli_flag"]
        flags = [cli_flag] if cli_flag else []

        uncached = f"{prefix}_uncached"
        cached = f"{prefix}_cached"
        all_modes += [uncached, cached]
        variant_config[uncached] = (list(flags), s["uncached_runs"])
        variant_config[cached]   = (flags + ["--cached"], s["cached_runs"])

    return all_modes, variant_config


ALL_MODES, VARIANT_CONFIG = _discover_suites()

# Per-query improvement/regression/neutral classification (incl. noise-level
# absolute-difference suppression) lives in prefetch_benchmark.classify_ratio, so
# this report matches direct prefetch_benchmark.py output.  REGRESSION_BOUNDARY
# is kept only for coloring an aggregate geomean value red.
REGRESSION_BOUNDARY = 1.01

# ANSI escape codes (overridden to empty strings with --no-color)
BOLD = "\033[1m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
DIM = "\033[2m"
RESET = "\033[0m"


def disable_colors():
    global BOLD, RED, GREEN, YELLOW, DIM, RESET
    BOLD = RED = GREEN = YELLOW = DIM = RESET = ""


# ── Helpers ──────────────────────────────────────────────────────────────

def find_result_files(results_dir, mode):
    """Find all JSON result files for a given mode, sorted newest first."""
    if not os.path.exists(results_dir):
        return []
    prefix = f"{mode}_"
    files = [os.path.join(results_dir, f)
             for f in os.listdir(results_dir)
             if f.startswith(prefix) and f.endswith(".json")]
    files.sort(key=os.path.getmtime, reverse=True)
    return files


def load_result(path):
    with open(path) as f:
        return json.load(f)


def compute_geomean(ratios):
    if not ratios:
        return None
    log_sum = sum(math.log(r) for r in ratios)
    return math.exp(log_sum / len(ratios))


def get_master_hash(baseline="master"):
    """Get current baseline git short hash."""
    source_dir = BASELINE_SOURCE_DIRS.get(baseline, MASTER_SOURCE_DIR)
    try:
        return subprocess.check_output(
            ["git", "-C", source_dir, "rev-parse", "--short", "HEAD"],
            text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return ""


def get_last_master_hash(mode):
    """Get master_hash from the most recent result file for a mode."""
    files = find_result_files(RESULTS_DIR, mode)
    if files:
        try:
            data = load_result(files[0])
            return data.get("master_hash", "")
        except Exception:
            pass
    return ""


def suite_name(mode):
    """Extract suite name from mode: 'benchmark_cached' -> 'benchmark'."""
    return re.sub(r'_(cached|uncached)$', '', mode)


def is_cached(mode):
    return mode.endswith("_cached")


def format_ms_short(ms):
    """Format ms value compactly for tables."""
    if ms is None:
        return "N/A"
    if ms >= 1000:
        return f"{ms:.1f} ms"
    elif ms >= 10:
        return f"{ms:.1f} ms"
    elif ms >= 1:
        return f"{ms:.1f} ms"
    else:
        return f"{ms:.3f} ms"


def format_ms_aligned(ms, width=10):
    """Format ms right-aligned to a fixed width."""
    s = format_ms_short(ms)
    return f"{s:>{width}}"


def ratio_bar(ratio, max_deviation=0.08):
    """Create a small Unicode bar showing deviation from 1.0.

    Green █ for improvement (ratio < 1.0), red ░ for regression (ratio > 1.0).
    """
    if ratio is None:
        return ""
    deviation = ratio - 1.0
    # Scale: max_deviation maps to ~10 characters
    bar_len = min(int(abs(deviation) / max_deviation * 10), 15)
    if bar_len == 0:
        return ""
    if deviation < 0:
        return f"  {GREEN}{'█' * bar_len}{RESET}"
    else:
        return f"  {RED}{'░' * bar_len}{RESET}"


# ── Data extraction ──────────────────────────────────────────────────────

def extract_query_data(results_by_mode):
    """Extract flat list of query records from all loaded results.

    Returns list of dicts with: qid, name, mode, suite, cached,
    master_ms, patch_ms, ratio
    """
    records = []
    for mode, data in results_by_mode.items():
        queries = data.get("queries", {})
        for qid, qdata in queries.items():
            master_times = qdata.get("master", {}).get("times", [])
            patch_on_times = qdata.get("patch_on", {}).get("times", [])
            if not master_times or not patch_on_times:
                continue
            master_ms = min(master_times)
            patch_ms = min(patch_on_times)
            ratio = patch_ms / master_ms if master_ms > 0 else None
            records.append({
                "qid": qid,
                "name": qdata.get("name", ""),
                "mode": mode,
                "suite": suite_name(mode),
                "cached": is_cached(mode),
                "index_only": (suite_name(mode), qid) in INDEX_ONLY_KEYS,
                "master_ms": master_ms,
                "patch_ms": patch_ms,
                "ratio": ratio,
                "category": classify_ratio(master_ms, patch_ms),
            })
    return records


def _dedup_index_only(records):
    """Collapse each index-only query's cached+uncached pair into one record.

    Index-only scans never touch the heap, so cached and uncached are the same
    query run twice (only setup differs); listing both in cross-suite rankings
    double-counts them.  Keep one canonical record per (suite, qid) -- preferring
    the uncached measurement -- and label its mode "idx-only" since the
    distinction is meaningless.  Non-index-only records pass through unchanged.
    """
    passthrough = []
    chosen = {}  # (suite, qid) -> record
    for r in records:
        if not r.get("index_only"):
            passthrough.append(r)
            continue
        key = (r["suite"], r["qid"])
        cur = chosen.get(key)
        if cur is None or (not r["cached"] and cur["cached"]):
            chosen[key] = r
    collapsed = [{**r, "mode_label": "idx-only"} for r in chosen.values()]
    return passthrough + collapsed


# ── Benchmark execution ─────────────────────────────────────────────────

def should_reuse_master(mode, master_mode, baseline="master"):
    """Determine if we should reuse old baseline results for this mode."""
    if master_mode == "fresh":
        return False
    if master_mode == "old":
        return True
    # smart: reuse if baseline hash hasn't changed
    current = get_master_hash(baseline)
    if not current:
        return False
    last = get_last_master_hash(mode)
    return current == last


def run_benchmarks(modes, master_mode, baseline="master", testbranch="patch",
                   passthrough=None):
    """Run prefetch_benchmark.py for each requested mode."""
    print(f"{BOLD}Running benchmarks for {len(modes)} modes...{RESET}\n")
    for i, mode in enumerate(modes):
        flags, _runs = VARIANT_CONFIG[mode]
        cmd = [sys.executable, BENCHMARK] + flags
        # Do NOT pass --runs: forcing an exact count both pinned every query at N
        # (here, the old static default) and disabled the auto-confirmation pass
        # (which only runs when args.runs is None).  Omitting it lets
        # prefetch_benchmark use its dynamic run-count + confirmation, so this
        # wrapper produces the same per-query results as a manual invocation.
        cmd += ["--prefetch-only", "--terse"]
        cmd += ["--baseline", baseline, "--testbranch", testbranch]
        if passthrough:
            cmd += passthrough

        if should_reuse_master(mode, master_mode, baseline):
            print(f"  [{i+1}/{len(modes)}] {mode} (reusing {baseline})")
            cmd.append("--old-master-results")
        else:
            print(f"  [{i+1}/{len(modes)}] {mode}")

        try:
            subprocess.run(cmd, check=True)
        except subprocess.CalledProcessError:
            print(f"\n{RED}FAILED: {mode}{RESET}")
            sys.exit(1)
        except KeyboardInterrupt:
            print(f"\n{YELLOW}Interrupted during {mode}{RESET}")
            sys.exit(130)

    print()


# ── Report sections ─────────────────────────────────────────────────────

def print_header(results_by_mode, baseline="master"):
    """Print report header with git hashes, IO settings, and timestamp."""
    # Get hashes and IO settings from first available result
    patch_hash = master_hash = "unknown"
    io_settings = None
    for data in results_by_mode.values():
        patch_hash = data.get("patch_hash", "unknown")
        master_hash = data.get("master_hash", "unknown")
        if io_settings is None:
            io_settings = data.get("io_settings")
        break

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"{'═' * 78}")
    print(f" PATCH PERFORMANCE REPORT")
    print(f" patch {patch_hash} vs {baseline} {master_hash}  ·  {now}")
    if io_settings:
        print()
        print(f" PostgreSQL I/O settings:")
        print(f"   effective_io_concurrency = {io_settings['effective_io_concurrency']}")
        print(f"   io_combine_limit = {io_settings['io_combine_limit']} (8kB units)")
        print(f"   io_max_combine_limit = {io_settings['io_max_combine_limit']} (8kB units)")
        print(f"   io_max_concurrency = {io_settings['io_max_concurrency']}")
        if 'io_max_workers' in io_settings:
            print(f"   io_max_workers = {io_settings['io_max_workers']}")
            print(f"   io_min_workers = {io_settings['io_min_workers']}")
        elif 'io_workers' in io_settings:
            print(f"   io_workers = {io_settings['io_workers']}")
        print(f"   io_method = {io_settings['io_method']}")
        if io_settings.get("direct_io"):
            print(f"   debug_io_direct = data")
        elif io_settings.get("patch_direct"):
            print(f"   debug_io_direct = data (testbranch only; baseline buffered)")
    print(f"{'═' * 78}")


def print_detail_section(records, cached_flag, results_by_mode):
    """Print the per-suite detail tables for cached or uncached modes."""
    label = "CACHED" if cached_flag else "UNCACHED"
    filtered_modes = [m for m in ALL_MODES if is_cached(m) == cached_flag
                      and m in results_by_mode]

    if not filtered_modes:
        return

    print(f"\n{'─' * 2} {label}: ALL QUERIES {'─' * (78 - 18 - len(label))}")

    for mode in filtered_modes:
        mode_records = [r for r in records if r["mode"] == mode]
        if not mode_records:
            continue

        # Geomean excludes neutral (noise-level) results -- their extreme ratios
        # would distort it (matches prefetch_benchmark.py).
        ratios = [r["ratio"] for r in mode_records
                  if r["ratio"] is not None and r["category"] != "neutral"]
        gm = compute_geomean(ratios)
        gm_str = f"geomean {gm:.3f}x (neutral excl.)" if gm else ""

        print(f"\n {BOLD}{mode}{RESET} · {len(mode_records)} queries · {gm_str}")
        print()

        # Column header
        print(f" {'Query':<6} {'Name':<40} {'Master':>10} {'Patch':>10} {'Ratio':>7}")
        print(f" {'─' * 6} {'─' * 40} {'─' * 10} {'─' * 10} {'─' * 7}")

        # Sort by ratio (best first)
        sorted_records = sorted(mode_records, key=lambda r: r["ratio"] or 999)

        for r in sorted_records:
            name = r["name"]
            if len(name) > 40:
                name = name[:37] + "..."

            ratio_str = f"{r['ratio']:.3f}x" if r["ratio"] else "N/A"
            bar = ratio_bar(r["ratio"])

            # Color the ratio by its shared classification (neutral incl. noise)
            if r["category"] == "improvement":
                color = GREEN
            elif r["category"] == "regression":
                color = RED
            else:
                color = ""

            reset = RESET if color else ""
            print(f" {r['qid']:<6} {name:<40} "
                  f"{format_ms_aligned(r['master_ms'])} "
                  f"{format_ms_aligned(r['patch_ms'])} "
                  f"{color}{ratio_str:>7}{reset}{bar}")


def print_summary(records):
    """Print summary section with geomeans and distribution."""
    cached = [r for r in records if r["cached"]]
    uncached = [r for r in records if not r["cached"]]

    print(f"\n{'─' * 2} SUMMARY {'─' * (78 - 11)}")

    # Overall geomean table
    print(f"\n {'':19} {'Geomean':>8}   {'Queries':>7}   {'Regressed':>9}   {'Improved':>8}   {'Neutral':>7}")

    for label, recs in [("Cached:", cached), ("Uncached:", uncached)]:
        if not recs:
            continue
        valid = [r for r in recs if r["ratio"] is not None]
        n_total = len(valid)
        n_regressed = sum(1 for r in valid if r["category"] == "regression")
        n_improved = sum(1 for r in valid if r["category"] == "improvement")
        n_neutral = sum(1 for r in valid if r["category"] == "neutral")
        # Geomean over non-neutral only (neutral noise would distort it).
        gm = compute_geomean([r["ratio"] for r in valid if r["category"] != "neutral"])
        gm_str = f"{gm:.3f}x" if gm else "N/A"

        # Color the geomean
        if gm and gm < 0.99:
            color = GREEN
        elif gm and gm > REGRESSION_BOUNDARY:
            color = RED
        else:
            color = ""
        reset = RESET if color else ""

        print(f"   {label:<17} {color}{gm_str:>8}{reset}   {n_total:>7}   "
              f"{n_regressed:>9}   {n_improved:>8}   {n_neutral:>7}")

    # Per-suite geomeans
    for label, recs in [("CACHED", cached), ("UNCACHED", uncached)]:
        if not recs:
            continue
        print(f"\n {label}: per-suite geomeans")
        suites_seen = []
        for mode in ALL_MODES:
            mode_recs = [r for r in recs if r["mode"] == mode]
            if not mode_recs:
                continue
            ratios = [r["ratio"] for r in mode_recs
                      if r["ratio"] is not None and r["category"] != "neutral"]
            gm = compute_geomean(ratios)
            if gm is None:
                continue
            sname = suite_name(mode)
            if sname in suites_seen:
                continue
            suites_seen.append(sname)
            gm_str = f"{gm:.3f}x"
            n = len(ratios)
            if gm < 0.99:
                color = GREEN
            elif gm > REGRESSION_BOUNDARY:
                color = RED
            else:
                color = ""
            reset = RESET if color else ""
            print(f"   {sname:<22} {color}{gm_str:>8}{reset}  ··· {n} queries")

    # Distribution histograms
    for label, recs in [("Cached", cached), ("Uncached", uncached)]:
        if not recs:
            continue
        ratios = [r["ratio"] for r in recs if r["ratio"] is not None]
        print_distribution(ratios, label)


def print_distribution(ratios, label):
    """Print a ratio distribution histogram."""
    if not ratios:
        return

    # Define buckets based on the data range
    min_r = min(ratios)
    max_r = max(ratios)

    if min_r < 0.5:
        # Wide range (uncached-like): use wider buckets
        buckets = [
            ("<0.05",     lambda r: r < 0.05,              GREEN),
            ("0.05-0.10", lambda r: 0.05 <= r < 0.10,      GREEN),
            ("0.10-0.20", lambda r: 0.10 <= r < 0.20,      GREEN),
            ("0.20-0.35", lambda r: 0.20 <= r < 0.35,      GREEN),
            ("0.35-0.50", lambda r: 0.35 <= r < 0.50,      GREEN),
            ("0.50-0.65", lambda r: 0.50 <= r < 0.65,      GREEN),
            ("0.65-0.80", lambda r: 0.65 <= r < 0.80,      GREEN),
            ("0.80-0.90", lambda r: 0.80 <= r < 0.90,      GREEN),
            ("0.90-0.99", lambda r: 0.90 <= r < 0.99,      GREEN),
            ("0.99-1.01", lambda r: 0.99 <= r <= 1.01,     ""),
            ("1.01-1.05", lambda r: 1.01 < r <= 1.05,      RED),
            ("1.05-1.10", lambda r: 1.05 < r <= 1.10,      RED),
        ]
        # Fine-grained 0.05-step buckets from 1.10 upward
        lo = 1.10
        while lo < max_r:
            hi = round(lo + 0.05, 2)
            buckets.append((f"{lo:.2f}-{hi:.2f}",
                            lambda r, lo=lo, hi=hi: lo < r <= hi, RED))
            lo = hi
        buckets.append((f">{lo:.2f}",
                        lambda r, lo=lo: r > lo, RED))
    else:
        # Narrow range (cached-like): use tight buckets
        buckets = [
            ("<0.90",     lambda r: r < 0.90,              GREEN),
            ("0.90-0.93", lambda r: 0.90 <= r < 0.93,      GREEN),
            ("0.93-0.95", lambda r: 0.93 <= r < 0.95,      GREEN),
            ("0.95-0.97", lambda r: 0.95 <= r < 0.97,      GREEN),
            ("0.97-0.98", lambda r: 0.97 <= r < 0.98,      GREEN),
            ("0.98-0.99", lambda r: 0.98 <= r < 0.99,      GREEN),
            ("0.99-1.01", lambda r: 0.99 <= r <= 1.01,     ""),
            ("1.01-1.02", lambda r: 1.01 < r <= 1.02,      RED),
            ("1.02-1.03", lambda r: 1.02 < r <= 1.03,      RED),
            ("1.03-1.05", lambda r: 1.03 < r <= 1.05,      RED),
            ("1.05-1.10", lambda r: 1.05 < r <= 1.10,      RED),
            (">1.10",     lambda r: r > 1.10,              RED),
        ]

    n = len(ratios)
    print(f"\n {label} ratio distribution ({n} queries):")

    max_bar = 40
    counts = []
    for bucket_label, pred, bucket_color in buckets:
        count = sum(1 for r in ratios if pred(r))
        counts.append((bucket_label, count, bucket_color))

    max_count = max(c for _, c, _ in counts) if counts else 1
    for bucket_label, count, color in counts:
        if count == 0:
            continue
        bar_len = max(1, int(count / max_count * max_bar))
        pct = count / n * 100
        reset = RESET if color else ""
        print(f"   {bucket_label:<12}{color}{'█' * bar_len}{reset}"
              f"{'':>{max_bar - bar_len + 2}}{count:>3} ({pct:4.0f}%)")


def print_top_improvements(records, n=15):
    """Print top N improvements across all suites."""
    # Improvements per the shared classification (noise-level results are neutral)
    records = _dedup_index_only(records)
    improved = [r for r in records if r["category"] == "improvement"]
    improved.sort(key=lambda r: r["ratio"])
    improved = improved[:n]

    if not improved:
        return

    print(f"\n{'─' * 2} TOP {n} IMPROVEMENTS (across all suites) "
          f"{'─' * (78 - 39 - len(str(n)))}")
    print()
    print(f" {'Query':<5} {'Suite':<17} {'Mode':<9} {'Master':>10} "
          f"{'Patch':>10} {'Ratio':>7}  {'Speedup':>14}")
    print(f" {'─' * 5} {'─' * 17} {'─' * 9} {'─' * 10} "
          f"{'─' * 10} {'─' * 7}  {'─' * 14}")

    for r in improved:
        speedup = 1.0 / r["ratio"] if r["ratio"] > 0 else 0
        mode_label = r.get("mode_label") or ("cached" if r["cached"] else "uncached")
        # Show "Nx faster" for big wins, percentage for small ones
        if speedup >= 1.5:
            speedup_str = f"{speedup:>5.1f}x faster"
        else:
            pct = (1.0 - r["ratio"]) * 100
            speedup_str = f"   -{pct:.1f}%      "
        print(f" {GREEN}{r['qid']:<5}{RESET} {r['suite']:<17} {mode_label:<9} "
              f"{format_ms_aligned(r['master_ms'])} "
              f"{format_ms_aligned(r['patch_ms'])} "
              f"{GREEN}{r['ratio']:.3f}x{RESET}  "
              f"{GREEN}{speedup_str}{RESET}")


def print_top_regressions(records, n=15):
    """Print top N regressions across all suites."""
    # Regressions per the shared classification, which already suppresses
    # noise-level absolute differences on tiny queries (see classify_ratio).
    records = _dedup_index_only(records)
    regressed = [r for r in records if r["category"] == "regression"]
    regressed.sort(key=lambda r: r["ratio"], reverse=True)
    regressed = regressed[:n]

    if not regressed:
        print(f"\n{'─' * 2} TOP REGRESSIONS {'─' * (78 - 19)}")
        print(f"\n {GREEN}No regressions above threshold.{RESET}")
        return

    print(f"\n{'─' * 2} TOP {n} REGRESSIONS (across all suites) "
          f"{'─' * (78 - 38 - len(str(n)))}")
    print()
    print(f" {'Query':<5} {'Suite':<17} {'Mode':<9} {'Master':>10} "
          f"{'Patch':>10} {'Ratio':>7}  {'Slowdown':>10}")
    print(f" {'─' * 5} {'─' * 17} {'─' * 9} {'─' * 10} "
          f"{'─' * 10} {'─' * 7}  {'─' * 10}")

    for r in regressed:
        slowdown_pct = (r["ratio"] - 1.0) * 100
        mode_label = r.get("mode_label") or ("cached" if r["cached"] else "uncached")
        print(f" {RED}{r['qid']:<5}{RESET} {r['suite']:<17} {mode_label:<9} "
              f"{format_ms_aligned(r['master_ms'])} "
              f"{format_ms_aligned(r['patch_ms'])} "
              f"{RED}{r['ratio']:.3f}x{RESET}  "
              f"{RED}{'+' if slowdown_pct > 0 else ''}{slowdown_pct:.1f}%{RESET}")


def print_footer(records, results_by_mode, elapsed_seconds=None):
    """Print report footer."""
    cached_count = len([r for r in records if r["cached"]])
    uncached_count = len([r for r in records if not r["cached"]])
    parts = []
    if cached_count:
        parts.append(f"{cached_count} cached queries")
    if uncached_count:
        parts.append(f"{uncached_count} uncached queries")
    if elapsed_seconds is not None:
        mins = int(elapsed_seconds) // 60
        secs = int(elapsed_seconds) % 60
        if mins > 0:
            parts.append(f"wall time {mins}m {secs}s")
        else:
            parts.append(f"wall time {secs}s")
    summary = " · ".join(parts)

    print(f"\n{'═' * 78}")
    print(f" {summary}")
    print(f"{'═' * 78}")


# ── Main ─────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(
        description="Run benchmarks and produce a unified patch vs master report.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Any argument not listed above is forwarded verbatim to every
prefetch_benchmark.py invocation, e.g.:
    %(prog)s --patch-direct
    %(prog)s --delay-pgdata --io_method worker
(see prefetch_benchmark.py --help for the full set).  Forwarded flags that
change how the baseline is run or measured (--delay-pgdata, --direct-io,
--io_method, --instrument, ...) disable smart hash-based baseline reuse for
this run; pass --reuse-master to override.""")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--cached", action="store_true",
                            help="Run/report only cached benchmark suites")
    mode_group.add_argument("--uncached", action="store_true",
                            help="Run/report only uncached benchmark suites")
    parser.add_argument("--report-only", action="store_true",
                        help="Skip running benchmarks, just report on existing results")

    master_group = parser.add_mutually_exclusive_group()
    master_group.add_argument("--reuse-master", action="store_true",
                              help="Always reuse old master results")
    master_group.add_argument("--force-fresh-master", action="store_true",
                              help="Always re-run master benchmarks")

    parser.add_argument("--no-color", action="store_true",
                        help="Disable ANSI color output")
    parser.add_argument("--top", type=int, default=15,
                        help="Number of top improvements/regressions to show (default: 15)")
    parser.add_argument("--baseline", type=str, default="master",
                        choices=["master", "rel18"],
                        help="Baseline PostgreSQL build to compare against (default: master)")
    parser.add_argument("--testbranch", type=str, default="patch",
                        choices=["patch", "master"],
                        help="PostgreSQL build to test (default: patch). Use 'master' to compare master vs rel18.")

    args, passthrough = parser.parse_known_args()

    # Everything unrecognized is forwarded to prefetch_benchmark.py.  Reject
    # stray positionals early (likely typos) -- a non-flag token is fine only
    # as the value of the forwarded flag right before it ("--io_method worker").
    # A bad --flag itself still fails fast with a clear argparse error on the
    # first benchmark invocation.
    stray = [a for i, a in enumerate(passthrough)
             if not a.startswith("-")
             and (i == 0 or "=" in passthrough[i - 1]
                  or not passthrough[i - 1].startswith("-"))]
    if stray:
        parser.error(f"unrecognized arguments: {' '.join(stray)}")

    if passthrough and not args.report_only:
        print(f"Forwarding to prefetch_benchmark.py: {' '.join(passthrough)}")

    return args, passthrough


def select_modes(args):
    """Select which modes to run/report based on --cached/--uncached."""
    if args.cached:
        return [m for m in ALL_MODES if is_cached(m)]
    elif args.uncached:
        return [m for m in ALL_MODES if not is_cached(m)]
    else:
        return list(ALL_MODES)


def main():
    import time
    wall_start = time.monotonic()

    args, passthrough = parse_args()

    if args.no_color:
        disable_colors()

    modes = select_modes(args)

    # Determine master reuse mode
    if args.reuse_master:
        master_mode = "old"
    elif args.force_fresh_master:
        master_mode = "fresh"
    else:
        master_mode = "smart"

    # Smart reuse compares only baseline git hashes, so a forwarded flag that
    # changes how the baseline is run or measured (--delay-pgdata, --direct-io,
    # --io_method, --instrument, ...) would silently reuse incomparable results.
    # Force a fresh baseline for those; an explicit --reuse-master still wins.
    if master_mode == "smart" and not args.report_only:
        unsafe = sorted({a.split("=")[0] for a in passthrough
                         if a.startswith("-")
                         and a.split("=")[0] not in REUSE_SAFE_PASSTHROUGH})
        if unsafe:
            print(f"{YELLOW}Forwarded flags may change baseline measurement "
                  f"({', '.join(unsafe)}): re-running baseline "
                  f"(use --reuse-master to override).{RESET}")
            master_mode = "fresh"

    # Run benchmarks unless --report-only
    if not args.report_only:
        run_benchmarks(modes, master_mode, baseline=args.baseline,
                       testbranch=args.testbranch, passthrough=passthrough)

    # Load results
    results_by_mode = {}
    missing_modes = []
    for mode in modes:
        files = find_result_files(RESULTS_DIR, mode)
        if files:
            results_by_mode[mode] = load_result(files[0])
        else:
            missing_modes.append(mode)

    if missing_modes:
        print(f"{YELLOW}No results found for: {', '.join(missing_modes)}{RESET}")

    if not results_by_mode:
        print(f"{RED}No benchmark results found in {RESULTS_DIR}/{RESET}")
        sys.exit(1)

    # Extract all query data
    records = extract_query_data(results_by_mode)

    # Generate report
    print_header(results_by_mode, baseline=args.baseline)

    # Detail tables: cached first, then uncached
    has_cached = any(r["cached"] for r in records)
    has_uncached = any(not r["cached"] for r in records)

    if has_cached:
        print_detail_section(records, True, results_by_mode)
    if has_uncached:
        print_detail_section(records, False, results_by_mode)

    # Summary
    print_summary(records)

    # Top improvements and regressions across all suites
    print_top_improvements(records, n=args.top)
    print_top_regressions(records, n=args.top)

    # Footer
    elapsed = time.monotonic() - wall_start
    print_footer(records, results_by_mode, elapsed)


if __name__ == "__main__":
    main()
