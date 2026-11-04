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
        help='write the worker scripts and submit them',
        description=(
            'Write a worker script for every resource class with tasks (in '
            '.jug_hpc/scripts/) and submit it. With --all, every class is '
            'submitted at once, with at most one worker per task: this suits '
            'task graphs where the classes do not wait for each other. With '
            '--dry-run, the scripts and the commands that would submit them '
            'are printed instead.'))
    mode = submit.add_mutually_exclusive_group(required=True)
    mode.add_argument('--all', action='store_true',
                      help='submit every class now')
    mode.add_argument('--dry-run', action='store_true',
                      help='write and print the scripts and submit commands, '
                           'without submitting')
    submit.add_argument(
        'classes', nargs='*', metavar='CLASS',
        help="only these classes ('default' for the default class); a class "
             'named here is submitted even if it has no tasks')
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
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


def cmd_submit(args, start):
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


COMMANDS = {
    'config': cmd_config,
    'check': cmd_check,
    'submit': cmd_submit,
}


if __name__ == '__main__':
    sys.exit(main())
