#!/usr/bin/env python3
'''Check a finished run of 2-three-classes (or 4-as-job)

Usage: venv/bin/python check.py 2-three-classes

Prints where each task ran, checks that it ran in the right class with the
right CPUs, and, from ``qstat -f -F json -x -t`` (needs job history), how long
each worker waited between starting and running its first task.
'''
import json
import os
import re
import subprocess
import sys
import time

import jug
from jug import value

EXPECTED = {'prepare': ('default', '1'), 'align': ('align', '2'),
            'merge': ('merge', '3'), 'report': ('default', '1')}


def job_key(job_id):
    '''1234[2].server -> 1234[2] (the server part may be written differently)'''
    return re.match(r'[^.]*', job_id or '').group(0)


def start_times(job_ids):
    '''Job (or array element) -> start time, from qstat -x -t'''
    if not job_ids:
        return {}
    try:
        proc = subprocess.run(['qstat', '-f', '-F', 'json', '-x', '-t', *job_ids],
                              check=False, capture_output=True, text=True)
    except FileNotFoundError:
        print('no qstat: no worker wait times')
        return {}
    if not proc.stdout.strip():
        print(f'qstat gave no output ({proc.stderr.strip()}): no worker wait times')
        return {}
    starts = {}
    for job_id, attrs in json.loads(proc.stdout, strict=False).get('Jobs', {}).items():
        if 'stime' in attrs:
            starts[job_key(job_id)] = time.mktime(
                time.strptime(attrs['stime'], '%a %b %d %H:%M:%S %Y'))
    return starts


def main(project):
    os.chdir(project)
    sys.path.insert(0, '.')
    _, space = jug.init('jugfile.py', 'jugfile.jugdata')
    if not space['result'].can_load():
        print(f'FAILED: the final task (report) has not run; see {project}/logs/')
        return 1
    res = value(space['result'])
    tasks = [(f'prepare[{i}]', a[0]) for i, a in enumerate(res['aligned'])]
    tasks += [(f'align[{i}]', a[1]) for i, a in enumerate(res['aligned'])]
    tasks += [('merge', res['merge']), ('report', res['report'])]
    t0 = min(r['start'] for _, r in tasks)
    errors = []
    print(f'{"task":<11} {"class":<8} cpus {"job":<24} {"host":<16} start    end')
    for name, r in tasks:
        print(f'{name:<11} {r["class"]:<8} {r["cpus"]:>4} {r["job"]!s:<24} {r["host"]:<16} '
              f'{r["start"] - t0:7.0f}s {r["end"] - t0:6.0f}s')
        if (r['class'], r['cpus']) != EXPECTED[name.split('[')[0]]:
            errors.append(f'{name} ran in class {r["class"]} with {r["cpus"]} CPUs, '
                          f'expected {EXPECTED[name.split("[")[0]]}')
    with open('.jug_hpc/jobs.json') as f:
        records = json.load(f)['jobs']
    print('\njobs submitted:')
    for r in records:
        print(f'  {r["job_id"]:<24} {r["class"]:<10} {r["workers"]} worker(s)  {r["submitted"]}')
    starts = start_times([r['job_id'] for r in records])
    if starts:
        first = {}
        for _, r in tasks:
            key = job_key(r['job'])
            first[key] = min(first.get(key, r['start']), r['start'])
        print('\nworker start -> its first task (no task: the worker found nothing to run):')
        for key, start in sorted(starts.items()):
            if key.endswith('[]'):
                continue        # an array job itself, not a worker
            if key.endswith('[]'):
                continue    # an array job's own entry
            if key in first:
                print(f'  {key:<16} {first[key] - start:6.0f}s')
            else:
                print(f'  {key:<16}   no task')
    print()
    for e in errors:
        print('ERROR:', e)
    print('FAILED' if errors else 'OK: every task ran in its class')
    return 1 if errors else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else '2-three-classes'))
