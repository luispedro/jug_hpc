#!/bin/bash
#PBS -N jhpc-walltime
#PBS -l select=1:ncpus=1:mem=1gb
#PBS -l walltime=00:02:00
#PBS -j oe
# runs past its 2 minutes
exec sleep 600
