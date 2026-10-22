CREATE TABLE IF NOT EXISTS mika_sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  url TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
  last_ok TEXT,
  raw_count INTEGER NOT NULL DEFAULT 0,
  valid_count INTEGER NOT NULL DEFAULT 0,
  exported_count INTEGER NOT NULL DEFAULT 0,
  last_error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mika_meta (
  singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
  current_version TEXT,
  previous_version TEXT,
  public_token_hash TEXT,
  published_at TEXT
);
INSERT OR IGNORE INTO mika_meta(singleton) VALUES (1);
CREATE TABLE IF NOT EXISTS mika_node_ids (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  hash TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS mika_builds (
  version TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  total INTEGER NOT NULL,
  parts INTEGER NOT NULL,
  raw_bytes INTEGER NOT NULL,
  b64_bytes INTEGER NOT NULL,
  raw_written INTEGER NOT NULL DEFAULT 0,
  b64_written INTEGER NOT NULL DEFAULT 0,
  stats TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'staging'
);
CREATE TABLE IF NOT EXISTS mika_build_parts (
  version TEXT NOT NULL,
  part INTEGER NOT NULL,
  count INTEGER NOT NULL,
  PRIMARY KEY(version,part)
);
CREATE TABLE IF NOT EXISTS mika_audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  action TEXT NOT NULL,
  source_id INTEGER,
  detail TEXT NOT NULL DEFAULT ''
);
