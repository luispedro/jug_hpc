# Independent tasks in two classes: suits `jug-hpc submit --dry-run` / `--all`.
import os
import socket
import time

from jug import TaskGenerator


def where():
    return {
        'class': os.environ.get('JUG_HPC_CLASS'),
        'cpus': os.environ.get('JUG_HPC_CPUS'),
        'job': os.environ.get('PBS_JOBID') or os.environ.get('JUG_HPC_JOB_ID'),
        'host': socket.gethostname(),
    }


@TaskGenerator
def work(i):
    time.sleep(60)
    return where()


@TaskGenerator
def single(i):
    time.sleep(60)
    return where()


results = [work(i) for i in range(6)]
singles = [single(i) for i in range(2)]
