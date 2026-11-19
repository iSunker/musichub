import sys
from musichub.metadata.audio_inspector import AudioInspector
from musichub.core.models import CanonicalMusicItem
from musichub.routing.graph import RoutingGraphManager
from musichub.adapters.sites.hdkylin import HDKylinAdapter
from musichub.publisher.gateway import DeduplicationGateway

insp = AudioInspector()
graph_mgr = RoutingGraphManager(db_path='/app/musichub/data/musichub.db')
adapter = HDKylinAdapter()
gateway = DeduplicationGateway(adapters=[adapter], graph_mgr=graph_mgr)

cands = [
    ('/volume1/video/download/music/music3/The Doors - Greatest Hits 1996 FLAC US', 'The Doors - Greatest Hits 1996 FLAC US', 'c3c35f6116ed06f1f7b45a875e5223322dad1e2a', 'The Doors', 'Greatest Hits', '1996'),
    ('/volume1/video/download/music/music3/Karajan - 6 Concerti 1972 FLAC', 'Karajan - 6 Concerti 1972 FLAC', '295d24f6d3df2f250e11f95b9a199a64b3fe8693', 'Karajan', '6 Concerti', '1972'),
    ('/volume1/video/download/music/music3/Morrissey - Southpaw Grammar (1995) [FLAC] {2009 Legacy Edition}', 'Morrissey - Southpaw Grammar (1995) [FLAC] {2009 Legacy Edition}', 'dca112c22f11b0717c4b197f17f1343cbad80869', 'Morrissey', 'Southpaw Grammar', '1995')
]

for p_path, name, ih, artist, album, year in cands:
    tracks = insp.scan_album_directory(p_path)
    item = CanonicalMusicItem(
        title=name,
        album=album,
        artist=artist,
        year=year,
        format='FLAC',
        origin_site='opencd',
        origin_torrent_id=ih,
        local_path=p_path,
        tracks=tracks,
    )
    chk_ok, chk_msg = adapter.pre_publish_check(item)
    print(f'=== {name} ===')
    print(f'  pre_publish_check: {chk_ok} ({chk_msg})')
    main_t, sub_t = adapter.format_title(item)
    print(f'  main_title: {main_t}')
    print(f'  sub_title: {sub_t}')
    decisions = gateway.evaluate_targets(item)
    print(f'  gateway allowed: {decisions.get("HDKylin")}')
