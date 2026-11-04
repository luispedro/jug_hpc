from jug import TaskGenerator, barrier


@TaskGenerator
def split(n):
    return list(range(n))


@TaskGenerator
def process(x):
    return x + 1


blocks = split(3)
barrier()
results = [process(b) for b in blocks.value()]
