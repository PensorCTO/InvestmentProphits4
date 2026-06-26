from database.replica_store import open_replica

conn = open_replica()
cursor = conn.cursor()

cursor.execute("PRAGMA table_info(trade_execution);")
columns = cursor.fetchall()
for col in columns:
    print(col[1])

print("---")

cursor.execute("SELECT * FROM trade_execution ORDER BY closed_at DESC LIMIT 5")
rows = cursor.fetchall()
for r in rows:
    print(r)

conn.close()
