"""Operator command for closed creator negotiation retention."""

import argparse
import json
import os
from uuid import UUID

from .maintenance import retain_creator_negotiations
from .storage import Store


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Preview or erase eligible closed creator negotiations."
    )
    parser.add_argument("workspace", type=UUID)
    parser.add_argument(
        "--retention-days", type=int, required=True, help="Approved retention after closure."
    )
    parser.add_argument("--replay-days", type=int, required=True, help="Approved relay replay window.")
    parser.add_argument("--limit", type=int, default=100, help="Maximum negotiations in this batch.")
    parser.add_argument("--apply", action="store_true", help="Erase the eligible batch.")
    args = parser.parse_args(argv)
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        parser.error("DATABASE_URL is required.")
    try:
        report = retain_creator_negotiations(
            Store(database_url),
            workspace=args.workspace,
            retention_days=args.retention_days,
            replay_days=args.replay_days,
            limit=args.limit,
            apply=args.apply,
        )
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
