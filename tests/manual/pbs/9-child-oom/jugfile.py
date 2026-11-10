# A task whose subprocess goes over the memory of its job (step 9 of README.md).
# With too-small.toml as jug_hpc.toml, the cgroup's OOM killer kills the
# subprocess, not Jug: the task fails (CalledProcessError, "died with
# <Signals.SIGKILL: 9>"), Jug exits 1, and PBS records Exit_status 1 with
# memory use at the limit. With fixed.toml it fits.
import subprocess
import sys

from jug import TaskGenerator

# grows by 64 MB every 2 s up to 4 GB (slowly, so that sampled memory use sees
# it near the limit), then holds it for 10 s
GROW = '''
import time
chunks = []
for _ in range(64):
    chunks.append(b"x" * (64 << 20))
    time.sleep(2)
time.sleep(10)
'''


@TaskGenerator
def child():
    subprocess.run([sys.executable, '-c', GROW], check=True)
    return 4096


@TaskGenerator
def final(n):
    return n


final(child())
