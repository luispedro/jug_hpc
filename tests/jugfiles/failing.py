from jug import TaskGenerator


@TaskGenerator
def before(x):
    return x


before(1)
raise RuntimeError('broken jugfile')
