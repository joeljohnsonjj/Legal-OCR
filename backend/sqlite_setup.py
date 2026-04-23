import sqlite3, pathlib
db = pathlib.Path("output/session_store.sqlite")
db.parent.mkdir(parents=True, exist_ok=True)
sql = pathlib.Path("memory_management/session_manager/setup_database_sqlite.sql").read_text(encoding="utf-8")
conn = sqlite3.connect(db)
conn.executescript(sql)
conn.commit()
conn.close()
print("SQLite schema created:", db)