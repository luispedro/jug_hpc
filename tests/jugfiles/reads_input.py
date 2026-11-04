import sys

from helper import process  # helper.py lives next to this jugfile
from jug import Task

# The first task records argv[0], so tests can check what the jugfile saw
Task(process, sys.argv[0])

with open('input.txt') as f:
    for line in f:
        Task(process, line.strip())
