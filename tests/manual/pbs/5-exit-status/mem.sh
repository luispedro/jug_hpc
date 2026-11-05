#!/bin/bash
#PBS -N jhpc-mem
#PBS -l select=1:ncpus=1:mem=1gb
#PBS -l walltime=00:10:00
#PBS -j oe
# allocates and touches 4 GB, far over its 1 GB
exec python3 -c 'import time; x = b"x" * (4 << 30); time.sleep(60)'
