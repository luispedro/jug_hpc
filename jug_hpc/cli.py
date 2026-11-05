'''Command line interface: ``jug-hpc <command>``'''
import argparse
import sys
from pathlib import Path

from .jug_hpc_version import __version__

PROG = 'jug-hpc'


def build_parser():
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=(
            'Run a Jug jugfile on an HPC cluster, submitting workers sized '
            'for each resource class of tasks.'),
    )
    parser.add_argument(
        '--version', action='version', version=f'%(prog)s {__version__}')
    parser.add_argument(
        '-C', '--project-dir', metavar='DIR', type=Path,
        help=('start looking for jug_hpc.toml in DIR instead of the current '
              'directory (the project is the nearest directory, DIR or a '
              'parent, holding jug_hpc.toml)'))
    commands = parser.add_subparsers(dest='command', metavar='<command>')
    commands.add_parser(
        'config',
        help='show the settings and where each value came from',
        description=(
            'Show the merged cluster settings (with the file each value came '
            'from) and the project settings (with every resource class fully '
            'resolved, marking values inherited from [default] or built in).'))
    commands.add_parser(
        'check',
        help='show which resource class runs each task of the jugfile',
        description=(
            'Load the jugfile and list, for every resource class, the tasks it '
            'runs (counted per task name). Tasks matched by two classes are an '
            'error (exit status 1); classes matching no task are a warning.'))
    submit = commands.add_parser(
        'submit',
        help='submit workers as their tasks become ready',
        description=(
            'Submit workers for every resource class as its tasks become '
            'ready, and keep polling (counting the tasks and asking the '
            'scheduler about the workers) until every task is finished, or '
            'no task can run because some failed. Each class gets at most '
            'min(ready tasks, workers) queued or running workers. Only one '
            'submitter may run per project. Worker scripts are kept in '
            '.jug_hpc/scripts/. With --all, every class is submitted at '
            'once instead, with at most one worker per task, and jug-hpc '
            'exits: this suits task graphs where the classes do not wait for '
            'each other. With --dry-run, the scripts and the commands that '
            'would submit them are printed instead. With --as-job, the '
            'submitter itself is submitted as a job (with the resources of '
            '[submitter] in jug_hpc.toml, which must set its wall time), '
            'logging to logs/submitter-<job id>.log.'))
    commands.add_parser(
        'status',
        help='show the tasks and workers of each class, and jobs killed by the scheduler',
        description=(
            'Show, for every resource class, its tasks in each state (counted as '
            'the submitter counts them, loading the jugfile) and its queued and '
            'running workers (asking the scheduler about the jobs recorded in '
            '.jug_hpc/jobs.json), and whether a submitter is running. Jobs that '
            'the scheduler killed (for memory, wall time or by a signal) are '
            'warnings, with what to change in jug_hpc.toml.'))
    commands.add_parser(
        'cancel',
        help='cancel every queued or running job of the project',
        description=(
            'Cancel every job recorded in .jug_hpc/jobs.json that is queued or '
            'running: the submitter job (submit --as-job) first, waiting until it '
            'has stopped, then the workers. Nothing is cancelled while a submitter '
            'runs outside the scheduler (jug-hpc submit in a terminal), as it would '
            'replace the workers: stop it first. The jugfile is not loaded, and '
            'jug_hpc.toml is not read.'))
    mode = submit.add_mutually_exclusive_group()
    mode.add_argument('--all', action='store_true',
                      help='submit every class now, and exit')
    mode.add_argument('--dry-run', action='store_true',
                      help='write and print the scripts and submit commands, '
                           'without submitting')
    mode.add_argument('--as-job', action='store_true',
                      help='submit the submitter as a job, and exit')
    submit.add_argument(
        '--interval', type=_positive_seconds, metavar='SECONDS', default=None,
        help='seconds between polls (default: 60)')
    submit.add_argument(
        '--max-interval', type=_positive_seconds, metavar='SECONDS', default=None,
        help='longest wait between polls: while nothing changes, the wait doubles '
             'after each poll, up to this (default: 5 times --interval)')
    submit.add_argument(
        'classes', nargs='*', metavar='CLASS',
        help="with --all or --dry-run: only these classes ('default' for the "
             'default class); a class named here is submitted even if it has '
             'no tasks')
    return parser


def _interval(args):
    '''Seconds between polls: ``--interval``, or the default'''
    from .poll import POLL_INTERVAL
    return args.interval if args.interval is not None else POLL_INTERVAL


def _positive_seconds(text):
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f'not a number: {text!r}') from None
    if not value > 0:
        raise argparse.ArgumentTypeError(f'must be positive: {text!r}')
    return value


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == 'submit':
        if args.all or args.dry_run:
            if args.interval is not None:
                parser.error('--interval: only without --all and --dry-run')
            if args.max_interval is not None:
                parser.error('--max-interval: only without --all and --dry-run')
        elif args.max_interval is not None and args.max_interval < _interval(args):
            parser.error(f'--max-interval ({args.max_interval:g} s) is less than '
                         f'--interval ({_interval(args):g} s)')
        elif args.classes:
            parser.error('CLASS: only with --all or --dry-run (the submitter '
                         'polls every class)')
    if args.command is None:
        parser.print_help(sys.stderr)
        return 2
    start = args.project_dir if args.project_dir is not None else Path.cwd()
    if not start.is_dir():
        parser.error(f'-C {start}: not a directory')
    return COMMANDS[args.command](args, start)


def _error(message):
    _message('error', message)


def _message(kind, message):
    print(f'{PROG}: {kind}: {message}', file=sys.stderr)


def cmd_config(args, start):
    '''``jug-hpc config``: every section that can be read is printed, and every
    error is reported, so that one run shows all that needs fixing
    '''
    from .config import (
        PROJECT_FILE,
        ConfigError,
        find_project_dir,
        load_cluster_settings,
        load_project_settings,
    )
    from .config.show import format_cluster_settings, format_project_settings
    from .schedulers.checks import check_scheduler_settings

    status = 0
    project_dir = find_project_dir(start)
    sections = []
    cluster = None
    try:
        cluster = load_cluster_settings(project_dir)
    except ConfigError as e:
        _error(e)
        status = 1
    else:
        sections.append(format_cluster_settings(cluster))

    project = None
    if project_dir is None:
        sections.append(
            f'# Project settings: no {PROJECT_FILE} found in {start.absolute()} '
            'or any parent directory')
    else:
        try:
            project = load_project_settings(project_dir)
        except ConfigError as e:
            _error(e)
            status = 1
        else:
            sections.append(format_project_settings(project, cluster))
    if sections:
        # flushed so that the listing comes before the messages when both
        # streams go to the same file
        print('\n\n'.join(sections), flush=True)

    found = check_scheduler_settings(cluster, project)
    for warning in found.warnings:
        _message('warning', warning)
    for error in found.errors:
        _error(error)
    if not found.ok:
        status = 1
    return status


def cmd_check(args, start):
    '''``jug-hpc check``: the listing goes to stdout, problems to stderr'''
    from .check import check
    from .config import (
        PROJECT_FILE,
        ConfigError,
        find_project_dir,
        load_project_settings,
    )
    from .jugstate import JugfileError, load_project

    project_dir = find_project_dir(start)
    if project_dir is None:
        _error(f'no {PROJECT_FILE} found in {start.absolute()} or any parent '
               'directory')
        return 1
    try:
        settings = load_project_settings(project_dir)
        state = load_project(settings)
    except ConfigError as e:
        _error(e)
        return 1
    except JugfileError as e:
        if e.details:
            print(e.details.rstrip('\n'), file=sys.stderr)
        _error(e)
        return 1
    report = check(settings, state)
    # flushed so that the listing comes before the messages when both
    # streams go to the same file
    print(report.text, flush=True)
    for note in report.notes:
        _message('note', note)
    for warning in report.warnings:
        _message('warning', warning)
    for error in report.errors:
        _error(error)
    return 0 if report.ok else 1


def cmd_status(args, start):
    '''``jug-hpc status``: the table goes to stdout, kills and notes to stderr'''
    from datetime import UTC, datetime

    from .classes import Classifier, counts_by_class
    from .config import (
        PROJECT_FILE,
        ConfigError,
        find_project_dir,
        load_cluster_settings,
        load_project_settings,
    )
    from .jugstate import JugfileError, project_task_counts
    from .schedulers.base import JobState, SchedulerError, create_scheduler
    from .state import ProjectState, StateError
    from .status import status

    project_dir = find_project_dir(start)
    if project_dir is None:
        _error(f'no {PROJECT_FILE} found in {start.absolute()} or any parent '
               'directory')
        return 1
    try:
        cluster = load_cluster_settings(project_dir)
        project = load_project_settings(project_dir)
        scheduler = create_scheduler(cluster)
        project_state = ProjectState(project_dir)
        records = project_state.jobs()
        ours = [r.job_id for r in records if r.scheduler == scheduler.name]
        statuses = scheduler.query(ours) if ours else {}
        finished = [j for j in ours if statuses[j].state is JobState.FINISHED]
        outcomes = scheduler.outcome(finished) if finished else {}
        lock_holder = project_state.submitter_lock_holder()
        counted = project_task_counts(project)
        by_class = counts_by_class(Classifier.from_project(project), counted.counts)
    except (ConfigError, SchedulerError, StateError) as e:
        _error(e)
        return 1
    except JugfileError as e:
        if e.details:
            print(e.details.rstrip('\n'), file=sys.stderr)
        _error(e)
        return 1
    report = status(project, by_class, counted.hit_barrier, records, statuses, outcomes,
                    datetime.now(UTC), scheduler_name=scheduler.name,
                    lock_holder=lock_holder)
    # flushed so that the table comes before the messages when both streams go
    # to the same file
    print(report.text, flush=True)
    for warning in report.warnings:
        _message('warning', warning)
    for note in report.notes:
        _message('note', note)
    return 0


def cmd_cancel(args, start):
    '''``jug-hpc cancel``: one line per cancelled job on stdout'''
    from .cancel import CancelError, cancel, describe_status
    from .config import PROJECT_FILE, ConfigError, find_project_dir, load_cluster_settings
    from .schedulers.base import SchedulerError, create_scheduler
    from .state import ProjectState, StateError

    project_dir = find_project_dir(start)
    if project_dir is None:
        _error(f'no {PROJECT_FILE} found in {start.absolute()} or any parent '
               'directory')
        return 1
    try:
        cluster = load_cluster_settings(project_dir)
        scheduler = create_scheduler(cluster)
        result = cancel(scheduler, ProjectState(project_dir))
    except (CancelError, ConfigError, SchedulerError, StateError) as e:
        _error(e)
        return 1
    for record, status in result.cancelled:
        print(f'{record.class_name}: cancelled job {record.job_id} '
              f'({describe_status(record, status)})', flush=True)
    for other, n in result.others.items():
        _message('note', f'{n} job{"s" if n != 1 else ""} submitted with the {other!r} '
                         f'backend not cancelled (the backend in use is {scheduler.name!r})')
    if not result.cancelled:
        _message('note', 'no job of this project is queued or running')
    if result.submitter_left:
        ids = ', '.join(r.job_id for r in result.submitter_left)
        _error(f'the submitter job ({ids}) had not stopped after it was cancelled, so it '
               "may have submitted workers since; run 'jug-hpc cancel' again once it "
               'has stopped')
        return 1
    return 0


def cmd_submit(args, start):
    '''``jug-hpc submit``: the polling submitter, or ``--all|--dry-run|--as-job``'''
    if args.as_job:
        return _submit_as_job(args, start)
    if not (args.all or args.dry_run):
        return _poll(args, start)
    return _submit_static(args, start)


def _submit_static(args, start):
    '''``jug-hpc submit --all|--dry-run``: scripts and job IDs go to stdout'''
    from .config import (
        PROJECT_FILE,
        ConfigError,
        find_project_dir,
        load_cluster_settings,
        load_project_settings,
    )
    from .jugstate import JugfileError, load_project
    from .schedulers.base import SchedulerError, create_scheduler
    from .state import ProjectState, StateError
    from .submit import format_dry_run, prepare, submit_worker, write_scripts

    project_dir = find_project_dir(start)
    if project_dir is None:
        _error(f'no {PROJECT_FILE} found in {start.absolute()} or any parent '
               'directory')
        return 1
    try:
        cluster = load_cluster_settings(project_dir)
        project = load_project_settings(project_dir)
        scheduler = create_scheduler(cluster)
        state = load_project(project)
        prep = prepare(cluster, project, state, scheduler,
                       classes=args.classes or None)
        for warning in prep.warnings:
            _message('warning', warning)
        for note in prep.notes:
            _message('note', note)
        project_state = ProjectState(project_dir)
        written = write_scripts(prep, scheduler, project_state)
    except (ConfigError, SchedulerError, StateError) as e:
        _error(e)
        return 1
    except JugfileError as e:
        if e.details:
            print(e.details.rstrip('\n'), file=sys.stderr)
        _error(e)
        return 1
    if args.dry_run:
        print(format_dry_run(written, project_dir), flush=True)
        return 0
    if not written:
        _message('note', 'nothing to submit')
        return 0
    for i, w in enumerate(written):
        try:
            record = submit_worker(w, scheduler, project_state)
        except (SchedulerError, StateError) as e:
            _error(e)
            rest = [x.prepared.class_name for x in written[i + 1:]]
            if rest:
                _message('note', f'not submitted: {", ".join(rest)}')
            return 1
        workers = record.workers
        print(f'{record.class_name}: submitted job {record.job_id} '
              f'({workers} worker{"s" if workers != 1 else ""}, {record.script})',
              flush=True)
    return 0


def _poll(args, start):
    '''``jug-hpc submit``: one line per event on stdout, with the time'''
    from .config import (
        PROJECT_FILE,
        ConfigError,
        find_project_dir,
        load_cluster_settings,
        load_project_settings,
    )
    from .jugstate import JugfileError
    from .kills import scheduler_kills
    from .poll import Submitter, run
    from .schedulers.base import SchedulerError, create_scheduler
    from .state import ProjectState, StateError

    project_dir = find_project_dir(start)
    if project_dir is None:
        _error(f'no {PROJECT_FILE} found in {start.absolute()} or any parent '
               'directory')
        return 1
    interval = _interval(args)
    last = [None]
    stopped = set()
    killed = []

    def on_poll(result):
        kills = scheduler_kills(result.ended)
        for record, outcome in kills:
            _message('warning', _kill_message(record, outcome, project))
        killed.extend(kills)
        for kind, message in _stopped_messages(stopped, result):
            _message(kind, message)
        stopped.clear()
        stopped.update(result.stopped)
        for record in result.submitted:
            workers = record.workers
            _log(f'{record.class_name}: submitted job {record.job_id} '
                 f'({workers} worker{"s" if workers != 1 else ""}, {record.script})')
        for e in result.errors:
            _error(e)
        summary = _poll_summary(result)
        if summary != last[0]:
            _log(summary)
            last[0] = summary

    def on_failure(e):
        _message('warning', f'{e} (trying again in {interval:g} s)')

    try:
        cluster = load_cluster_settings(project_dir)
        project = load_project_settings(project_dir)
        scheduler = create_scheduler(cluster)
        project_state = ProjectState(project_dir)
        submitter = Submitter(cluster, project, scheduler, project_state)
        for warning in submitter.warnings:
            _message('warning', warning)
        with project_state.submitter_lock():
            result = run(submitter, interval=interval, max_interval=args.max_interval,
                         on_poll=on_poll, on_failure=on_failure)
    except (ConfigError, SchedulerError, StateError) as e:
        _error(e)
        return 1
    except JugfileError as e:
        if e.details:
            print(e.details.rstrip('\n'), file=sys.stderr)
        _error(e)
        return 1
    except KeyboardInterrupt:
        _message('note', 'interrupted: the workers already submitted keep running')
        return 130
    if killed:
        _message('warning', f'jobs killed by the scheduler: {_kills_by_class(killed)}')
    return _report_end(result.report, _relative(project.logs, project_dir))


def _kill_message(record, outcome, project):
    '''A job killed by the scheduler, and what to change in its class'''
    from .kills import suggestion
    message = (f'{_table_label(record.class_name)}: job {record.job_id} ended: '
               f'{outcome.detail or outcome.outcome.value}')
    try:
        rc = project.get_class(record.class_name)
    except KeyError:    # the class is no longer in jug_hpc.toml
        return message
    advice = suggestion(outcome, rc)
    return f'{message}; {advice}' if advice else message


def _kills_by_class(kills):
    '''``[class.big] 2 for memory; [default] 1 for wall time``'''
    from .kills import kill_counts, kill_summary
    return '; '.join(f'{_table_label(name)} {kill_summary(by_outcome)}'
                     for name, by_outcome in kill_counts(kills).items())


def _report_end(report, logs='logs'):
    '''Report a run that is over (finished, blocked or stalled); returns the exit
    status

    ``logs`` is the worker logs directory, as shown to the user.
    '''
    from .submitter import Progress
    from .worker import SCHEDULER_LOGS
    if report.progress is Progress.FINISHED:
        _log('every task is finished')
        return 0
    if report.progress is Progress.STALLED:
        stalled = '; '.join(f'{_table_label(n)}: {_failed_jobs(jobs)}'
                            for n, jobs in report.stalled.items())
        message = f'no task can run: the workers keep failing: {stalled}'
        if report.failed:
            failed = ', '.join(f'{_table_label(n)} {c}' for n, c in report.failed.items())
            message += f'; failed tasks: {failed}'
        _error(message)
        _message('note', f"the worker logs (in {logs}/) and the scheduler's (in "
                         f'{logs}/{SCHEDULER_LOGS}/) say why the jobs failed; once fixed, '
                         "run 'jug-hpc submit' again")
        return 1
    failed = ', '.join(f'{_table_label(n)} {c}' for n, c in report.failed.items())
    message = f'no task can run: failed tasks: {failed}'
    if report.waiting:
        waiting = ', '.join(f'{_table_label(n)} {c}' for n, c in report.waiting.items())
        message += f'; tasks waiting for them: {waiting}'
    _error(message)
    _message('note', 'the worker logs say why the tasks failed; once fixed, '
                     "'jug cleanup --failed-only' lets them run again")
    return 1


def _stopped_messages(before, result):
    '''Messages about the classes stopped or started again since the last poll

    Parameters
    ----------
    before : collection of str
        The classes stopped at the last poll
    result : PollResult

    Returns
    -------
    messages : list of (kind, message)
    '''
    messages = [('note', (f'{_table_label(name)}: a task of the class ended or a job '
                         'succeeded: submitting its workers again'))
                for name in before if name not in result.stopped]
    for name, jobs in result.stopped.items():
        # a run that ends now says so in its error
        if name not in before and name not in result.report.stalled:
            messages.append(('warning', (f'{_table_label(name)}: {_failed_jobs(jobs)}; no '
                                        'more workers are submitted for it while this lasts')))
    return messages


def _failed_jobs(jobs):
    '''What :func:`jug_hpc.submitter.stopped_classes` found, in a few words'''
    ids = ', '.join(r.job_id for r, _ in jobs)
    last = jobs[-1][1]
    return (f'{len(jobs)} jobs in a row ended in error before any task of the class '
            f'ended (jobs {ids}; the last: {last.detail or last.outcome.value})')


def _relative(path, start):
    '''``path`` relative to ``start`` if it is inside it, as a string'''
    try:
        return str(Path(path).relative_to(start))
    except ValueError:
        return str(path)


def _submit_as_job(args, start):
    '''``jug-hpc submit --as-job``: submit the polling submitter as a job

    Everything the submitter would check at its first poll is checked here
    first (settings, jugfile), so that mistakes are reported now rather than
    in the job's log. Nothing is submitted if a submitter is running (the
    lock is held) or a submitter job is queued or running, or if no task is
    left to run.
    '''
    from datetime import UTC, datetime

    from .classes import counts_by_class
    from .config import (
        PROJECT_FILE,
        ConfigError,
        find_project_dir,
        load_cluster_settings,
        load_project_settings,
    )
    from .jugstate import JugfileError, project_task_counts
    from .poll import Submitter
    from .schedulers.base import SchedulerError, create_scheduler
    from .state import ProjectState, StateError
    from .submit import prepare_submitter, submit_worker, write_script
    from .submitter import Progress, active_submitter_jobs, progress, submitter_jobs

    project_dir = find_project_dir(start)
    if project_dir is None:
        _error(f'no {PROJECT_FILE} found in {start.absolute()} or any parent '
               'directory')
        return 1
    interval = _interval(args)
    try:
        cluster = load_cluster_settings(project_dir)
        project = load_project_settings(project_dir)
        scheduler = create_scheduler(cluster)
        project_state = ProjectState(project_dir)
        submitter = Submitter(cluster, project, scheduler, project_state)
        for warning in submitter.warnings:
            _message('warning', warning)
        prepared = prepare_submitter(cluster, project, scheduler, interval=interval,
                                     max_interval=args.max_interval)

        jobs = submitter_jobs(project_state.jobs(), scheduler.name)
        statuses = scheduler.query([r.job_id for r in jobs]) if jobs else {}
        active = active_submitter_jobs(jobs, statuses, datetime.now(UTC))
        if active:
            ids = ', '.join(r.job_id for r in active)
            _error(f'a submitter job is already queued or running for this project '
                   f'(job {ids}); nothing submitted')
            return 1
        with project_state.submitter_lock():
            pass

        counted = project_task_counts(project)
        report = progress(counts_by_class(submitter.classifier, counted.counts),
                          counted.hit_barrier)
        if report.progress is not Progress.RUNNING:
            _message('note', 'no submitter job submitted')
            return _report_end(report)

        written = write_script(prepared, scheduler, project_state)
        record = submit_worker(written, scheduler, project_state)
    except (ConfigError, SchedulerError, StateError) as e:
        _error(e)
        return 1
    except JugfileError as e:
        if e.details:
            print(e.details.rstrip('\n'), file=sys.stderr)
        _error(e)
        return 1
    logs = _relative(prepared.spec.logs, project_dir)
    print(f'{record.class_name}: submitted job {record.job_id} ({record.script}); '
          f'it logs to {logs}/{record.class_name}-<job id>.log', flush=True)
    return 0


def _table_label(name):
    return '[default]' if name == 'default' else f'[class.{name}]'


def _poll_summary(result):
    '''One line: the tasks of each state, and the active workers'''
    from .jugstate import STATES, TaskCounts
    total = sum(result.by_class.values(), TaskCounts())
    parts = ', '.join(f'{getattr(total, s)} {s}' for s in STATES)
    workers = sum(result.active.values()) + sum(r.workers for r in result.submitted)
    barrier = ' (more after a barrier)' if result.hit_barrier else ''
    return f'tasks: {parts}{barrier}; workers queued or running: {workers}'


def _log(message):
    from datetime import datetime
    print(f'[{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}] {message}', flush=True)


COMMANDS = {
    'config': cmd_config,
    'check': cmd_check,
    'submit': cmd_submit,
    'status': cmd_status,
    'cancel': cmd_cancel,
}


if __name__ == '__main__':
    sys.exit(main())
