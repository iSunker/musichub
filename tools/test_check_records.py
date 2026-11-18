import sqlite3

con = sqlite3.connect("/data/data.db")
cur = con.cursor()
print("tangpt records:", cur.execute("SELECT info_hash, torrent_id, status, synced_at FROM site_publish WHERE site='tangpt'").fetchall())
print("muxuege records:", cur.execute("SELECT info_hash, torrent_id, status, synced_at FROM site_publish WHERE site='muxuege'").fetchall())
