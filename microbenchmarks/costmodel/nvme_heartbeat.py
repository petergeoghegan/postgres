#!/usr/bin/env python3
"""Keep the NVMe awake: one O_DIRECT 8 kB read of a random block every 50 ms."""
import os, mmap, random, time, sys
path = sys.argv[1]
fd = os.open(path, os.O_RDONLY | os.O_DIRECT)
buf = mmap.mmap(-1, 8192)
n = os.fstat(fd).st_size // 8192
while True:
    os.preadv(fd, [buf], random.randrange(n) * 8192)
    time.sleep(0.05)
