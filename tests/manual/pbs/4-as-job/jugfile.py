# A default -> align -> merge -> default chain, each step waiting for the last.
# Each task records where and when it ran; ./check.py prints and checks it.
import os
import socket
import time

from jug import TaskGenerator


def run(seconds):
    start = time.time()
    time.sleep(seconds)
    return {
        'class': os.environ.get('JUG_HPC_CLASS'),
        'cpus': os.environ.get('JUG_HPC_CPUS'),
        'job': os.environ.get('PBS_JOBID') or os.environ.get('JUG_HPC_JOB_ID'),
        'host': socket.gethostname(),
        'start': start,
        'end': time.time(),
    }


@TaskGenerator
def prepare(i):
    return [run(60)]


@TaskGenerator
def align(prev):
    return prev + [run(120)]


@TaskGenerator
def merge(aligned):
    return {'aligned': aligned, 'merge': run(60)}


@TaskGenerator
def report(merged):
    return dict(merged, report=run(10))


result = report(merge([align(prepare(i)) for i in range(4)]))
