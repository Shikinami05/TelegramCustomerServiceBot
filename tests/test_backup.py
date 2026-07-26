import os
import sqlite3
import stat
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from scripts import manage_backup


class RollbackBackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.temp_dir.name)
        self.database = self.base_dir / "bot.db"
        self.backup = self.base_dir / "backups" / "rollback-test.db"
        with closing(sqlite3.connect(self.database)) as conn:
            conn.execute("CREATE TABLE messages (value TEXT NOT NULL)")
            conn.execute("INSERT INTO messages (value) VALUES ('before')")
            conn.commit()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def read_values(self) -> list[str]:
        with closing(sqlite3.connect(self.database)) as conn:
            return [
                str(row[0])
                for row in conn.execute("SELECT value FROM messages ORDER BY rowid")
            ]

    def test_backup_restores_the_previous_database_state(self) -> None:
        manage_backup.create_backup(self.database, self.backup, keep=5)
        with closing(sqlite3.connect(self.database)) as conn:
            conn.execute("INSERT INTO messages (value) VALUES ('after')")
            conn.commit()

        self.assertEqual(self.read_values(), ["before", "after"])
        manage_backup.restore_backup(self.database, self.backup)
        self.assertEqual(self.read_values(), ["before"])

    def test_backup_pruning_keeps_the_newest_files(self) -> None:
        backup_dir = self.backup.parent
        backup_dir.mkdir()
        backups = []
        for index in range(4):
            path = backup_dir / f"rollback-{index}.db"
            path.write_bytes(b"backup")
            os.utime(path, (index + 1, index + 1))
            backups.append(path)

        manage_backup.prune_backups(backup_dir, keep=2)
        self.assertFalse(backups[0].exists())
        self.assertFalse(backups[1].exists())
        self.assertTrue(backups[2].exists())
        self.assertTrue(backups[3].exists())

    def test_manual_backups_are_pruned_separately(self) -> None:
        backup_dir = self.backup.parent
        first = backup_dir / "manual-1.db"
        second = backup_dir / "manual-2.db"
        third = backup_dir / "manual-3.db"
        rollback = backup_dir / "rollback-keep.db"

        manage_backup.create_backup(
            self.database,
            first,
            keep=10,
            kind="manual",
        )
        manage_backup.create_backup(
            self.database,
            second,
            keep=10,
            kind="manual",
        )
        os.utime(first, (1, 1))
        os.utime(second, (2, 2))
        rollback.write_bytes(b"separate rollback backup")

        manage_backup.create_backup(
            self.database,
            third,
            keep=2,
            kind="manual",
        )

        self.assertFalse(first.exists())
        self.assertTrue(second.exists())
        self.assertTrue(third.exists())
        self.assertTrue(rollback.exists())

    def test_backup_kind_must_match_the_filename(self) -> None:
        with self.assertRaises(ValueError):
            manage_backup.create_backup(
                self.database,
                self.backup.parent / "rollback-wrong.db",
                keep=5,
                kind="manual",
            )

    def test_corrupt_rollback_backup_does_not_replace_live_database(self) -> None:
        self.backup.parent.mkdir()
        self.backup.write_bytes(b"not a sqlite database")
        database_before = self.database.read_bytes()
        wal_path = Path(f"{self.database}-wal")
        shm_path = Path(f"{self.database}-shm")
        wal_path.write_bytes(b"existing wal")
        shm_path.write_bytes(b"existing shm")

        with self.assertRaises(sqlite3.DatabaseError):
            manage_backup.restore_backup(self.database, self.backup)
        self.assertEqual(self.database.read_bytes(), database_before)
        self.assertEqual(wal_path.read_bytes(), b"existing wal")
        self.assertEqual(shm_path.read_bytes(), b"existing shm")

    @unittest.skipUnless(os.name == "posix", "POSIX permission modes required")
    def test_backup_permissions_are_private(self) -> None:
        manage_backup.create_backup(self.database, self.backup, keep=5)
        self.assertEqual(stat.S_IMODE(self.backup.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.backup.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
