import sqlite3
db = sqlite3.connect('/config/manga_arr.db')
db.row_factory = sqlite3.Row

print("=" * 60)
print("END-TO-END STATE MACHINE VERIFICATION")
print("=" * 60)

# 1. Grabbed volumes
r = db.execute("""
  SELECT
    SUM(CASE WHEN grabbed_at IS NULL THEN 1 ELSE 0 END) AS no_grabbed_at,
    SUM(CASE WHEN torrent_name IS NULL THEN 1 ELSE 0 END) AS no_torrent_name,
    SUM(CASE WHEN indexer IS NULL THEN 1 ELSE 0 END) AS no_indexer,
    SUM(CASE WHEN protocol IS NULL THEN 1 ELSE 0 END) AS no_protocol,
    SUM(CASE WHEN source_url IS NULL THEN 1 ELSE 0 END) AS no_source_url,
    COUNT(*) AS total
  FROM volumes WHERE status='grabbed'
""").fetchone()
print("\n[1] GRABBED VOLUMES (n=%d):" % r['total'])
print("    no grabbed_at:   %d" % r['no_grabbed_at'])
print("    no torrent_name: %d" % r['no_torrent_name'])
print("    no indexer:      %d" % r['no_indexer'])
print("    no protocol:     %d" % r['no_protocol'])
print("    no source_url:   %d" % r['no_source_url'])

r = db.execute("""
  SELECT
    SUM(CASE WHEN import_path IS NULL THEN 1 ELSE 0 END) AS no_import_path,
    SUM(CASE WHEN quality IS NULL THEN 1 ELSE 0 END) AS no_quality,
    SUM(CASE WHEN imported_at IS NULL THEN 1 ELSE 0 END) AS no_imported_at,
    COUNT(*) AS total
  FROM volumes WHERE status='downloaded'
""").fetchone()
print("\n[2] DOWNLOADED VOLUMES (n=%d):" % r['total'])
print("    no import_path: %d" % r['no_import_path'])
print("    no quality:     %d" % r['no_quality'])
print("    no imported_at: %d" % r['no_imported_at'])

r = db.execute("""
  SELECT
    SUM(CASE WHEN grabbed_at IS NULL THEN 1 ELSE 0 END) AS no_grabbed_at,
    SUM(CASE WHEN indexer IS NULL THEN 1 ELSE 0 END) AS no_indexer,
    COUNT(*) AS total
  FROM chapters WHERE status='grabbed'
""").fetchone()
print("\n[3] GRABBED CHAPTERS (n=%d):" % r['total'])
print("    no grabbed_at:  %d" % r['no_grabbed_at'])
print("    no indexer:     %d" % r['no_indexer'])

r = db.execute("""
  SELECT
    SUM(CASE WHEN quality IS NULL THEN 1 ELSE 0 END) AS no_quality,
    SUM(CASE WHEN import_path IS NULL THEN 1 ELSE 0 END) AS no_import_path,
    SUM(CASE WHEN imported_at IS NULL THEN 1 ELSE 0 END) AS no_imported_at,
    SUM(CASE WHEN quality IS NULL AND import_path IS NULL THEN 1 ELSE 0 END) AS ghost_dl,
    COUNT(*) AS total
  FROM chapters WHERE status='downloaded'
""").fetchone()
print("\n[4] DOWNLOADED CHAPTERS (n=%d):" % r['total'])
print("    no quality:     %d" % r['no_quality'])
print("    no import_path: %d" % r['no_import_path'])
print("    no imported_at: %d" % r['no_imported_at'])
print("    ghost (no file, no quality): %d" % r['ghost_dl'])

r = db.execute("SELECT status, COUNT(*) as n FROM import_queue GROUP BY status").fetchall()
print("\n[5] IMPORT QUEUE STATES:")
if not r:
    print("    (empty)")
for row in r:
    print("    %s: %d" % (row['status'], row['n']))

r = db.execute("SELECT COUNT(*) AS n FROM volumes WHERE status='grabbed' AND grabbed_at < datetime('now', '-2 days')").fetchone()
print("\n[6] STUCK GRABBED (>2 days): %d" % r['n'])

r = db.execute("SELECT COUNT(*) AS n FROM blocklist").fetchone()
print("\n[7] BLOCKLIST ENTRIES: %d" % r['n'])

print("\n[8] Settings:")
for key in ('blocklist_ttl_days', 'api_key'):
    r = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    v = r['value'] if r else '(missing)'
    if key == 'api_key':
        v = 'set' if v and v != '(missing)' else 'EMPTY - security risk'
    print("    %s: %s" % (key, v))

r = db.execute("SELECT COUNT(*) AS n FROM chapters c WHERE c.volume_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM volumes v WHERE v.id = c.volume_id)").fetchone()
print("\n[9] ORPHANED CHAPTERS (volume_id points to missing volume): %d" % r['n'])

r = db.execute("SELECT COUNT(*) AS n FROM volumes v WHERE NOT EXISTS (SELECT 1 FROM series s WHERE s.id = v.series_id)").fetchone()
print("    ORPHANED VOLUMES (series_id points to missing series): %d" % r['n'])

print("\n" + "=" * 60)
print("DONE")
