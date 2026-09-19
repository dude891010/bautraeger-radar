"""Erlaubt `python -m radar <befehl>`, siehe radar/cli.py."""

import sys

from radar.cli import main

if __name__ == "__main__":
    sys.exit(main())
