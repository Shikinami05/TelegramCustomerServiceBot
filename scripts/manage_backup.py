import argparse
import os
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE = BASE_DIR / "bot.db"


def enforce_private_mode(path: Path, mode: int) -> None:
    if os.name == "posix":
        path.chmod(mode)


def temporary_path(destination: Path) -> Path:
    return destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")


def copy_database(
    source: Path,
    destination: Path,
    clear_destination_sidecars: bool = False,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    enforce_private_mode(destination.parent, 0o700)
    temp_path = temporary_path(destination)
    try:
        with closing(sqlite3.connect(source, timeout=30)) as source_conn:
            with closing(sqlite3.connect(temp_path)) as destination_conn:
                source_conn.backup(destination_conn)
                integrity = destination_conn.execute(
                    "PRAGMA integrity_check"
                ).fetchone()
                if not integrity or integrity[0] != "ok":
                    raise RuntimeError("SQLite backup integrity check failed")
        enforce_private_mode(temp_path, 0o600)
        if clear_destination_sidecars:
            for suffix in ("-wal", "-shm"):
                Path(f"{destination}{suffix}").unlink(missing_ok=True)
        temp_path.replace(destination)
        enforce_private_mode(destination, 0o600)
    finally:
        temp_path.unlink(missing_ok=True)


def prune_backups(directory: Path, keep: int) -> None:
    backups = sorted(
        directory.glob("rollback-*.db"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for old_backup in backups[keep:]:
        old_backup.unlink(missing_ok=True)


def create_backup(database: Path, output: Path, keep: int) -> None:
    if not database.is_file():
        raise FileNotFoundError(f"Database does not exist: {database}")
    if database.resolve() == output.resolve():
        raise ValueError("Backup output must differ from the live database")
    copy_database(database, output)
    prune_backups(output.parent, keep)


def restore_backup(database: Path, source: Path) -> None:
    if not source.is_file():
        raise FileNotFoundError(f"Rollback backup does not exist: {source}")
    if database.resolve() == source.resolve():
        raise ValueError("Rollback source must differ from the live database")
    copy_database(source, database, clear_destination_sidecars=True)
    for suffix in ("-wal", "-shm"):
        Path(f"{database}{suffix}").unlink(missing_ok=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create or restore SQLite rollback backups")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_parser = subparsers.add_parser("create")
    create_parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    create_parser.add_argument("--output", type=Path, required=True)
    create_parser.add_argument("--keep", type=int, default=5)

    restore_parser = subparsers.add_parser("restore")
    restore_parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    restore_parser.add_argument("--input", type=Path, required=True)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "create":
        if args.keep < 1:
            raise SystemExit("--keep must be at least 1")
        create_backup(args.database.resolve(), args.output.resolve(), args.keep)
        print(args.output.resolve())
        return
    restore_backup(args.database.resolve(), args.input.resolve())
    print(args.database.resolve())


if __name__ == "__main__":
    main()
