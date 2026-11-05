# Schedulers

The `scheduler` key of `cluster.toml` chooses the backend that submits and
queries jobs. jug_hpc comes with two:

| `scheduler` | For |
|---|---|
| `"pbs"` | OpenPBS and PBS Professional clusters |
| `"local"` | Running workers on this machine: laptops, workstations, trying things out |

A SLURM backend is planned. Backends are found through the
`jug_hpc.schedulers` entry point group, so other packages can add more.

## PBS

The PBS backend supports OpenPBS and PBS Professional. Torque's `qsub` and
`qstat` are different and are not supported.

```{note}
The PBS backend has been tested on one PBS Professional cluster (version
2026.1, with cgroups), besides a simulated `qsub`/`qstat` in the tests. Please
report any problem, especially with other versions or with OpenPBS.
```

### Job scripts

Workers are submitted with `qsub <script>`, run from the project directory.
The script's header is built from the class's settings:

```bash
#PBS -N jug-<class>-<project>
#PBS -l select=1:ncpus=<cpus>:mem=<memory>
#PBS -l walltime=HH:MM:SS
#PBS -q <partition>          # if set
#PBS -A <account>            # if set
#PBS -J 1-<workers>          # only if there are several workers
#PBS -o logs/scheduler/
#PBS -e logs/scheduler/
```

- `<project>` is the name of the project directory.
- Memory is written in PBS units: `16G` becomes `16gb`.
- Several workers are submitted as one job array (`-J 1-<n>`), whose elements
  find their index in `PBS_ARRAY_INDEX`. A single worker is a plain job,
  since some PBS installations reject arrays of one element.

The submitter queries jobs with `qstat -f -F json -x`. For an array job, it
counts the elements still queued or running, so an array whose elements end at
different times only counts the workers that are left. When a job ends, it
reads how it ended with `qstat -f -F json -x -t` (so array elements are listed
with their own `Exit_status`); a job whose workers keep failing stops the
submissions for its class (see {doc}`submitting`). This needs the PBS server
to keep job history (`job_history_enable`); without it, a finished job is
simply gone, and how it ended is not known.

How a job ended is read from its `Exit_status` (and `substate`):

| PBS reports | jug_hpc reports |
|---|---|
| `0` | success |
| `-29`, or a job that used all of its `walltime` | killed for wall time |
| `-27`, `-26` | killed for memory |
| `128 + N` or `256 + N` | killed by signal `N` |
| `137` or `265` (SIGKILL) | killed by SIGKILL, *probably for using more memory than requested* |
| substate 91 | deleted with `qdel` |
| other values | failed, with that exit status |

The SIGKILL case is a guess: on clusters that enforce memory limits with
cgroups, a job that uses too much memory is killed by the kernel, and PBS
records nothing but the SIGKILL (the job's PBS output file, in
`logs/scheduler/`, may say more).

### Raw options

PBS options don't fit in one flat list of keys, so the `[pbs]` tables (in
`cluster.toml`, or `[default.pbs]`, `[class.<name>.pbs]` and `[submitter.pbs]`
in `jug_hpc.toml`) have three keys:

`select`
: A table of extra chunk resources, appended to the `select` statement:
  `select = { ngpus = 1 }` gives `-l select=1:ncpus=8:mem=32gb:ngpus=1`.

`resources`
: A table of extra job-wide `-l` resources: `resources = { place = "scatter" }`
  gives `#PBS -l place=scatter`.

`directives`
: A list of `qsub` options, each written as a `#PBS` line as is:
  `directives = ["-m n", "-W group_list=mygroup"]`. A leading `#PBS` is
  optional.

```toml
# jug_hpc.toml
[class.train]
match  = "train"
cpus   = 4
memory = "64G"
time   = "1-00:00"

[class.train.pbs]
select     = { ngpus = 1, gpu_model = "a100" }
directives = ["-m ae"]
```

The resources that jug_hpc sets itself (`ncpus`, `mem`, `walltime`,
`select`) can't be set as raw resources. Use `cpus`, `memory` and `time`
instead: the worker script relies on them for `JUG_HPC_CPUS` and
`JUG_MAX_SECONDS`.

When a class and the cluster both set the same key, the class's value wins.

Options that every job on a cluster needs, such as a project with `-P`, go in
`cluster.toml`:

```toml
[pbs]
directives = ["-P my_project"]
```

As a class's `directives` replace the cluster's, a class that sets its own
must repeat them.

(local-backend)=
## Local

`scheduler = "local"` runs workers as processes on this machine, using the same
worker scripts as a cluster would. Use it to try jug_hpc on a laptop, or to
run a jugfile with several classes on a workstation:

```toml
# cluster.toml, next to jug_hpc.toml
scheduler = "local"
setup = ""          # clear the cluster's setup (module load ...), if any
```

All the workers of a job start at the same time. Only the wall time is
enforced: a worker still running after `time` gets `SIGTERM`, then `SIGKILL`.
`cpus` and `memory` only set the worker's environment variables, and
`partition` and `account` are ignored. The local backend has no raw options.

Its job records live in `$XDG_STATE_HOME/jug_hpc/local/` (by default
`~/.local/state/jug_hpc/local/`). Like a scheduler's queue, they are shared by
all your projects.
