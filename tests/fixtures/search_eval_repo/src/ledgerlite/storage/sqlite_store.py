"""SQLite persistence."""
import sqlite3

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS invoices (number TEXT PRIMARY KEY, customer_id TEXT, total TEXT);
CREATE TABLE IF NOT EXISTS journal (id INTEGER PRIMARY KEY, account TEXT, amount TEXT, posted_at REAL);
"""


class SqliteStore:
    def __init__(self, path: str):
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA_V1)

    def schema_version(self) -> int:
        row = self.conn.execute("PRAGMA user_version").fetchone()
        return int(row[0]) if row else 0

    def migrate_v2(self) -> None:
        """Add the tax_rate column to invoices. Idempotent."""
        cols = [r[1] for r in self.conn.execute("PRAGMA table_info(invoices)")]
        if "tax_rate" not in cols:
            self.conn.execute("ALTER TABLE invoices ADD COLUMN tax_rate TEXT")
        self.conn.execute("PRAGMA user_version = 2")
        self.conn.commit()

    def put_invoice(self, number: str, customer_id: str, total: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO invoices VALUES (?, ?, ?)", (number, customer_id, total))
        self.conn.commit()

    def get_invoice(self, number: str):
        return self.conn.execute("SELECT * FROM invoices WHERE number = ?", (number,)).fetchone()
