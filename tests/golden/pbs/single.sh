#!/bin/bash
#PBS -N jug-align-proj
#PBS -l select=1:ncpus=8:mem=16gb
#PBS -l walltime=01:00:00
#PBS -o logs/scheduler/
#PBS -e logs/scheduler/

set -e
cd -- /home/u/proj
JUG_HPC_JOB_ID="${PBS_JOBID%%[.[]*}"
mkdir -p -- /home/u/proj/logs
exec >>/home/u/proj/logs/align-"$JUG_HPC_JOB_ID".log 2>&1
export JUG_HPC_CLASS=align JUG_HPC_JOB_ID JUG_HPC_CPUS=8 OMP_NUM_THREADS=8
unset JUG_MAX_HOURS JUG_MAX_MINUTES
export JUG_MAX_SECONDS=3240
exec jug execute --name=align --nr-wait-cycles=15 --wait-cycle-time=12 --jugdir=/home/u/proj/jugfile.jugdata -- /home/u/proj/jugfile.py
