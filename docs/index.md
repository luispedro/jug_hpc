# jug_hpc: Jug on HPC clusters, with workers sized per task type

```{warning}
jug_hpc is in early development and has not had a release yet. The commands
described here work, and the PBS backend has been tested on one real PBS
Professional cluster as well as a simulated one. SLURM is not supported
yet.
```

## What is jug_hpc?

[Jug](https://jug.readthedocs.io) already works on HPC clusters. Submit a job
array of identical `jug execute` workers and they coordinate through the
jugdir. This stops working well when tasks need very different resources. If
one step needs 64 GB and 12 hours and another needs 1 GB and 5 minutes, every
worker has to be sized for the largest task. That wastes your allocation and
lowers your priority in the queue.

jug_hpc lets you describe the resources that each *type* of task needs, in a
small TOML file next to your jugfile. It then submits workers of the right size
to the scheduler, and submits them only when their tasks are ready to run.

jug_hpc follows three principles:

1. **Your jugfile stays unchanged.** All scheduling information lives in
   separate configuration files, so the same jugfile still runs with plain
   `jug execute` on a laptop.
2. **It builds on Jug and does not replace it.** Workers are ordinary
   `jug execute --name ...` processes. Tasks are matched to resource classes
   with Jug's own `--name`/`--pattern` rules, and locking, results and status
   all come from Jug.
3. **It is transparent.** `jug-hpc submit --dry-run` shows the exact scheduler
   scripts and the commands that would submit them. You can read them, and you
   can submit them by hand.

## A short example

Take a jugfile that aligns 20 samples, counts each alignment and merges the
counts:

```python
from jug import TaskGenerator

@TaskGenerator
def align(sample):
    ...

@TaskGenerator
def count(bam):
    ...

@TaskGenerator
def merge(counts):
    ...

samples = [f's{i}' for i in range(20)]
counts = [count(align(s)) for s in samples]
total = merge(counts)
```

Alignment needs 8 CPUs and 32 GB, merging needs 128 GB on a special queue, and
counting is cheap. `jug_hpc.toml`, next to the jugfile, says so:

```toml
jugfile = "jugfile.py"

[default]               # every task no class matches (here: count)
memory  = "4G"
time    = "1:00:00"
workers = 50

[class.align]
match   = "align"       # same rules as `jug execute --name`
cpus    = 8
memory  = "32G"
time    = "4h"
workers = 10

[class.merge]
match     = "merge"
memory    = "128G"
time      = "12:00:00"
partition = "bigmem"
```

Then:

```bash
jug-hpc check            # which class runs which tasks
jug-hpc submit --dry-run # the scripts that would be submitted
jug-hpc submit           # submit workers as tasks become ready, until done
```

`jug-hpc submit` starts 10 `align` workers. As alignments finish, it starts
`default` workers for the counts, and when every count is done it starts one
`merge` worker. It exits when every task is finished. The {doc}`tutorial` goes
through this example step by step.

## Contents

```{toctree}
:maxdepth: 2

tutorial
concepts
configuration
submitting
schedulers
faq
llms
```
