"""CLI: python -m intentcypher "question" [--env .env] [--uri/--username/--password/--database]."""

from __future__ import annotations

import argparse
import json
import sys

from intentsql.jev_client import JevClient

from intentcypher.connection import apply_env_file, connection_config
from intentcypher.semantic_cypher import run_read


def _prepare(args) -> None:
    """Apply System One settings from the .env file before constructing JevClient."""
    apply_env_file(args.env)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="intentcypher",
        description="Ask one plain-English question about a Neo4j database; "
                    "get parameterized Cypher and its read-only result.")
    parser.add_argument("question")
    parser.add_argument("--env", default=".env", help=".env file with connection details")
    parser.add_argument("--uri", help="Neo4j URI (overrides env)")
    parser.add_argument("--username", help="Neo4j username (overrides env)")
    parser.add_argument("--password", help="Neo4j password (overrides env)")
    parser.add_argument("--database", help="Neo4j database (overrides env)")
    parser.add_argument("--plan", action="store_true", help="Also print the typed plan and trace")
    args = parser.parse_args(argv)
    _prepare(args)

    try:
        config = connection_config(
            env={},
            uri=args.uri, username=args.username, password=args.password,
            database=args.database)
    except ConnectionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    answer = run_read(args.question, config, JevClient())
    if answer.query:
        print(answer.query)
        if answer.parameters:
            print(f"-- Parameters: {json.dumps(answer.parameters, default=str)}")
    if answer.status == "answered":
        for row in answer.rows:
            print(json.dumps(row, default=str, ensure_ascii=False))
        if args.plan:
            print(json.dumps({"plan": answer.plan, "jev_calls": answer.jev_calls,
                              "input_tokens": answer.input_tokens,
                              "output_tokens": answer.output_tokens},
                             indent=2, default=str))
        return 0
    print(f"refused: {answer.reason}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
