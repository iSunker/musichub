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
    ('/volume1/video/download/music/music3/2026 Overmono - Pure Devotion WEB Qobuz [24Bit-44.1kHz] FLAC', '2026 Overmono - Pure Devotion WEB Qobuz [24Bit-44.1kHz] FLAC', 'd4059e0b2e53d8a11364b893a8f3e371de6a4e6b', 'Overmono', 'Pure Devotion', '2026'),
    ('/volume1/video/download/music/music3/2026 Tyler Ballgame - For The First Time, Again WEB Qobuz [24Bit-44.1kHz] FLAC [E]', '2026 Tyler Ballgame - For The First Time, Again WEB Qobuz [24Bit-44.1kHz] FLAC [E]', 'f6075e6a9245ddf441ee9e9f7e0aa326e9a9aea7', 'Tyler Ballgame', 'For The First Time, Again', '2026'),
    ('/volume1/video/download/music/music3/2024 Jamie xx - In Waves WEB Qobuz [24Bit-44.1kHz] FLAC [E]', '2024 Jamie xx - In Waves WEB Qobuz [24Bit-44.1kHz] FLAC [E]', 'ca0043c440dd95b3336ee3021028353aba3970f6', 'Jamie xx', 'In Waves', '2024')
]

for p_path, name, ih, artist, album, year in cands:
    tracks = insp.scan_album_directory(p_path)
    item = CanonicalMusicItem(
        title=name,
        album=album,
        artist=artist,
        year=year,
        format='FLAC',
        medium='WEB-DL',
        standard='Hi-Res',
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
