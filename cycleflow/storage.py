import sqlite3
from contextlib import contextmanager
from pathlib import Path


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, external_id TEXT NOT NULL,
  kind TEXT NOT NULL, occurred_at TEXT NOT NULL, received_at TEXT NOT NULL,
  payload TEXT NOT NULL, fingerprint TEXT NOT NULL, state TEXT NOT NULL,
  error_code TEXT, error_text TEXT, attempts INTEGER NOT NULL DEFAULT 0,
  UNIQUE(source, external_id));
CREATE INDEX IF NOT EXISTS ix_events_state ON events(state, id);
CREATE INDEX IF NOT EXISTS ix_events_time ON events(occurred_at, id);
CREATE TABLE IF NOT EXISTS designs (
  design_id TEXT PRIMARY KEY, product TEXT NOT NULL, revision INTEGER NOT NULL,
  parent_id TEXT REFERENCES designs(design_id), released_at TEXT NOT NULL,
  specification TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'released',
  UNIQUE(product, revision));
CREATE TABLE IF NOT EXISTS requirements (
  design_id TEXT NOT NULL REFERENCES designs(design_id), part TEXT NOT NULL,
  quantity INTEGER NOT NULL CHECK(quantity>0), PRIMARY KEY(design_id, part));
CREATE TABLE IF NOT EXISTS lots (
  lot_id TEXT PRIMARY KEY, part TEXT NOT NULL, supplier TEXT NOT NULL,
  received_at TEXT NOT NULL, quantity INTEGER NOT NULL CHECK(quantity>0),
  quarantined INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS jobs (
  job_id TEXT PRIMARY KEY, design_id TEXT NOT NULL REFERENCES designs(design_id),
  quantity INTEGER NOT NULL CHECK(quantity>0), opened_at TEXT NOT NULL,
  status TEXT NOT NULL, completed_at TEXT, parent_job TEXT REFERENCES jobs(job_id),
  failure_reason TEXT);
CREATE TABLE IF NOT EXISTS reservations (
  job_id TEXT NOT NULL REFERENCES jobs(job_id), lot_id TEXT NOT NULL REFERENCES lots(lot_id),
  quantity INTEGER NOT NULL CHECK(quantity>0), PRIMARY KEY(job_id, lot_id));
CREATE TABLE IF NOT EXISTS job_steps (
  job_id TEXT NOT NULL REFERENCES jobs(job_id), stage TEXT NOT NULL,
  started_at TEXT NOT NULL, completed_at TEXT, event_id INTEGER NOT NULL REFERENCES events(id),
  PRIMARY KEY(job_id, stage));
CREATE TABLE IF NOT EXISTS plans (
  plan_id TEXT PRIMARY KEY, product TEXT NOT NULL, revision INTEGER NOT NULL,
  minimum_pass_rate TEXT NOT NULL, effective_at TEXT NOT NULL,
  UNIQUE(product, revision));
CREATE TABLE IF NOT EXISTS tests (
  test_id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(job_id),
  plan_id TEXT NOT NULL REFERENCES plans(plan_id), tested_at TEXT NOT NULL,
  passed INTEGER NOT NULL, failed INTEGER NOT NULL, note TEXT NOT NULL,
  event_id INTEGER NOT NULL REFERENCES events(id));
CREATE TABLE IF NOT EXISTS equipment (
  device_id TEXT PRIMARY KEY, name TEXT NOT NULL,
  registered_at TEXT NOT NULL, event_id INTEGER NOT NULL REFERENCES events(id));
CREATE TABLE IF NOT EXISTS calibrations (
  calibration_id TEXT PRIMARY KEY, device_id TEXT NOT NULL REFERENCES equipment(device_id),
  valid_from TEXT NOT NULL, valid_to TEXT NOT NULL, recorded_at TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'active', event_id INTEGER NOT NULL REFERENCES events(id));
CREATE INDEX IF NOT EXISTS ix_calibrations_device ON calibrations(device_id, valid_from, valid_to);
CREATE TABLE IF NOT EXISTS releases (
  release_id TEXT PRIMARY KEY, product TEXT NOT NULL, window_start TEXT NOT NULL,
  window_end TEXT NOT NULL, created_at TEXT NOT NULL, report TEXT NOT NULL,
  fingerprint TEXT NOT NULL, source_head INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS batches (
  batch_id TEXT PRIMARY KEY, manifest TEXT NOT NULL, next_line INTEGER NOT NULL,
  total_lines INTEGER NOT NULL, state TEXT NOT NULL, updated_at TEXT NOT NULL);
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        db = self.connect()
        try:
            db.executescript(SCHEMA)
            self._migrate(db)
        finally:
            db.close()

    def _migrate(self, db):
        columns = {row[1] for row in db.execute("PRAGMA table_info(tests)")}
        if "device_id" not in columns:
            db.execute("ALTER TABLE tests ADD COLUMN device_id TEXT REFERENCES equipment(device_id)")
        if "verified" not in columns:
            db.execute("ALTER TABLE tests ADD COLUMN verified INTEGER NOT NULL DEFAULT 1")
        if "superseded" not in columns:
            db.execute("ALTER TABLE tests ADD COLUMN superseded INTEGER NOT NULL DEFAULT 0")
        if "verify_reason" not in columns:
            db.execute("ALTER TABLE tests ADD COLUMN verify_reason TEXT")

    def connect(self):
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=15000")
        return db

    @contextmanager
    def transaction(self):
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @contextmanager
    def read(self):
        db = self.connect()
        try:
            yield db
        finally:
            db.close()

    def scalar(self, sql: str, args=()):
        with self.read() as db:
            row = db.execute(sql, args).fetchone()
            return row[0] if row else None
