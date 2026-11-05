#!/bin/bash
# Run every step of README.md unattended, recording the results in this directory.
#
# Usage (on a login node; it takes an hour or more, so use tmux/screen or nohup):
#   RPID=<project> ./run-everything.sh [/path/to/jug_hpc [/path/to/jug]]
#   ./run-everything.sh --clean          # remove the results of an earlier run
#   STEPS="2 4" RPID=<project> ./run-everything.sh   # only these steps, adding to
#                                        # the results of an earlier run
#
# The jug_hpc path is needed only the first time (to create venv/ with install.sh).
# If ~/.config/jug_hpc/cluster.toml does not exist, it is created from
# cluster.toml.example with -P $RPID.
# WAIT (seconds, default 14400) caps each wait for jobs, including queueing.
#
# Afterwards, copy this directory back without venv/, e.g.
#   rsync -a --exclude venv/ <this dir>/ me@host:jug_hpc/tests/manual/pbs/
set -u
KIT=$(cd -- "$(dirname -- "$0")" && pwd)
cd -- "$KIT" || exit 1

if [ "${1:-}" = --clean ]; then
    rm -rf -- recorded run.log */jugfile.jugdata */.jug_hpc */logs */submit.log */check.txt \
        1-static/dry-run.txt 1-static/jug-status.txt 1-static/classes.txt 5-exit-status/jhpc-*
    exit 0
fi
: "${RPID:?set RPID to the project for qsub -P, e.g. RPID=ab12 $0}"
WAIT=${WAIT:-14400}
if [ -e recorded ] && [ -z "${STEPS:-}" ]; then
    echo "error: results of an earlier run are here; move them away, run $0 --clean," \
         "or choose steps to rerun with STEPS" >&2
    exit 1
fi
STEPS=${STEPS:-1 2 3 4 5}
mkdir -p recorded
exec > >(tee -a run.log) 2>&1

log() { echo "=== $(date '+%F %T') $*"; }
W() { python "$KIT/pbswait.py" "$1" "$WAIT" "${@:2}"; }
collect() { "$KIT/collect.sh" "$@"; }
# job IDs in .jug_hpc/jobs.json (optionally only those of one class)
job_ids() {
    [ -e .jug_hpc/jobs.json ] || return 0
    python -c 'import json, sys
print(*(j["job_id"] for j in json.load(open(".jug_hpc/jobs.json"))["jobs"]
        if len(sys.argv) < 2 or j["class"] == sys.argv[1]))' "$@"
}

# --- setup ---------------------------------------------------------------------
log setup
if [ ! -x venv/bin/jug-hpc ]; then
    if [ $# -lt 1 ]; then
        echo "error: no venv/ yet: give the path to jug_hpc: $0 /path/to/jug_hpc" >&2
        exit 1
    fi
    ./install.sh "$@" || exit 1
fi
# shellcheck disable=SC1091
source venv/bin/activate
[ -n "${XDG_CONFIG_HOME:-}" ] && echo "warning: XDG_CONFIG_HOME is set; PBS jobs won't see it"
cfg=${XDG_CONFIG_HOME:-$HOME/.config}/jug_hpc/cluster.toml
if [ ! -e "$cfg" ]; then
    mkdir -p -- "$(dirname -- "$cfg")"
    sed "s/myproject/$RPID/" cluster.toml.example >"$cfg"
    echo "created $cfg"
fi
grep -q -- "-P $RPID" "$cfg" || echo "warning: $cfg does not contain -P $RPID"
cp -- "$cfg" recorded/cluster.toml
{
    echo "--- steps: $STEPS"
    date; hostname; uname -a
    qstat --version
    qmgr -c 'print server' | grep -i -e history -e version
    python --version; jug-hpc --version; jug --version; pip freeze
} >>recorded/env.txt 2>&1
qstat -Qf >>recorded/queues.txt 2>&1    # queue limits (resources_min/max), for rejected jobs
jug-hpc -C 1-static config >recorded/config.txt 2>&1 || { cat recorded/config.txt; exit 1; }

# --- 1. static submission, by hand ------------------------------------------------
step1() (
    cd 1-static || exit 1
    jug-hpc check >classes.txt 2>&1
    jug-hpc submit --dry-run >dry-run.txt 2>&1 || { cat dry-run.txt; exit 1; }
    ids=()
    while read -r cmd; do
        id=$(eval "$cmd") || exit 1
        echo "$cmd -> $id"
        ids+=("$id")
    done < <(sed -n 's/^ *\(qsub .*\)$/\1/p' dry-run.txt)
    collect queued "${ids[@]}"
    W RBEFX "${ids[@]}" && collect running "${ids[@]}"
    W F "${ids[@]}" || exit 1
    collect finished "${ids[@]}"
    collect unknown 99999999
    collect mixed "${ids[0]}" 99999999
    jug status >jug-status.txt 2>&1
    cat jug-status.txt
)

# --- 2. exit statuses -------------------------------------------------------------
step2() (
    cd 5-exit-status || exit 1
    declare -A id
    rejected=0
    for s in ok exit3 mem walltime array-ok qdel array; do
        if id[$s]=$(qsub -P "$RPID" "$s.sh"); then
            echo "$s.sh -> ${id[$s]}"
        else
            echo "$s.sh: rejected by qsub"
            unset "id[$s]"
            rejected=1
        fi
    done
    [ ${#id[@]} -gt 0 ] || exit 1
    if [ -n "${id[qdel]:-}" ]; then
        W RBEFX "${id[qdel]}" && qdel "${id[qdel]}"
    fi
    if [ -n "${id[array]:-}" ]; then
        element=${id[array]/\[\]/[3]}
        W RBEFX "$element" && qdel "$element"
    fi
    W F "${id[@]}" || exit 1
    for s in "${!id[@]}"; do
        collect "exit-$s" "${id[$s]}"
    done
    [ "$rejected" = 0 ]
)

# --- 3. three classes, polling submitter --------------------------------------------
step3() (
    cd 2-three-classes || exit 1
    timeout "$WAIT" jug-hpc submit --interval=30 2>&1 | tee submit.log
    st=${PIPESTATUS[0]}
    echo "exit status: $st" >>submit.log
    # shellcheck disable=SC2046
    W F $(job_ids)
    python ../check.py . 2>&1 | tee check.txt
    # shellcheck disable=SC2046
    collect three-classes $(job_ids)
    [ "$st" = 0 ] && grep -q '^OK' check.txt
)

# --- 4. failing setup -----------------------------------------------------------
step4() (
    cd 3-failing-setup || exit 1
    timeout 3600 jug-hpc submit --interval=20 2>&1 | tee submit.log
    st=${PIPESTATUS[0]}
    echo "exit status: $st" >>submit.log
    # shellcheck disable=SC2046
    [ "$st" = 124 ] && qdel $(job_ids)
    # shellcheck disable=SC2046
    W F $(job_ids)
    # shellcheck disable=SC2046
    collect failing-setup $(job_ids)
    [ "$st" = 1 ]    # expected: stopped because the workers keep failing
)

# --- 5. the submitter as a job ------------------------------------------------------
step5() (
    cd 4-as-job || exit 1
    jug-hpc submit --as-job 2>&1 | tee submit.log
    [ "${PIPESTATUS[0]}" = 0 ] || exit 1
    # shellcheck disable=SC2046
    W F $(job_ids submitter) || exit 1
    # shellcheck disable=SC2046
    W F $(job_ids)
    python ../check.py . 2>&1 | tee check.txt
    # shellcheck disable=SC2046
    collect as-job $(job_ids)
    grep -q '^OK' check.txt
)

echo "--- run of $(date '+%F %T'), steps: $STEPS" >>recorded/summary.txt
for n in $STEPS; do
    log "step $n"
    if "step$n"; then r=OK; else r=FAILED; fi
    log "step $n: $r"
    echo "step $n: $r" >>recorded/summary.txt
done
log finished
cat recorded/summary.txt
