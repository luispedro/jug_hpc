from jug import TaskGenerator, bvalue


@TaskGenerator
def split(n):
    return list(range(n))


@TaskGenerator
def process(x):
    return x + 1


results = [process(b) for b in bvalue(split(2))]
