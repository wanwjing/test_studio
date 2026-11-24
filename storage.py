import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        Path(path).touch(mode=0o600, exist_ok=True)
        self.path = path
        with self.connection() as db:
            db.execute("CREATE TABLE IF NOT EXISTS records (kind TEXT, id TEXT, data TEXT, PRIMARY KEY(kind,id))")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path)
        try:
            with db:
                yield db
        finally:
            db.close()

    def write(self, kind, key, value):
        with self.connection() as db:
            db.execute("INSERT OR REPLACE INTO records VALUES (?,?,?)", (kind, key, json.dumps(value)))

    def delete(self, kind, key):
        with self.connection() as db:
            return db.execute("DELETE FROM records WHERE kind=? AND id=?", (kind, key)).rowcount > 0

    def read(self, kind, key=None):
        with self.connection() as db:
            if key is None:
                return [json.loads(row[0]) for row in db.execute(
                    "SELECT data FROM records WHERE kind=? ORDER BY rowid DESC", (kind,))]
            row = db.execute("SELECT data FROM records WHERE kind=? AND id=?", (kind, key)).fetchone()
            return json.loads(row[0]) if row else None
