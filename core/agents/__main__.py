import argparse
import json
import os
import sys

from . import CLIS, AgentError, review, version, work, workspace


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="agent", description="Run a real agent CLI as the loop's reviewer or worker (stdin is the prompt).")
    parser.add_argument("role", choices=("review", "worker", "versions"))
    parser.add_argument("--cli", choices=CLIS, default="codex")
    args = parser.parse_args(argv)
    model = os.environ.get("AGENT_MODEL")
    try:
        if args.role == "versions":
            print(json.dumps({cli: version(cli) for cli in CLIS}))
            return 0
        prompt = sys.stdin.read()
        if args.role == "review":
            print(json.dumps(review(args.cli, prompt, workspace(), model)))
        else:
            work(args.cli, prompt, workspace(), model)
        return 0
    except (AgentError, ValueError, KeyError) as error:
        print(f"agent {args.role}: {error}", file=sys.stderr)
        return error.code if isinstance(error, AgentError) else 3


if __name__ == "__main__":
    raise SystemExit(main())
