"""Command line entry point."""
import argparse

from ledgerlite.config import load_config
from ledgerlite.storage.sqlite_store import SqliteStore


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="ledgerlite")
    parser.add_argument("command", choices=["serve", "migrate"])
    parser.add_argument("--config", default="configs/settings.yaml")
    args = parser.parse_args(argv)
    settings = load_config(args.config)
    store = SqliteStore(settings.database_path)
    if args.command == "migrate":
        store.migrate_v2()
        return 0
    print("serving on :8080")
    return 0
