# Tasks too long to finish before `jug-hpc cancel` (step 6 of README.md)
# cancels the submitter job and its workers.
import time

from jug import TaskGenerator


@TaskGenerator
def slow(i):
    time.sleep(1200)
    return i


[slow(i) for i in range(4)]
