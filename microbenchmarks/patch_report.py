#!/usr/bin/env python3
"""
Unified patch vs master performance report across all benchmark suites.

Runs all benchmark modes (or a subset), then produces a single report
showing per-suite details, overall summary, and cross-suite top
improvements/regressions.

Usage:
    ./patch_report.py                          # run all 8 modes, then report
    ./patch_report.py --cached                 # run only 4 cached suites
    ./patch_report.py --uncached               # run only 4 uncached suites
    ./patch_report.py --report-only            # skip benchmarks, report existing results
    ./patch_report.py --reuse-master           # force reuse of old master results
    ./patch_report.py --force-fresh-master     # force re-running master
    ./patch_report.py --no-color               # disable ANSI colors
"""

import argparse
import json
import math
import os
import re
import subprocess
import sys
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BENCHMARK = os.path.join(SCRIPT_DIR, "prefetch_benchmark.py")
RESULTS_DIR = os.path.join(SCRIPT_DIR, "prefetch_results")
MASTER_SOURCE_DIR = "/mnt/nvme/postgresql/master/source"

# All 8 benchmark modes, grouped for display order
ALL_MODES = [
    "benchmark_uncached",
    "benchmark_cached",
    "readstream_uncached",
    "readstream_cached",
    "random_backwards_uncached",
    "random_backwards_cached",
    "munro_uncached",
    "munro_cached",
]

# mode_name -> (extra_flags, default_runs)
VARIANT_CONFIG = {
    "benchmark_uncached":          ([], 3),
    "benchmark_cached":            (["--cached"], 10),
    "readstream_uncached":         (["--readstream-tests"], 3),
    "readstream_cached":           (["--readstream-tests", "--cached"], 10),
    "random_backwards_uncached":   (["--random-backwards-tests"], 3),
    "random_backwards_cached":     (["--random-backwards-tests", "--cached"], 10),
    "munro_uncached":              (["--munro"], 3),
    "munro_cached":                (["--munro", "--cached"], 10),
}

REGRESSION_BOUNDARY = 1.01
ABS_TOLERANCE_MS = 2.0

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


def get_master_hash():
    """Get current master git short hash."""
    try:
        return subprocess.check_output(
            ["git", "-C", MASTER_SOURCE_DIR, "rev-parse", "--short", "HEAD"],
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
                "master_ms": master_ms,
                "patch_ms": patch_ms,
                "ratio": ratio,
            })
    return records


# ── Benchmark execution ─────────────────────────────────────────────────

def should_reuse_master(mode, master_mode):
    """Determine if we should reuse old master results for this mode."""
    if master_mode == "fresh":
        return False
    if master_mode == "old":
        return True
    # smart: reuse if master hash hasn't changed
    current = get_master_hash()
    if not current:
        return False
    last = get_last_master_hash(mode)
    return current == last


def run_benchmarks(modes, master_mode):
    """Run prefetch_benchmark.py for each requested mode."""
    print(f"{BOLD}Running benchmarks for {len(modes)} modes...{RESET}\n")
    for i, mode in enumerate(modes):
        flags, default_runs = VARIANT_CONFIG[mode]
        cmd = [sys.executable, BENCHMARK] + flags
        cmd += ["--runs", str(default_runs), "--prefetch-only", "--terse"]

        if should_reuse_master(mode, master_mode):
            print(f"  [{i+1}/{len(modes)}] {mode} (reusing master)")
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

def print_header(results_by_mode):
    """Print report header with git hashes and timestamp."""
    # Get hashes from first available result
    patch_hash = master_hash = "unknown"
    for data in results_by_mode.values():
        patch_hash = data.get("patch_hash", "unknown")
        master_hash = data.get("master_hash", "unknown")
        break

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"{'═' * 78}")
    print(f" PATCH PERFORMANCE REPORT")
    print(f" patch {patch_hash} vs master {master_hash}  ·  {now}")
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

        ratios = [r["ratio"] for r in mode_records if r["ratio"] is not None]
        gm = compute_geomean(ratios)
        gm_str = f"geomean {gm:.3f}x" if gm else ""

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

            # Color the ratio
            if r["ratio"] is not None and r["ratio"] < 0.99:
                color = GREEN
            elif r["ratio"] is not None and r["ratio"] > REGRESSION_BOUNDARY:
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
        ratios = [r["ratio"] for r in recs if r["ratio"] is not None]
        gm = compute_geomean(ratios)
        n_total = len(ratios)
        n_regressed = sum(1 for r in ratios if r > REGRESSION_BOUNDARY)
        n_improved = sum(1 for r in ratios if r < 0.99)
        n_neutral = n_total - n_regressed - n_improved
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
            ratios = [r["ratio"] for r in mode_recs if r["ratio"] is not None]
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
            ("<0.15", lambda r: r < 0.15),
            ("0.15-0.50", lambda r: 0.15 <= r < 0.50),
            ("0.50-0.99", lambda r: 0.50 <= r < 0.99),
            ("0.99-1.01", lambda r: 0.99 <= r <= 1.01),
            ("1.01-1.05", lambda r: 1.01 < r <= 1.05),
            (">1.05", lambda r: r > 1.05),
        ]
    else:
        # Narrow range (cached-like): use tight buckets
        buckets = [
            ("<0.97", lambda r: r < 0.97),
            ("0.97-0.99", lambda r: 0.97 <= r < 0.99),
            ("0.99-1.01", lambda r: 0.99 <= r <= 1.01),
            ("1.01-1.03", lambda r: 1.01 < r <= 1.03),
            ("1.03-1.05", lambda r: 1.03 < r <= 1.05),
            (">1.05", lambda r: r > 1.05),
        ]

    n = len(ratios)
    print(f"\n {label} ratio distribution ({n} queries):")

    max_bar = 40
    counts = []
    for bucket_label, pred in buckets:
        count = sum(1 for r in ratios if pred(r))
        counts.append((bucket_label, count))

    max_count = max(c for _, c in counts) if counts else 1
    for bucket_label, count in counts:
        if count == 0:
            continue
        bar_len = max(1, int(count / max_count * max_bar))
        pct = count / n * 100
        # Color: green for fast buckets, red for slow buckets
        if "0.99" in bucket_label or bucket_label.startswith("0.99"):
            color = ""
        elif bucket_label.startswith("<") or bucket_label.startswith("0."):
            if "1.0" in bucket_label:
                color = ""
            else:
                color = GREEN
        else:
            color = RED
        reset = RESET if color else ""
        print(f"   {bucket_label:<11} {color}{'█' * bar_len}{reset}"
              f"{'':>{max_bar - bar_len + 2}}{count:>3} ({pct:4.0f}%)")


def print_top_improvements(records, n=15):
    """Print top N improvements across all suites."""
    # Filter to actual improvements (ratio < 0.99, i.e. at least 1% faster)
    improved = [r for r in records
                if r["ratio"] is not None and r["ratio"] < 0.99]
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
        mode_label = "cached" if r["cached"] else "uncached"
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
    # Filter to actual regressions (ratio > 1.01), but skip if both
    # master and patch are tiny (absolute difference below tolerance
    # on sub-millisecond queries is just noise)
    regressed = []
    for r in records:
        if r["ratio"] is None or r["ratio"] <= REGRESSION_BOUNDARY:
            continue
        abs_diff = abs(r["patch_ms"] - r["master_ms"])
        if abs_diff <= ABS_TOLERANCE_MS and r["master_ms"] < 5.0:
            continue
        regressed.append(r)
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
        mode_label = "cached" if r["cached"] else "uncached"
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
        description="Run benchmarks and produce a unified patch vs master report.")
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
    return parser.parse_args()


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

    args = parse_args()

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

    # Run benchmarks unless --report-only
    if not args.report_only:
        run_benchmarks(modes, master_mode)

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
    print_header(results_by_mode)

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
