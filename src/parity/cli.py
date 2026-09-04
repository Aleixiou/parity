"""Command line entry point.

Milestone 0 wires up the console script and the argument surface so that
``parity --help`` is informative. The ``diff`` subcommand is deliberately not
executable yet - it is built in Milestone 3, together with the human and JSON
renderers and the CI exit codes. Shipping a half-wired ``diff`` that printed a
partial answer would violate the project's first rule: never let a parity check
look more complete than it is.
"""

from __future__ import annotations

import argparse
import sys

__version__ = "0.1.0"

# Exit codes are part of the contract - they are what make this a CI check.
EXIT_IDENTICAL = 0
EXIT_DIFFERENCES = 1
EXIT_ERROR = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="parity",
        description=(
            "Prove two tables in two different database engines hold the same "
            "data - without moving the data out of either engine."
        ),
    )
    parser.add_argument("--version", action="version", version=f"parity {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    d = sub.add_parser("diff", help="compare two tables across two engines")
    d.add_argument("--a", required=True, metavar="CONN", help="side A connection string")
    d.add_argument("--a-table", required=True, metavar="TABLE", help="side A table")
    d.add_argument("--b", required=True, metavar="CONN", help="side B connection string")
    d.add_argument("--b-table", required=True, metavar="TABLE", help="side B table")
    d.add_argument("--key", required=True, metavar="COL", help="integer key column")
    d.add_argument("--columns", metavar="a,b,c", help="compare only these columns")
    d.add_argument("--exclude", metavar="x,y", help="skip these columns")
    d.add_argument("--bisection-factor", type=int, default=32, metavar="N")
    d.add_argument("--threshold", type=int, default=10_000, metavar="N")
    d.add_argument("--float-scale", type=int, default=6, metavar="N",
                   help="decimal places at which floats and decimals are compared")
    d.add_argument("--max-diffs", type=int, metavar="N")
    d.add_argument("--json", action="store_true", help="machine-readable output")
    d.add_argument("--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return EXIT_ERROR
    print(
        "parity: `diff` is not wired up yet - it lands in Milestone 3.\n"
        "The dialect layer and bisection engine are usable from Python today; "
        "see demo/proof.py.",
        file=sys.stderr,
    )
    return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
