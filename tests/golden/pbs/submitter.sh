#!/bin/bash
#PBS -N jug-submitter-proj
#PBS -l select=1:ncpus=1:mem=2gb
#PBS -l walltime=168:00:00
#PBS -o logs/scheduler/
#PBS -e logs/scheduler/

set -e
cd -- /home/u/proj
JUG_HPC_JOB_ID="${PBS_JOBID%%[.[]*}"
mkdir -p -- /home/u/proj/logs
exec >>/home/u/proj/logs/submitter-"$JUG_HPC_JOB_ID".log 2>&1
export JUG_HPC_CLASS=submitter JUG_HPC_JOB_ID JUG_HPC_CPUS=1 OMP_NUM_THREADS=1
exec jug-hpc submit --interval=60
