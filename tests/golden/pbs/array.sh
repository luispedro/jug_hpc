#!/bin/bash
#PBS -N jug-align-proj
#PBS -l select=1:ncpus=8:mem=16gb:ngpus=1:host_shared=true
#PBS -l walltime=01:00:00
#PBS -l place=scatter:excl
#PBS -q long
#PBS -A grant42
#PBS -J 1-4
#PBS -o logs/scheduler/
#PBS -e logs/scheduler/
#PBS -m n
#PBS -W group_list=lab

set -e
cd -- /home/u/proj
JUG_HPC_JOB_ID="${PBS_JOBID%%[.[]*}"
JUG_HPC_ARRAY_INDEX="$PBS_ARRAY_INDEX"
mkdir -p -- /home/u/proj/logs
exec >>/home/u/proj/logs/align-"$JUG_HPC_JOB_ID"-"$JUG_HPC_ARRAY_INDEX".log 2>&1
export JUG_HPC_CLASS=align JUG_HPC_JOB_ID JUG_HPC_ARRAY_INDEX JUG_HPC_CPUS=8 OMP_NUM_THREADS=8

module load python/3.12

source venv/bin/activate

unset JUG_MAX_HOURS JUG_MAX_MINUTES
export JUG_MAX_SECONDS=3240
exec jug execute --name=align --nr-wait-cycles=15 --wait-cycle-time=12 --jugdir=/home/u/proj/jugfile.jugdata -- /home/u/proj/jugfile.py
