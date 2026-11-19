import sys, os
from musichub.metadata.audio_inspector import AudioInspector
from musichub.core.models import CanonicalMusicItem
from musichub.routing.graph import RoutingGraphManager
from musichub.adapters.sites.xingwan import XingwanAdapter
from musichub.publisher.gateway import DeduplicationGateway
from musichub.publisher.engine import PublisherEngine
from musichub.core.qb_client import QBClient

insp = AudioInspector()
graph_mgr = RoutingGraphManager(db_path='/app/musichub/data/musichub.db')
adapter = XingwanAdapter()
publish_qb = QBClient('192.168.0.7', 3010)
engine = PublisherEngine(publish_qb, graph_mgr)

cands = [
    ('/volume1/video/download/music/music3/林淑娟 - 经典金曲vol.2 2006 FLAC', '林淑娟 - 经典金曲vol.2 2006 FLAC', '4f0a9113276d44088219c68e360112781cdc9abe', '林淑娟', '经典金曲vol.2', '2006'),
    ('/volume1/video/download/music/music3/1995 - 愿望 (新曲+精选) [FLAC] {HK - Polydor 527 611-2}', '1995 - 愿望 (新曲+精选) [FLAC] {HK - Polydor 527 611-2}', '43fa3eb3979d893de65b3b3f50904b3feb6a8e54', '群星', '愿望 (新曲+精选)', '1995')
]

for p_path, name, ih, artist, album, year in cands:
    print('=== Processing for Xingwan: ' + name + ' ===', flush=True)
    tracks = insp.scan_album_directory(p_path)
    item = CanonicalMusicItem(
        title=name,
        album=album,
        artist=artist,
        year=year,
        format='FLAC',
        medium='CD',
        standard='Lossless',
        origin_site='opencd',
        origin_torrent_id=ih,
        local_path=p_path,
        tracks=tracks,
    )

    # 1. 查重
    exists, why = adapter.check_exists(item)
    print('Check exists on Xingwan: ' + str(exists) + ' (' + why + ')', flush=True)
    if exists:
        print('Skip publish due to exists on Xingwan', flush=True)
        continue

    # 2. 物理就绪
    ready, reason = engine.verify_physical_data_readiness(item)
    if not ready:
        print('Physical readiness failed: ' + reason, flush=True)
        continue

    # 3. 制种与分块校验
    ok, msg, t_path = engine.create_torrent_safe(item, 'Xingwan', '')
    if not ok or not t_path:
        print('Create torrent failed: ' + msg, flush=True)
        continue
    print('Torf created & verified: ' + t_path, flush=True)

    # 4. 发布
    pub_ok, tid, pub_torrent_bytes = adapter.publish(item, t_path)
    print('Publish result: ok=' + str(pub_ok) + ', tid=' + str(tid), flush=True)

    if pub_ok and tid:
        graph_mgr.record_publish(item, 'xingwan', target_torrent_id=tid)
        with open(t_path, 'rb') as f:
            local_bytes = f.read()
        readd_bytes = pub_torrent_bytes if pub_torrent_bytes else local_bytes
        # 5. 回加与保活探针
        readd_ok, r_msg = engine.readd_publish_with_keepalive_probe(readd_bytes, item, timeout_seconds=120)
        print('Readd probe: ok=' + str(readd_ok) + ', msg=' + r_msg, flush=True)

    if t_path and os.path.exists(t_path):
        os.remove(t_path)
