# -*- coding: utf-8 -*-
import subprocess

script = r'''
import sqlite3
import qb_client
import config
from types import SimpleNamespace

# Check HDKylin rows in site_publish
conn = sqlite3.connect('/data/data.db')
c = conn.cursor()
c.execute("""
    SELECT sp.torrent_id, sp.info_hash, sp.synced_at, t.torrent_name
    FROM site_publish sp
    JOIN tasks t ON t.info_hash = sp.info_hash
    WHERE sp.site = 'hdkylin' AND sp.status = 'EXISTS'
""")
hdkylin_rows = c.fetchall()

ports = [3010, 3220, 3240, 3260]
clients = {p: qb_client.build(SimpleNamespace(
    QB_HOST=f"http://192.168.0.7:{p}",
    QB_USER=config.QB_USER,
    QB_PASS=config.QB_PASS,
    QB_API_KEY=getattr(config, "QB_API_KEY", "")
)) for p in ports}

all_torrents = {}
for p, cli in clients.items():
    for t in cli.torrents_info():
        all_torrents[t.hash.lower()] = (p, t)

print(f"Total HDKylin rows in DB: {len(hdkylin_rows)}")
# Check which ones are in 3010 right now
hdkylin_3010 = [t for t in clients[3010].torrents_info() if t.category == 'HDKylin']
print(f"Total HDKylin category torrents on 3010: {len(hdkylin_3010)}")
for t in hdkylin_3010:
    print(f"  hash={t.hash[:12]} | state={t.state} | prog={t.progress} | name={t.name[:45]}")

print("\nDetail of 37 HDKylin DB records:")
for tid, ih, synced, name in hdkylin_rows:
    print(f"  tid={tid} | synced={synced} | name={name[:45]}")
'''

cmd = [
    'ssh', '-p', '36688',
    '-i', 'C:/Users/艾森克/.ssh/id_ed25519',
    'iSunker@192.168.0.7',
    '/usr/local/bin/docker exec -i opencd2musopia python -'
]

res = subprocess.run(cmd, input=script.encode('utf-8'), capture_output=True)
print("STDOUT:\n", res.stdout.decode('utf-8', errors='replace'))
print("STDERR:\n", res.stderr.decode('utf-8', errors='replace'))
