#!/bin/bash
#PBS -N jhpc-qdel
#PBS -l select=1:ncpus=1:mem=1gb
#PBS -l walltime=00:20:00
#PBS -j oe
# qdel it once it is running
exec sleep 600
