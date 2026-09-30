"""Remembers what was already alerted, so ten channels posting the same
deal gives you one notification, not ten."""
import sqlite3
import time


class Seen:
    def __init__(self, path, hours):
        self.ttl = hours * 3600
        self.db = sqlite3.connect(path)
        self.db.execute('CREATE TABLE IF NOT EXISTS seen (key TEXT PRIMARY KEY, ts REAL)')
        self.db.commit()

    def check_and_add(self, key):
        """True if this key is new (and records it); False if seen recently."""
        now = time.time()
        self.db.execute('DELETE FROM seen WHERE ts < ?', (now - self.ttl,))
        row = self.db.execute('SELECT 1 FROM seen WHERE key = ?', (key,)).fetchone()
        if row is None:
            self.db.execute('INSERT INTO seen VALUES (?, ?)', (key, now))
        self.db.commit()
        return row is None
