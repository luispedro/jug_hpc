# A failed task that another depends on (step 8 of README.md). Once the `good`
# tasks have run, no task can run: the submitter stops and cancels the workers
# still waiting (for `total`, which can never run) or queued.
import time

from jug import TaskGenerator


@TaskGenerator
def bad():
    raise ValueError('this task fails on purpose')


@TaskGenerator
def good(i):
    time.sleep(150)
    return i


@TaskGenerator
def total(b, goods):
    return b + sum(goods)


total(bad(), [good(i) for i in range(2)])
