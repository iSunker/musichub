import sys, os
from musichub.metadata.audio_inspector import AudioInspector
from musichub.core.models import CanonicalMusicItem
from musichub.routing.graph import RoutingGraphManager
from musichub.adapters.sites.hdkylin import HDKylinAdapter
from musichub.publisher.gateway import DeduplicationGateway
from musichub.publisher.engine import PublisherEngine
from musichub.core.qb_client import QBClient

insp = AudioInspector()
graph_mgr = RoutingGraphManager(db_path='/app/musichub/data/musichub.db')
adapter = HDKylinAdapter()
publish_qb = QBClient('192.168.0.7', 3010)
engine = PublisherEngine(publish_qb, graph_mgr)

cands = [
    ('/volume1/video/download/music/music3/The Doors - Greatest Hits 1996 FLAC US', 'The Doors - Greatest Hits 1996 FLAC US', 'c3c35f6116ed06f1f7b45a875e5223322dad1e2a', 'The Doors', 'Greatest Hits', '1996', 'CD', 'Lossless'),
    ('/volume1/video/download/music/music3/Morrissey - Southpaw Grammar (1995) [FLAC] {2009 Legacy Edition}', 'Morrissey - Southpaw Grammar (1995) [FLAC] {2009 Legacy Edition}', 'dca112c22f11b0717c4b197f17f1343cbad80869', 'Morrissey', 'Southpaw Grammar', '1995', 'CD', 'Lossless'),
    ('/volume1/video/download/music/music3/2026 Overmono - Pure Devotion WEB Qobuz [24Bit-44.1kHz] FLAC', '2026 Overmono - Pure Devotion WEB Qobuz [24Bit-44.1kHz] FLAC', 'd4059e0b2e53d8a11364b893a8f3e371de6a4e6b', 'Overmono', 'Pure Devotion', '2026', 'WEB-DL', 'Hi-Res')
]

for p_path, name, ih, artist, album, year, medium, standard in cands:
    print(f'=== Starting Live Publish to HDKylin: {name} ===', flush=True)
    tracks = insp.scan_album_directory(p_path)
    item = CanonicalMusicItem(
        title=name,
        album=album,
        artist=artist,
        year=year,
        format='FLAC',
        medium=medium,
        standard=standard,
        origin_site='opencd',
        origin_torrent_id=ih,
        local_path=p_path,
        tracks=tracks,
    )

    # 1. 物理就绪校验
    ready, reason = engine.verify_physical_data_readiness(item)
    if not ready:
        print(f'Physical readiness failed: {reason}', flush=True)
        continue

    # 2. 麒麟前置校验（主标题 Latin-1 检查）
    chk_ok, chk_msg = adapter.pre_publish_check(item)
    if not chk_ok:
        print(f'Pre-publish check failed: {chk_msg}', flush=True)
        continue

    # 3. 制种与自反验 (Torf)
    ok, msg, t_path = engine.create_torrent_safe(item, 'hdkylin', '')
    if not ok or not t_path:
        print(f'Create torrent failed: {msg}', flush=True)
        continue

    print(f'Torf created & verified: {t_path}', flush=True)

    # 4. 发布到 HDKylin
    pub_ok, tid, pub_torrent_bytes = adapter.publish(item, t_path)
    print(f'Publish result: ok={pub_ok}, tid={tid}', flush=True)

    if pub_ok and tid:
        graph_mgr.record_publish(item, 'hdkylin', target_torrent_id=tid)
        with open(t_path, 'rb') as f:
            local_bytes = f.read()
        readd_bytes = pub_torrent_bytes if pub_torrent_bytes else local_bytes
        # 5. 回加 3010 保活探针
        readd_ok, r_msg = engine.readd_publish_with_keepalive_probe(readd_bytes, item, timeout_seconds=120)
        print(f'Readd probe: ok={readd_ok}, msg={r_msg}', flush=True)

    if t_path and os.path.exists(t_path):
        os.remove(t_path)
