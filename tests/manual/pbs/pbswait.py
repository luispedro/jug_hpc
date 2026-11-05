#!/usr/bin/env python3
'''Wait until PBS jobs reach one of some states

Usage: pbswait.py STATES TIMEOUT JOBID...

STATES are ``job_state`` letters, e.g. ``RBEFX`` (running or later) or ``F``
(finished). A job that ``qstat -x -t`` does not list counts as gone, i.e. in
every state. ``-t`` lists array elements (``1234[3].server``) too.
Prints each job's state when it changes. Exits with 1 after TIMEOUT seconds.
'''
import json
import re
import subprocess
import sys
import time

POLL = 15


def key(job_id):
    '''1234[2].server -> 1234[2] (the server part may be written differently)'''
    return re.match(r'[^.]*', job_id).group(0)


def states(jobs):
    '''Job ID -> job_state ('-' if qstat does not list it, None if qstat failed)'''
    proc = subprocess.run(['qstat', '-f', '-F', 'json', '-x', '-t', *jobs],
                          check=False, capture_output=True, text=True)
    if not proc.stdout.strip() and 'Unknown Job' not in proc.stderr:
        print(f'pbswait: qstat failed: {proc.stderr.strip()}', flush=True)
        return dict.fromkeys(jobs)
    found = {}
    if proc.stdout.strip():
        for job_id, attrs in json.loads(proc.stdout, strict=False).get('Jobs', {}).items():
            found[key(job_id)] = attrs.get('job_state', '?')
    return {j: found.get(key(j), '-') for j in jobs}


def main(wanted, timeout, jobs):
    if not jobs:
        return 0
    wanted += '-'
    deadline = time.time() + timeout
    last = {}
    while True:
        now = states(jobs)
        for j, s in now.items():
            if s != last.get(j):
                print(f'pbswait: {time.strftime("%H:%M:%S")} {j}: {s}', flush=True)
        last = now
        if all(s is not None and s in wanted for s in now.values()):
            return 0
        if time.time() > deadline:
            print(f'pbswait: timed out waiting for {wanted} after {timeout} s', flush=True)
            return 1
        time.sleep(POLL)


if __name__ == '__main__':
    sys.exit(main(sys.argv[1], float(sys.argv[2]), sys.argv[3:]))
