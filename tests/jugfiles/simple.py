from jug import TaskGenerator


@TaskGenerator
def load(i):
    return i


@TaskGenerator
def align_sample(x):
    return x * 2


@TaskGenerator
def merge(xs):
    return sum(xs)


aligned = [align_sample(load(i)) for i in range(3)]
total = merge(aligned)
