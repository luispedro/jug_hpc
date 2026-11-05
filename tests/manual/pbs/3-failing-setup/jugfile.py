from jug import TaskGenerator


@TaskGenerator
def work(i):
    return i


results = [work(i) for i in range(4)]
