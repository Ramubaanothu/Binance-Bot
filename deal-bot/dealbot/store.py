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


IST = 5.5 * 3600


class Orders:
    """Order attempts, for the daily cap and never buying one ASIN twice.

    'unknown' (clicked Place order but no confirmation seen) counts as
    placed: better to under-order than to double-order.
    """
    COUNTED = ('placed', 'unknown')

    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute('CREATE TABLE IF NOT EXISTS orders '
                        '(asin TEXT, ts REAL, status TEXT, price REAL, note TEXT)')
        self.db.commit()

    def today_count(self, now=None):
        now = time.time() if now is None else now
        ist_midnight = now - (now + IST) % 86400
        q = 'SELECT COUNT(*) FROM orders WHERE ts >= ? AND status IN (?, ?)'
        return self.db.execute(q, (ist_midnight,) + self.COUNTED).fetchone()[0]

    def already_ordered(self, asin):
        q = 'SELECT 1 FROM orders WHERE asin = ? AND status IN (?, ?)'
        return self.db.execute(q, (asin,) + self.COUNTED).fetchone() is not None

    def record(self, asin, status, price=None, note=''):
        self.db.execute('INSERT INTO orders VALUES (?, ?, ?, ?, ?)',
                        (asin, time.time(), status, price, note))
        self.db.commit()
