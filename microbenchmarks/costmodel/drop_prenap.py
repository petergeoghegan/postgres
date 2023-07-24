#!/usr/bin/env python3
"""Remove cold rows that are not trustworthy:
  - recorded before the NVMe heartbeat started (they include the drive's
    ~8 ms power-state wake-up), and
  - recorded while the user's dm-delay creation copied 45 GB on the same
    NVMe (12:20:15 .. 12:23:10 on 2026-10-12).
The harness re-runs dropped cells on resume (it keys on cell+cache+eic+run)."""
import json, os
W = "/mnt/nvme/postgresql/patch/claude_work/costmodel"
start = open(f"{W}/heartbeat_started.txt").read().strip()
windows = [("2026-10-12T12:20:15", "2026-10-12T12:23:10")]
path = f"{W}/results/timing.jsonl"
keep, drop_nap, drop_win = [], 0, 0
for line in open(path):
    r = json.loads(line)
    if r["cache"] == "cold":
        if r["ts"] < start:
            drop_nap += 1
            continue
        if any(a <= r["ts"] <= b for a, b in windows):
            drop_win += 1
            continue
    keep.append(line)
os.replace(path, path + ".bak")
with open(path, "w") as f:
    f.writelines(keep)
print(f"dropped {drop_nap} pre-heartbeat and {drop_win} copy-window cold rows, kept {len(keep)}")
