# -*- coding: utf-8 -*-
"""向下兼容桥接模块：重定向导入至 musichub.adapters.sites 目录包"""

from musichub.adapters.sites import (
    MusopiaAdapter,
    CrabPTAdapter,
    PTerClubAdapter,
    XingwanAdapter,
    HDKylinAdapter,
    TangPTAdapter,
    MuXueGeAdapter,
    OpenCDAdapter,
)

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
