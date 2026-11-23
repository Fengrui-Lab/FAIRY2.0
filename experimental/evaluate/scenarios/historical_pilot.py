"""Compatibility entry point for the relocated offline command."""

from evaluate.experiments.historical.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
