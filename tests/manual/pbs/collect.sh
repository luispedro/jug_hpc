#!/bin/bash
# Record qstat output for jug_hpc's tests (tests/pbs_output/).
# Usage: ./collect.sh NAME JOBID...      (job IDs as qsub printed them)
# Writes, in recorded/:
#   NAME.json, NAME.err, NAME.status        qstat -f -F json -x JOBID...
#   NAME-t.json, NAME-t.err, NAME-t.status  qstat -f -F json -x -t JOBID...
# These are the commands the PBS backend runs (query and outcome).
set -u
cd -- "$(dirname -- "$0")" || exit 1
mkdir -p recorded
name=${1:?usage: $0 NAME JOBID...}
shift
qstat -f -F json -x "$@" >"recorded/$name.json" 2>"recorded/$name.err"
echo $? >"recorded/$name.status"
qstat -f -F json -x -t "$@" >"recorded/$name-t.json" 2>"recorded/$name-t.err"
echo $? >"recorded/$name-t.status"
for f in "recorded/$name.json" "recorded/$name-t.json"; do
    if [ -s "$f" ]; then
        python3 -c 'import json, sys; json.loads(open(sys.argv[1]).read(), strict=False)' "$f" \
            && echo "$f: valid JSON" || echo "$f: INVALID JSON (keep it, that is useful too)"
    fi
done
echo "exit status: $(cat "recorded/$name.status") (-x), $(cat "recorded/$name-t.status") (-x -t)"
