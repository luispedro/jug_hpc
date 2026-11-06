# Two tasks killed by the scheduler (step 7 of README.md), then a task needing both.
# With too-small.toml as jug_hpc.toml, `hog` goes over its memory (the cgroup's
# OOM killer: SIGKILL, so its Jug lock is left behind) and `slow` over its wall
# time (SIGTERM: Jug releases the lock, so the submitter tries again, until the
# class is stopped). With fixed.toml both fit.
import time

from jug import TaskGenerator


@TaskGenerator
def hog():
    x = b'x' * (3 << 30)    # 3 GB, touched
    time.sleep(30)
    return len(x)


@TaskGenerator
def slow():
    time.sleep(240)
    return 240


@TaskGenerator
def final(n, s):
    return n + s


final(hog(), slow())
