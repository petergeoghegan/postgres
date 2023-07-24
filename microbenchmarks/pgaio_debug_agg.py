#!/usr/bin/env python3
"""
pgaio_debug_agg.py -- aggregate PostgreSQL AIO debug ("pgaio_debug") server-log
lines into per-backend summary statistics.

PURPOSE
-------
The AIO subsystem emits very verbose per-IO debug logging (macros pgaio_debug /
pgaio_debug_io in src/include/storage/aio_internal.h).  When capturing a single
query with log_min_messages >= DEBUG3 the firehose is thousands of lines per
statement.  This tool turns that firehose into useful aggregates:

  * IO lifecycles counted (one completed IO == one "reclaiming:" line)
  * synchronous vs asynchronous split (from the "staged (synchronous: N ...)" line)
  * per-IO size / io_combine histogram (blocks per IO, from the md_readv
    completion "result <bytes>" -> bytes/8192)
  * distinct-blocks vs total-IO "count >> read" churn ratio (best-effort;
    see LIMITATIONS -- block numbers are NOT in the default log format)
  * foreign-attach ("already in progress") vs real-pread completions
    (from read_stream/bufmgr "foreign" markers when present)
  * per-IO-slot reuse and callback-stage counts

SCOPE / BRANCH
--------------
Written for the index-prefetch branch (index-prefetch-v0.94, PG target 20) and
io_method=io_uring.  The core "io <id>|op ...|target ...|state ...: <msg>" prefix
and the state-transition / staged / reclaiming / callback lines are emitted from
the common aio.c / aio_callback.c layer and are therefore io-method-independent.
The io_uring-specific lines this tool also recognizes are:
    "aio method uring: submitted N IOs", "wait_one ...", "check_one ...",
    "drained A/B, now expecting C".
The io_method=worker and io_method=sync line variants (e.g. the per-worker
"worker N processing IO" line) are NOT specially handled yet -- they will simply
fall through to the generic prefix parser and be counted, but worker-specific
aggregates are not produced.

EXPECTED LOG-LINE FORMAT
------------------------
With log_line_prefix = '%m [%p] ' (or any prefix ending in "[<pid>] "), lines look
like:

  2026-07-01 16:49:25.668 EDT [1613360] DEBUG:  io 80        |op readv|target smgr|state SUBMITTED       : updating state to COMPLETED_IO
  2026-07-01 16:49:25.668 EDT [1613360] DEBUG:  io 80        |op readv|target smgr|state COMPLETED_IO    : calling cb #2, id 1/aio_md_readv_cb->complete_shared(0) with distilled result: (status OK, id 0, error_data 0, result 24576)
  2026-07-01 16:49:25.668 EDT [1613360] DEBUG:  io 80        |op readv|target smgr|state STAGED          : staged (synchronous: 0, in_batch: 0)
  2026-07-01 16:49:25.668 EDT [1613360] DEBUG:  io 80        |op readv|target smgr|state COMPLETED_LOCAL : reclaiming: distilled_result: (status OK, id 0, error_data 0), raw_result: 24576
  2026-07-01 16:49:25.668 EDT [1613360] DEBUG:  aio method uring: submitted 1 IOs

The pid is taken from the "[<pid>]" field in the prefix; if your prefix differs,
pass --pid-regex.  The AIO fields are parsed from the stable
"io <id>|op <op>|target <tgt>|state <state>: <msg>" body, which does not depend on
log_line_prefix at all.

IMPORTANT: a scan may emit a trailing " at character N" (statement-position error
context).  The parser tolerates and strips it.

LIMITATIONS
-----------
* Block numbers are NOT in the default pgaio_debug format (the prefix carries only
  target *name* "smgr", not the block).  So true distinct-block counting and the
  per-block re-request histogram are only available if the log also contains the
  target *description* (e.g. "blocks 100..102 in file ...") -- which happens for
  error/retry lines and, in worker mode, ps titles, not the normal hot path.
  This tool therefore reports the block-churn metrics as "n/a (no block info in
  log)" unless such lines are present, and instead reports the IO-size histogram
  which is the actionable proxy for io_combine behavior.
* The IO handle id ("io 80") is a reusable slot, NOT a unique per-IO key; do not
  count distinct ids to count IOs.  Count "reclaiming:" lines instead.

USAGE
-----
  python3 pgaio_debug_agg.py [LOGFILE ...]        # or read stdin
  python3 pgaio_debug_agg.py server.log --pid 1613360
  python3 pgaio_debug_agg.py server.log --since '2026-07-01 16:49' --until '2026-07-01 16:50'
  python3 pgaio_debug_agg.py server.log --only sizes,sync,foreign
  cat server.log | python3 pgaio_debug_agg.py --pid 1613360

Dependency-light: Python 3 standard library only.
"""

import argparse
import re
import sys
from collections import Counter, defaultdict

# ---- Regexes tied to the real format strings (file:line references) ----------

# prefix pid, default log_line_prefix '%m [%p] ' -> "... [1613360] DEBUG:"
DEFAULT_PID_RE = re.compile(r"\[(\d+)\]")

# leading timestamp (for --since/--until); matches '%m' = 'YYYY-MM-DD HH:MM:SS.mmm TZ'
TS_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(?:\.\d+)?)")

# The stable AIO body prefix from pgaio_debug_io()
#   aio_internal.h:408  "io %-10d|op %-5s|target %-4s|state %-16s: "
BODY_RE = re.compile(
    r"io\s+(?P<id>\d+)\s*\|op\s+(?P<op>\S+)\s*\|target\s+(?P<target>\S+)\s*\|"
    r"state\s+(?P<state>\S+)\s*:\s*(?P<msg>.*?)(?:\s+at character \d+)?\s*$"
)

# "reclaiming: distilled_result: ..."  aio.c:750  -> one per completed IO lifecycle
RECLAIM_RE = re.compile(r"^reclaiming:")

# "updating state to <STATE>"          aio.c:417
UPDATE_STATE_RE = re.compile(r"^updating state to (?P<to>\S+)")

# "staged (synchronous: N, in_batch: N)"   aio.c:479
STAGED_RE = re.compile(r"^staged \(synchronous: (?P<sync>\d+), in_batch: (?P<batch>\d+)\)")

# md readv completion carries the real byte count -> nblocks
#   aio_callback.c:252 "...aio_md_readv_cb->complete_shared(N) with distilled
#   result: (status %s, id %u, error_data %d, result %d)"
MD_COMPLETE_RE = re.compile(
    r"aio_md_readv_cb->complete_shared\(\d+\) with distilled result: "
    r"\(status (?P<status>\w+), id \d+, error_data (?P<edata>-?\d+), result (?P<bytes>-?\d+)\)"
)

# io_uring submit line (no io-prefix): method_io_uring.c:481 "aio method uring: submitted %d IOs"
URING_SUBMIT_RE = re.compile(r"aio method uring: submitted (?P<n>\d+) IOs")
# generic submit: aio.c:1263 "aio: submitted %d IOs"
GEN_SUBMIT_RE = re.compile(r"^\s*aio: submitted (?P<n>\d+) IOs")

# foreign / already-in-progress markers.  On this branch bufmgr/read_stream use
# the word "foreign" for an already-in-progress attach; also match the generic
# "waiting for IO"/"in flight" waits.  These are best-effort textual matches.
FOREIGN_RE = re.compile(r"foreign", re.IGNORECASE)
WAIT_INFLIGHT_RE = re.compile(r"waiting for (free )?IO", re.IGNORECASE)

BLCKSZ = 8192


class BackendStats:
    def __init__(self, pid):
        self.pid = pid
        self.lines = 0
        self.reclaims = 0                 # completed IO lifecycles
        self.state_to = Counter()         # transitions "updating state to X"
        self.sync = 0
        self.async_ = 0
        self.nblocks_hist = Counter()     # blocks-per-IO -> count
        self.md_completions = 0
        self.md_errors = 0
        self.submit_lines = 0
        self.submit_ios = 0               # sum of "submitted N IOs"
        self.foreign_hits = 0
        self.wait_inflight = 0
        self.slot_ids = Counter()         # io-handle-slot reuse
        self.ops = Counter()
        # best-effort block accounting (only if block info ever appears)
        self.block_reqs = Counter()       # blocknum -> times requested
        self.have_block_info = False

    def total_blocks_read(self):
        return sum(nb * cnt for nb, cnt in self.nblocks_hist.items())


def parse_stream(lines, pid_re, want_pid, since, until):
    backends = {}
    for line in lines:
        # time-window filter (string compare works for ISO-ish timestamps)
        if since or until:
            m = TS_RE.match(line)
            if m:
                ts = m.group(1)
                if since and ts < since:
                    continue
                if until and ts > until:
                    continue

        # pid filter
        pid = None
        pm = pid_re.search(line)
        if pm:
            pid = pm.group(1)
        if want_pid and pid != want_pid:
            # allow AIO body lines even if pid unknown only when no filter set
            continue

        bm = BODY_RE.search(line)
        if not bm:
            # non-AIO-body lines: still capture submit lines (they carry no io-prefix)
            sub = URING_SUBMIT_RE.search(line) or GEN_SUBMIT_RE.search(line)
            if sub and pid is not None:
                st = backends.setdefault(pid, BackendStats(pid))
                st.submit_lines += 1
                st.submit_ios += int(sub.group("n"))
            continue

        key = pid if pid is not None else "?"
        st = backends.setdefault(key, BackendStats(key))
        st.lines += 1
        st.slot_ids[bm.group("id")] += 1
        st.ops[bm.group("op")] += 1
        msg = bm.group("msg")

        if RECLAIM_RE.match(msg):
            st.reclaims += 1
        m = UPDATE_STATE_RE.match(msg)
        if m:
            st.state_to[m.group("to")] += 1
        m = STAGED_RE.match(msg)
        if m:
            if m.group("sync") == "1":
                st.sync += 1
            else:
                st.async_ += 1
        m = MD_COMPLETE_RE.search(msg)
        if m:
            st.md_completions += 1
            b = int(m.group("bytes"))
            if m.group("status") != "OK" or b < 0 or int(m.group("edata")) != 0:
                st.md_errors += 1
            else:
                st.nblocks_hist[max(1, b // BLCKSZ)] += 1
        if FOREIGN_RE.search(msg):
            st.foreign_hits += 1
        if WAIT_INFLIGHT_RE.search(msg):
            st.wait_inflight += 1

        # opportunistic block-number capture (error/retry/description lines)
        bl = re.search(r"blocks? (\d+)(?:\.\.(\d+))?", msg)
        if bl:
            st.have_block_info = True
            lo = int(bl.group(1))
            hi = int(bl.group(2)) if bl.group(2) else lo
            for b in range(lo, hi + 1):
                st.block_reqs[b] += 1

    return backends


def fmt_hist(counter, unit=""):
    if not counter:
        return "  (none)"
    out = []
    for k in sorted(counter):
        out.append(f"    {k}{unit}: {counter[k]}")
    return "\n".join(out)


def report(st, only):
    def want(sec):
        return not only or sec in only

    print(f"==== backend pid {st.pid} ====")
    print(f"  AIO debug body lines parsed : {st.lines}")
    print(f"  completed IO lifecycles     : {st.reclaims}  (count of 'reclaiming:' lines)")

    if want("sync"):
        tot = st.sync + st.async_
        pct = (100.0 * st.async_ / tot) if tot else 0.0
        print(f"  staged synchronous          : {st.sync}")
        print(f"  staged asynchronous         : {st.async_}  ({pct:.1f}% async)")

    if want("submit"):
        avg = (st.submit_ios / st.submit_lines) if st.submit_lines else 0.0
        print(f"  submit calls / IOs submitted: {st.submit_lines} calls, "
              f"{st.submit_ios} IOs (avg batch {avg:.2f})")

    if want("sizes"):
        tot_ios = sum(st.nblocks_hist.values())
        tot_blocks = st.total_blocks_read()
        print(f"  md readv completions        : {st.md_completions} "
              f"({st.md_errors} error/partial)")
        print(f"  blocks-per-IO histogram (io_combine):")
        print(fmt_hist(st.nblocks_hist, " block(s)"))
        if tot_ios:
            print(f"    -> {tot_blocks} blocks in {tot_ios} IOs "
                  f"(avg {tot_blocks / tot_ios:.2f} blocks/IO)")

    if want("churn"):
        # "count >> read": how many block-accesses vs how many actual read IOs.
        # Without per-block info we express it as blocks-read / distinct... which
        # we can only do if block info is present. Otherwise report the proxy.
        if st.have_block_info:
            distinct = len(st.block_reqs)
            total_req = sum(st.block_reqs.values())
            ratio = (total_req / distinct) if distinct else 0.0
            print(f"  distinct blocks seen        : {distinct}")
            print(f"  total block requests        : {total_req}  "
                  f"(count>>read ratio {ratio:.2f}x)")
            rereq = Counter(v for v in st.block_reqs.values())
            print(f"  per-block re-request histogram (times requested -> #blocks):")
            print(fmt_hist(rereq))
        else:
            print("  block-churn metrics         : n/a "
                  "(block numbers not present in log; see LIMITATIONS)")
            print(f"    proxy: {st.total_blocks_read()} blocks read across "
                  f"{st.reclaims} IO lifecycles")

    if want("foreign"):
        print(f"  'foreign' (already-in-progress) mentions : {st.foreign_hits}")
        print(f"  'waiting for (free) IO' waits            : {st.wait_inflight}")

    if want("slots"):
        print(f"  distinct IO-handle slots used: {len(st.slot_ids)} "
              f"(slots are reused; not a per-IO key)")
        top = st.slot_ids.most_common(5)
        print("    busiest slots (id: lines): " +
              ", ".join(f"{i}:{c}" for i, c in top))

    if want("states"):
        print("  state-transition counts:")
        print(fmt_hist(st.state_to))
    print()


def main():
    ap = argparse.ArgumentParser(
        description="Aggregate PostgreSQL pgaio_debug server-log lines.")
    ap.add_argument("logs", nargs="*", help="log file(s); default stdin")
    ap.add_argument("--pid", help="only this backend pid")
    ap.add_argument("--pid-regex", default=None,
                    help=r"regex with one group capturing the pid "
                         r"(default '\[(\d+)\]' for log_line_prefix '%%m [%%p] ')")
    ap.add_argument("--since", help="ignore lines before this timestamp (string compare)")
    ap.add_argument("--until", help="ignore lines after this timestamp (string compare)")
    ap.add_argument("--only", help="comma list of sections to show: "
                    "sync,submit,sizes,churn,foreign,slots,states")
    args = ap.parse_args()

    pid_re = re.compile(args.pid_regex) if args.pid_regex else DEFAULT_PID_RE
    only = set(s.strip() for s in args.only.split(",")) if args.only else None

    if args.logs:
        lines = []
        for path in args.logs:
            with open(path, "r", errors="replace") as f:
                lines.extend(f.readlines())
    else:
        lines = sys.stdin.readlines()

    backends = parse_stream(lines, pid_re, args.pid, args.since, args.until)
    if not backends:
        print("no pgaio debug lines matched.", file=sys.stderr)
        return 1
    # busiest backend first
    for st in sorted(backends.values(), key=lambda s: -s.reclaims):
        report(st, only)
    return 0


if __name__ == "__main__":
    sys.exit(main())
