#!/bin/bash
#PBS -N jhpc-exit3
#PBS -l select=1:ncpus=1:mem=1gb
#PBS -l walltime=00:10:00
#PBS -j oe

exit 3
