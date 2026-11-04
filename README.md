# jug_hpc

Run [Jug](https://jug.readthedocs.io) on HPC clusters, with workers sized for
each *type* of task.

**Status: early development, not usable yet.** See [GOALS.md](GOALS.md) for
the design and [PLAN.md](PLAN.md) for the implementation plan.

## Development

```bash
uv sync           # create .venv with jug_hpc and the dev tools
uv run pytest     # run the tests
uv run ruff check .
```

jug_hpc requires Python 3.11 or later and Jug 2.7 or later.

## License

MIT (see [COPYING.MIT](COPYING.MIT)).
