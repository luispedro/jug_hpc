#!/bin/bash
# jug_hpc local worker: jug-align-proj
# cpus=8 memory=16G time=01:00:00 workers=1
# (the local backend enforces only the wall time)

set -e
cd -- /home/u/proj
JUG_HPC_JOB_ID="${JUG_HPC_LOCAL_JOB_ID:-$$}"
mkdir -p -- /home/u/proj/logs
exec >>/home/u/proj/logs/align-"$JUG_HPC_JOB_ID".log 2>&1
export JUG_HPC_CLASS=align JUG_HPC_JOB_ID JUG_HPC_CPUS=8 OMP_NUM_THREADS=8
unset JUG_MAX_HOURS JUG_MAX_MINUTES
export JUG_MAX_SECONDS=3240
exec jug execute --name=align --nr-wait-cycles=15 --wait-cycle-time=12 --jugdir=/home/u/proj/jugfile.jugdata -- /home/u/proj/jugfile.py
