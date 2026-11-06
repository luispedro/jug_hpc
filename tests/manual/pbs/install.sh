#!/bin/bash
# Create venv/ next to this script with Jug, numpy and jug_hpc (from scratch:
# an existing venv/ is emptied first, as jug_hpc's version does not change
# between commits).
# Usage: ./install.sh /path/to/jug_hpc [/path/to/jug]
# Needs Python >= 3.11 (set PYTHON=... to choose it, e.g. after `module load`).
set -euo pipefail
cd -- "$(dirname -- "$0")"
jug_hpc=${1:?usage: $0 /path/to/jug_hpc [/path/to/jug]}
"${PYTHON:-python3}" -m venv --clear venv
venv/bin/pip install --upgrade pip
venv/bin/pip install numpy "${2:-jug}" "$jug_hpc"
venv/bin/jug-hpc --version
venv/bin/jug --version
