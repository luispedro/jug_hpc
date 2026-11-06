#!/bin/bash
# Run every step of README.md unattended, recording the results in this directory.
#
# Usage (on a login node; it takes a few hours, so use tmux/screen or nohup):
#   RPID=<project> ./run-everything.sh [/path/to/jug_hpc [/path/to/jug]]
#   STEPS="2 4" RPID=<project> ./run-everything.sh   # only these steps, adding to
#                                        # the results of an earlier run
#   ./run-everything.sh --clean          # remove the results of an earlier run
#
# A full run (no STEPS) starts from scratch: the results of an earlier run, if
# any, are first moved into old-results-<date>.tgz.
# venv/ is (re)created at every run (install.sh) from the jug_hpc checkout
# given, or else from the checkout this directory is in, so the code tested is
# the checkout's. With neither, an existing venv/ is used as it is.
# If ~/.config/jug_hpc/cluster.toml does not exist, it is created from
# cluster.toml.example with -P $RPID.
# WAIT (seconds, default 14400) caps each wait for jobs, including queueing.
#
# At the end, everything worth sending back is in pbs-results.tgz.
set -u
KIT=$(cd -- "$(dirname -- "$0")" && pwd)
cd -- "$KIT" || exit 1

# what a run leaves here (everything else is in git). No nullglob: job IDs of
# arrays (1234[].server) are glob patterns, and $(job_ids) is unquoted.
result_paths() {
    local p
    for p in recorded run.log pbs-results.tgz [0-9]*/jugfile.jugdata [0-9]*/.jug_hpc \
             [0-9]*/logs [0-9]*/*.log [0-9]*/*.txt 5-exit-status/jhpc-* 7-kills/jug_hpc.toml; do
        [ -e "$p" ] && echo "$p"
    done
}

if [ "${1:-}" = --clean ]; then
    # shellcheck disable=SC2046
    rm -rf -- $(result_paths)
    exit 0
fi
: "${RPID:?set RPID to the project for qsub -P, e.g. RPID=ab12 $0}"
WAIT=${WAIT:-14400}
if [ -z "${STEPS:-}" ] && [ -n "$(result_paths)" ]; then
    old=old-results-$(date +%Y%m%d-%H%M%S).tgz
    # shellcheck disable=SC2046
    tar czf "$old" $(result_paths) || exit 1
    # shellcheck disable=SC2046
    rm -rf -- $(result_paths)
    echo "moved the results of an earlier run into $old"
fi
STEPS=${STEPS:-1 2 3 4 5 6 7 8}
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
# want DESCRIPTION COMMAND...: an expected result; any not met fails the step
# (`bad` is local to each step, as the steps run in subshells)
want() {
    local what=$1
    shift
    if "$@" >/dev/null 2>&1; then
        echo "    ok: $what"
    else
        echo "    NOT AS EXPECTED: $what"
        bad=1
    fi
}
# `jug-hpc status` every 2 minutes, appended to status.log, until killed
status_loop() {
    while :; do
        { echo "--- $(date '+%F %T')"; jug-hpc status; echo "exit status: $?"; } \
            >>status.log 2>&1
        sleep 120
    done
}

# --- setup ---------------------------------------------------------------------
log setup
src=${1:-}
if [ -z "$src" ] && grep -qs '^name = "jug_hpc"' "$KIT/../../../pyproject.toml"; then
    src=$(cd -- "$KIT/../../.." && pwd)
fi
if [ -n "$src" ]; then
    ./install.sh "$src" "${@:2}" || exit 1
elif [ ! -x venv/bin/jug-hpc ]; then
    echo "error: no venv/ yet: give the path to jug_hpc: $0 /path/to/jug_hpc" >&2
    exit 1
else
    echo "warning: no jug_hpc checkout given or found: using venv/ as it is"
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
    if [ -n "$src" ]; then
        echo "jug_hpc checkout: $src"
        git -C "$src" log -1 --format='%H %cd %s'
        git -C "$src" status --short
    fi
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
    bad=0
    status_loop &
    sl=$!
    timeout "$WAIT" jug-hpc submit --interval=30 2>&1 | tee submit.log
    st=${PIPESTATUS[0]}
    kill "$sl"
    echo "exit status: $st" >>submit.log
    # shellcheck disable=SC2046
    W F $(job_ids)
    jug-hpc status >status.txt 2>&1
    python ../check.py . 2>&1 | tee check.txt
    # shellcheck disable=SC2046
    collect three-classes $(job_ids)
    want 'submit exits with status 0' [ "$st" = 0 ]
    want 'every task ran in its class (check.txt)' grep -q '^OK' check.txt
    want 'status during the run shows running workers (status.log)' \
        grep -q 'running' status.log
    exit "$bad"
)

# --- 4. failing setup -----------------------------------------------------------
step4() (
    cd 3-failing-setup || exit 1
    bad=0
    timeout 3600 jug-hpc submit --interval=20 2>&1 | tee submit.log
    st=${PIPESTATUS[0]}
    echo "exit status: $st" >>submit.log
    # shellcheck disable=SC2046
    [ "$st" = 124 ] && qdel $(job_ids)
    # shellcheck disable=SC2046
    W F $(job_ids)
    jug-hpc status >status.txt 2>&1
    cat status.txt
    # shellcheck disable=SC2046
    collect failing-setup $(job_ids)
    want 'submit stops with status 1, the workers failing' [ "$st" = 1 ]
    want 'status warns that the jobs of [default] keep failing (status.txt)' \
        grep -q 'jobs ended in error' status.txt
    exit "$bad"
)

# --- 5. the submitter as a job ------------------------------------------------------
step5() (
    cd 4-as-job || exit 1
    bad=0
    jug-hpc submit --as-job 2>&1 | tee submit.log
    [ "${PIPESTATUS[0]}" = 0 ] || exit 1
    status_loop &
    sl=$!
    # shellcheck disable=SC2046
    W F $(job_ids submitter)
    w=$?
    kill "$sl"
    [ "$w" = 0 ] || exit 1
    # shellcheck disable=SC2046
    W F $(job_ids)
    jug-hpc status >status.txt 2>&1
    python ../check.py . 2>&1 | tee check.txt
    # shellcheck disable=SC2046
    collect as-job $(job_ids)
    want 'every task ran in its class (check.txt)' grep -q '^OK' check.txt
    want 'the submitter job says every task is finished' \
        grep -q 'every task is finished' /dev/null logs/submitter-*.log
    want 'status during the run shows the submitter job (status.log)' \
        grep -q -i 'submitter' status.log
    exit "$bad"
)

# --- 6. jug-hpc cancel -------------------------------------------------------------
step6() (
    cd 6-cancel || exit 1
    bad=0
    jug-hpc submit --as-job --interval=30 2>&1 | tee submit.log
    [ "${PIPESTATUS[0]}" = 0 ] || exit 1
    # wait for the submitter to submit workers, and for them to start
    waited=0
    while [ -z "$(job_ids default)" ]; do
        [ "$waited" -lt "$WAIT" ] || exit 1
        sleep 15
        waited=$((waited + 15))
    done
    # shellcheck disable=SC2046
    W RBEFX $(job_ids default)
    jug-hpc status >status-before.txt 2>&1
    jug-hpc cancel 2>&1 | tee cancel.log
    st=${PIPESTATUS[0]}
    echo "exit status: $st" >>cancel.log
    # shellcheck disable=SC2046
    W F $(job_ids)
    # again: nothing is left to cancel
    { jug-hpc cancel; echo "exit status: $?"; } >>cancel.log 2>&1
    jug-hpc status >status.txt 2>&1
    # shellcheck disable=SC2046
    collect cancel $(job_ids)
    # qdel's messages for a finished job and an unknown one (which the backend ignores)
    # shellcheck disable=SC2046
    { qdel $(job_ids submitter) 99999999; echo "exit status: $?"; } \
        >../recorded/qdel-finished-unknown.txt 2>&1
    cat ../recorded/qdel-finished-unknown.txt
    cat /dev/null logs/submitter-*.log
    want 'cancel exits with status 0' [ "$st" = 0 ]
    want 'cancelling again exits with status 0' [ "$(tail -1 cancel.log)" = 'exit status: 0' ]
    want 'the submitter job logs that it was stopped by SIGTERM' \
        grep -q 'stopped by SIGTERM' /dev/null logs/submitter-*.log
    exit "$bad"
)

# --- 7. jobs killed by the scheduler, and stale locks ---------------------------------
step7() (
    cd 7-kills || exit 1
    bad=0
    cp too-small.toml jug_hpc.toml
    jug-hpc check >check.txt 2>&1
    # the submitter in the background (SIGTERM stops it; SIGINT is ignored there)
    jug-hpc submit --interval=30 --max-interval=60 >submit.log 2>&1 &
    pid=$!
    # wait for: hog killed for memory, its lock left; slow killed for wall time 3 times
    waited=0
    until grep -q '\[class.hog\]: 1 task is locked' submit.log &&
          grep -q '\[class.slow\]: 3 jobs in a row ended in error' submit.log; do
        if ! kill -0 "$pid" 2>/dev/null; then
            echo 'the submitter stopped by itself'
            break
        fi
        if [ "$waited" -ge "$WAIT" ]; then
            echo "timed out after $WAIT s"
            break
        fi
        sleep 15
        waited=$((waited + 15))
    done
    jug-hpc status >status-during.txt 2>&1      # the submitter still runs
    kill -TERM "$pid"
    wait "$pid"
    st=$?
    echo "exit status: $st" >>submit.log
    cat submit.log
    jug-hpc status >status-stopped.txt 2>&1     # no submitter
    cat status-stopped.txt
    # the fix the warnings suggest, then free hog's lock and run again
    cp fixed.toml jug_hpc.toml
    { jug-hpc status --release-stale; echo "exit status: $?"; } >release.txt 2>&1
    cat release.txt
    timeout "$WAIT" jug-hpc submit --interval=30 >submit2.log 2>&1
    st2=$?
    echo "exit status: $st2" >>submit2.log
    cat submit2.log
    # shellcheck disable=SC2046
    W F $(job_ids)
    jug-hpc status >status.txt 2>&1
    cat status.txt
    # shellcheck disable=SC2046
    collect kills $(job_ids)
    want 'the submitter warns that a job of [class.hog] was killed (memory)' \
        grep -q '\[class.hog\]: job .*memory' submit.log
    want 'the submitter warns that a job of [class.slow] was killed (wall time)' \
        grep -q '\[class.slow\]: job .*raise time' submit.log
    want "the submitter warns about [class.hog]'s stale lock" \
        grep -q '\[class.hog\]: 1 task is locked' submit.log
    want 'the submitter stops submitting workers for [class.slow]' \
        grep -q '\[class.slow\]: 3 jobs in a row ended in error' submit.log
    want 'the submitter exits with 143 on SIGTERM, saying so' \
        sh -c "[ $st = 143 ] && grep -q 'stopped by SIGTERM' submit.log"
    want 'status (submitter running) reports the kill of [class.hog]' \
        grep -q '\[class.hog\]: 1 job killed' status-during.txt
    want 'status (submitter running) reports the kills of [class.slow] (wall time)' \
        grep -q '\[class.slow\]: [0-9]* jobs\? killed for wall time' status-during.txt
    want 'status (no submitter) warns that [class.slow] keeps failing' \
        grep -q 'jobs ended in error' status-stopped.txt
    want 'status (no submitter) warns about the stale lock of [class.hog]' \
        grep -q '\[class.hog\]: 1 task is locked' status-stopped.txt
    want '--release-stale releases the lock of [class.hog]' \
        grep -q '\[class.hog\]: released the locks of 1 task' release.txt
    want 'with fixed.toml, a new submit finishes every task' \
        sh -c "[ $st2 = 0 ] && grep -q 'every task is finished' submit2.log"
    exit "$bad"
)

# --- 8. failed tasks: the submitter cancels the workers left ------------------------
step8() (
    cd 8-blocked || exit 1
    bad=0
    timeout "$WAIT" jug-hpc submit --interval=20 --max-interval=20 2>&1 | tee submit.log
    st=${PIPESTATUS[0]}
    echo "exit status: $st" >>submit.log
    # shellcheck disable=SC2046
    W F $(job_ids)
    jug-hpc status >status.txt 2>&1
    cat status.txt
    # shellcheck disable=SC2046
    collect blocked $(job_ids)
    want 'submit stops with status 1, a task having failed' [ "$st" = 1 ]
    want 'submit says no task can run because of the failed task' \
        grep -q 'no task can run: failed tasks' submit.log
    want 'submit cancels the workers still queued or running' \
        grep -q 'cancelled job' submit.log
    exit "$bad"
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
# everything worth sending back (not the jugdirs, nor venv/)
# shellcheck disable=SC2046
tar czf pbs-results.tgz --exclude='*/jugfile.jugdata' $(result_paths | grep -v '^pbs-results.tgz$')
echo "results: $KIT/pbs-results.tgz"
