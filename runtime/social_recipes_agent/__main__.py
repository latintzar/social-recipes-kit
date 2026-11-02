"""Launch a source checkout using explicit, isolated project configuration."""

from __future__ import annotations

import argparse
import os
import sys

from .config import ConfigurationError, RuntimeConfig


def main() -> int:
    parser = argparse.ArgumentParser(description="Run your Instagram recipe agent")
    parser.add_argument("command", choices=("check", "serve"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    try:
        config = RuntimeConfig.from_environment()
        config.validate_sdk()
        config.prepare_state()
    except ConfigurationError as exc:
        parser.error(str(exc))
    if args.command == "check":
        print(
            "Project configuration and pinned Hermes source are ready. Provider connectivity is checked when you send your own test Reel."
        )
        return 0
    # A new interpreter receives only selected own configuration, never inherited
    # provider variables, Python startup hooks, project dotenv or Hermes accounts.
    os.execve(
        sys.executable,
        [
            sys.executable,
            "-m",
            "social_recipes_agent.worker",
            "--host",
            args.host,
            "--port",
            str(args.port),
        ],
        config.child_environment(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
