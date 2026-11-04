'''Command line interface: ``jug-hpc <command>``'''
import argparse
import sys

from .jug_hpc_version import __version__


def build_parser():
    parser = argparse.ArgumentParser(
        prog='jug-hpc',
        description=(
            'Run a Jug jugfile on an HPC cluster, submitting workers sized '
            'for each resource class of tasks.'),
    )
    parser.add_argument(
        '--version', action='version', version=f'%(prog)s {__version__}')
    parser.add_subparsers(dest='command', metavar='<command>')
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help(sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
