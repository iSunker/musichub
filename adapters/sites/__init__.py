# -*- coding: utf-8 -*-
"""MusicHub 3.0 目标站点适配器统一导出入口

支持全站独立模块化接入：
1. Musopia (乌托邦)
2. CrabPT (大闸蟹)
3. PTerClub (猫站)
4. Xingwan (星湾)
5. HDKylin (麒麟)
6. TangPT (唐门 / 不可躺)
7. MuXueGe (慕雪阁)
8. OpenCD (皇后)
"""

from musichub.adapters.sites.musopia import MusopiaAdapter
from musichub.adapters.sites.crabpt import CrabPTAdapter
from musichub.adapters.sites.pterclub import PTerClubAdapter
from musichub.adapters.sites.xingwan import XingwanAdapter
from musichub.adapters.sites.hdkylin import HDKylinAdapter
from musichub.adapters.sites.tangpt import TangPTAdapter
from musichub.adapters.sites.muxuege import MuXueGeAdapter
from musichub.adapters.sites.opencd import OpenCDAdapter

__all__ = [
    "MusopiaAdapter",
    "CrabPTAdapter",
    "PTerClubAdapter",
    "XingwanAdapter",
    "HDKylinAdapter",
    "TangPTAdapter",
    "MuXueGeAdapter",
    "OpenCDAdapter",
]
