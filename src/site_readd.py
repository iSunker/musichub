"""把"丢失的站点种(B)"加回 qB，让站点重新看到做种（`#49` 护栏-2，写操作）。

═══════════════════════════════════════════════════════════════════════════
它在做的一件事（一句话）
═══════════════════════════════════════════════════════════════════════════
`site_seed_patrol`（`#39`）**只读**照出"站点页面挂的那条 B 现在没在做种"；本模块是它的
**执行臂**：把那条 **B**（从站点 `download.php` 取，**不是** DB 里的 A）加回 qB、
recheck 到 `progress=1`、reannounce，让站点重新收到我们的上报。

★ 为什么是"加 B"而不是"删 3040"或"加 A"（`#38`/`#44` 实测钉死）：
  C 类"页面 0 做种"的真相 = **给站点做种的那条 B 丢了**，只剩皇后原种 A
  （`cat=opencd`、只报 `tracker.open.cd`）⇒ 站点从没收到上报 ⇒ 页 0 做种。
  ⇒ 修法 = 把 **B** 加回；**不删 3040**、**不碰 A**。
★ ★★★ 身份铁律：**qB 的 `hash` 等于 B 的 infohash** 才算"这条 B 在做种"
  。

═══════════════════════════════════════════════════════════════════════════
★★ 与 `tools/readd_site_torrent.py` 的关系
═══════════════════════════════════════════════════════════════════════════
`tools/readd_site_torrent.py`（`#38`）是**手工当班用**的 CLI：显式给 `--ids`、逐条打印。
本模块把它**抽成库函数**，好让 daemon 线程（`#51`）能按 `site_seed_patrol` 的 `MISSING`
清单**自动**驱动同一条逻辑 —— 两边**同一份实现**。
★ 本模块**不做隔离决策**：它只"执行加回"；"该不该加回"由调用方按巡检结论声明
  （`MISSING` ⇒ 加回；`AMBIGUOUS` ⇒ 交给 `#50`；`NO_SITE_TORRENT` ⇒ 不碰）。

═══════════════════════════════════════════════════════════════════════════
安全
═══════════════════════════════════════════════════════════════════════════
· **默认 dry-run**；`dry_run=False` 才真写。
· **幂等**：B 已在 qB（按 hash）⇒ 直接返回"已存在"，不重复 add。
· **数据目录用 A 的 `content_path` 的父目录**（ 那个坑：多文件种
  `save_path` 必须是 `info.name` 的**父**目录，`/downloads`，不是专辑目录本身）。
· **回读 tracker `msg`**：`msg` 含 `same-location` ⇒ 判失败（：站点见
  到"我们自己的陈旧 peer 占着做种位"）；只有 `status∈{1,2}` 且无 same-location 才算成功。
"""
import os
import time

# 做种族（与 `handoff.SEEDING_STATES` 同款；含 checkingUP = 正常中间态）
_SEEDING_STATES = {"stalledUP", "uploading", "forcedUP", "queuedUP", "checkingUP"}


def _getattr(o, name, default=None):
    if isinstance(o, dict):
        return o.get(name, default)
    return getattr(o, name, default)


def resolve_save_path(qb_state_client, a_info_hash, *, fallback="/downloads"):
    """由**皇后原种 A** 在 qB 里的 `content_path` 求 B 的 `save_path`（= 其父目录）。

    ★ ：多文件种 qB 的 `save_path` 必须是 `info.name` 的**父**目录
      （`/downloads`）。拿 A 的 `content_path`（`/downloads/<专辑>`）当 save_path 会**错一层**
      ⇒ qB 去 `/downloads/<专辑>/<专辑>/…` 找 ⇒ `progress=0`（实测 `20517` 踩到）。
    ★ A 不在 qB / 查不到 ⇒ 退回 `fallback`（`/downloads`）—— 3020 的 `/downloads` 就是
      HDD 原件根，B 与 A 同盘同目录。
    """
    if qb_state_client is None or not a_info_hash:
        return fallback
    try:
        info = qb_state_client.torrents_info(torrent_hashes=a_info_hash)
    except Exception:                                # noqa: BLE001
        return fallback
    if not info:
        return fallback
    cp = str(_getattr(info[0], "content_path", "") or "").rstrip("/\\")
    if not cp:
        return fallback
    return os.path.dirname(cp) or fallback


def b_present(qb_clients, b_info_hash):
    """B（按 hash）是否已在任一 qB 里。返回 `(在不在, 在哪个)`。★ 幂等判据。"""
    want = str(b_info_hash or "").lower()
    if not want:
        return False, ""
    for which, qb in qb_clients:
        if qb is None:
            continue
        try:
            info = qb.torrents_info(torrent_hashes=want)
        except Exception:                            # noqa: BLE001
            continue
        if info:
            return True, which
    return False, ""


def readd_b_torrent(qb_target, raw_b, b_info_hash, save_path, *,
                    category=None, already_present=False, dry_run=True,
                    poll_tries=30, poll_delay=3, sleep=time.sleep,
                    tracker_reader=None, progress_reader=None):
    """把站点种 B 的**字节**加回 `qb_target`（paused → recheck → resume → reannounce → 回读）。

    :param qb_target: 目标 qB（3020）—— B 的落脚点。
    :param raw_b: 站点 `.torrent`(B) 的**字节**（调用方从 `download.php` 取）。
    :param b_info_hash: B 的 infohash（40-hex）—— **幂等键 + 回读键**（按 hash 认种）。
    :param save_path: B 的 `save_path`（= A 的 `content_path` 父目录；见 `resolve_save_path`）。
    :param category: 加回时打的分类（本项目白名单之一；缺省不打 —— 交棒那边会按名兜底）。
    :param already_present: 调用方已知 B 已在 qB（省一次查询）⇒ 直接成功短路。
    :param dry_run: True ⇒ 只返回"将做"，不写。
    :param tracker_reader: `callable(b_hash) -> [ (url, status, msg), ... ]` —— 回读 tracker。
      ★ 缺省用 `qb_target.torrents_trackers`。**为的是把 `same-location` 判出来**
      。★ 自检可注入假件。
    :param progress_reader: `callable(b_hash) -> (progress, state)` —— 回读进度/状态。
      缺省用 `qb_target.torrents_info`。自检可注入假件。
    :return: `(ok: bool, detail: str)`。**绝不抛**（调用方按 bool 决策）。
    """
    if already_present:
        return True, "B 已在 qB（幂等跳过）"
    if not raw_b:
        return False, "没有 B 的字节（download.php 返回空）"
    if not b_info_hash:
        return False, "没有 B 的 infohash（无法做幂等/回读键）"
    want = str(b_info_hash).lower()
    if dry_run:
        return True, (f"[dry-run] 将 add(B, save_path={save_path}, cat={category or '∅'})"
                      f" + recheck + resume + reannounce")

    def _default_trackers(h):
        out = []
        for x in qb_target.torrents_trackers(torrent_hash=h):
            url = str(_getattr(x, "url", "") or "")
            if url and "announce" in url:
                out.append((url, _getattr(x, "status", None), str(_getattr(x, "msg", "") or "")))
        return out

    def _default_progress(h):
        info = qb_target.torrents_info(torrent_hashes=h)
        if not info:
            return (0.0, "")
        return (float(_getattr(info[0], "progress", 0) or 0),
                str(_getattr(info[0], "state", "") or ""))

    trk = tracker_reader or _default_trackers
    prg = progress_reader or _default_progress

    try:
        # ★ 关键：`is_skip_checking=False` + 先 pause ⇒ 让 qB 对 save_path 里的**现成文件**
        #   做哈希校验（recheck）⇒ progress→1 ⇒ 才进入做种（否则当"新下载"，progress=0）。
        qb_target.torrents_add(torrent_files=raw_b, save_path=save_path,
                               is_paused=True, category=category or "",
                               is_skip_checking=False)
    except Exception as e:                            # noqa: BLE001
        # qB 对重复 add 常回 409；若已在 ⇒ 视为成功
        if b_present([("t", qb_target)], want)[0]:
            return True, f"add 报错但 B 已在（{type(e).__name__}）⇒ 视为已加"
        return False, f"add 失败：{type(e).__name__}: {e}"

    sleep(min(poll_delay, 3))
    try:
        qb_target.torrents_recheck(torrent_hashes=want)
    except Exception:                                 # noqa: BLE001
        pass
    prog, state = 0.0, ""
    for _ in range(poll_tries):
        sleep(poll_delay)
        try:
            prog, state = prg(want)
        except Exception:                             # noqa: BLE001
            continue
        if prog >= 1.0 or state in _SEEDING_STATES:
            break
    try:
        qb_target.torrents_resume(torrent_hashes=want)
    except Exception:                                 # noqa: BLE001
        pass
    sleep(2)
    try:
        qb_target.torrents_reannounce(torrent_hashes=want)
    except Exception:                                 # noqa: BLE001
        pass

    # ---- 回读：progress + tracker msg（same-location 判据）----
    sleep(5)
    try:
        prog, state = prg(want)
    except Exception:                                 # noqa: BLE001
        pass
    try:
        tracks = trk(want)
    except Exception as e:                            # noqa: BLE001
        tracks = []
        tracks_err = f"（回读 tracker 失败：{type(e).__name__}）"
    else:
        tracks_err = ""
    same_loc = [t for t in tracks if t[2] and "same-location" in t[2].lower().replace(" ", "-")]
    ok_status = [t for t in tracks if t[1] in (1, 2)]
    detail = (f"progress={prog:.2f} state={state} trk={[(t[0].split('/')[2], t[1], t[2][:40]) for t in tracks]}"
              f"{tracks_err}")
    if same_loc:
        return False, (f"★ 站点回 same-location（陈旧 peer 占做种位，）⇒ {detail}")
    if prog < 1.0:
        return False, f"progress 仍 <1（校验没认到本地文件？）⇒ {detail}"
    if not ok_status:
        return False, f"tracker 未就绪（无 status∈{{1,2}}）⇒ {detail}"
    return True, f"✅ 已加回并在做种 ⇒ {detail}"


def readd_row(qb_target, qb_state_client, row, raw_b, b_info_hash, *,
              category=None, dry_run=True, **kw):
    """巡检结果行 → 加回（便捷编排：自己算 save_path、查幂等、调 `readd_b_torrent`）。

    :param row: `site_seed_patrol` 的结果 dict（用 `info_hash`(A) 求 save_path）。
    """
    save_path = resolve_save_path(qb_state_client, row.get("info_hash"), fallback="/downloads")
    present, where = b_present([("target", qb_target)], b_info_hash)
    ok, detail = readd_b_torrent(qb_target, raw_b, b_info_hash, save_path,
                                 category=category, already_present=present,
                                 dry_run=dry_run, **kw)
    return ok, detail, save_path


def readd_missing(rows, *, qb_target, qb_state_client, download_for, category_for=None,
                  dry_run=True, only_verdicts=("MISSING",), **kw):
    """按巡检结果批量加回（daemon 线程 `#51` / CLI 都用这一份）。

    :param rows: `site_seed_patrol.patrol_row` 的结果 dict 列表。
    :param qb_target: B 的落脚 qB（3020）。
    :param qb_state_client: 用来查 A 的 `content_path`（求 save_path）的 qB（同 3020 即可）。
    :param download_for: `callable(site, torrent_id) -> bytes|None` —— 按站点取 B 的字节。
    :param category_for: `callable(site) -> str|None` —— 按站点取加回时的分类。
    :param only_verdicts: 只对这些判定动手（默认仅 `MISSING`；
      ★ `AMBIGUOUS` 属 `#50`、`NO_SITE_TORRENT`/`PRESENT`/`SEEDING` 不动）。
    :return: `list[(row, ok, detail)]`。
    """
    import site_seed_patrol as ssp
    out = []
    for row in rows:
        if row.get("verdict") not in only_verdicts:
            continue
        b_ih = row.get("b_ih")
        raw = download_for(row.get("site"), row.get("torrent_id"))
        cat = category_for(row.get("site")) if category_for else None
        ok, detail, save_path = readd_row(qb_target, qb_state_client, row, raw, b_ih,
                                          category=cat, dry_run=dry_run, **kw)
        out.append((row, ok, detail))
    return out


# ==================== 自检（不需要 .env / 不需要 qB）====================

class _FakeQBAdd:
    """假 qB：可配置 tracker 回读与 progress —— 自检用，**不碰真 qB**。

    ★ `has=None` ⇒ `torrents_info` 对**任意** hash 都返回该种（模拟"目标 qB 加完就持有 B"）；
      `has={...}` ⇒ 只对这些 hash 返回（模拟"只持有指定集合"，用于幂等/身份对照）。
    """
    def __init__(self, *, tracks=None, prog=(1.0, "stalledUP"), has=None, add_raises=False):
        self._tracks = tracks or []
        self._prog = prog
        self._has = None if has is None else set(has)
        self._add_raises = add_raises
        self.added = []

    def torrents_info(self, torrent_hashes=None, **kw):
        t = str(torrent_hashes or "").lower()
        if self._has is None:
            return [_FakeInfo(self._prog, t or "x")] if t else []
        if t and t in self._has:
            return [_FakeInfo(self._prog, t)]
        return []

    def torrents_add(self, **kw):
        if self._add_raises:
            raise RuntimeError("409 Conflict")
        self.added.append(kw)
        return "Ok."

    def torrents_recheck(self, **kw):
        return None

    def torrents_resume(self, **kw):
        return None

    def torrents_reannounce(self, **kw):
        return None

    def torrents_trackers(self, torrent_hash=None, **kw):
        return [_FakeTrk(u, s, m) for (u, s, m) in self._tracks]


class _FakeInfo:
    def __init__(self, prog, h, content_path=None):
        self.progress, self.state = prog
        self.hash = h
        self.content_path = content_path


class _FakeTrk:
    def __init__(self, url, status, msg):
        self.url, self.status, self.msg = url, status, msg


def _selftest():
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:                                 # noqa: BLE001
        pass
    fails = []

    def chk(label, got, want):
        ok = got == want
        print(f"  {'✅' if ok else '❌'} {label}  got={got!r} want={want!r}")
        if not ok:
            fails.append(label)

    no_sleep = lambda *_a, **_k: None                       # noqa: E731

    # ── ① dry-run ⇒ 成功、不写 ───────────────────────────────────
    qb = _FakeQBAdd(tracks=[("https://x/announce.php", 2, "")])
    ok, d = readd_b_torrent(qb, b"\x00", "a" * 40, "/downloads", dry_run=True, sleep=no_sleep)
    chk("① dry-run ⇒ ok", ok, True)
    chk("① dry-run 不 add", qb.added, [])

    # ── ② 幂等：已存在 ⇒ 短路成功、不 add ────────────────────────
    ok, d = readd_b_torrent(qb, b"\x00", "a" * 40, "/downloads",
                            already_present=True, dry_run=False, sleep=no_sleep)
    chk("② 已存在 ⇒ ok", ok, True)
    chk("② 已存在不 add", qb.added, [])

    # ── ③ 无字节 ⇒ 失败 ─────────────────────────────────────────
    ok, d = readd_b_torrent(qb, b"", "a" * 40, "/downloads", dry_run=False, sleep=no_sleep)
    chk("③ 无 B 字节 ⇒ 失败", ok, False)

    # ── ④ 正常：prog=1 + tracker status=2 ⇒ 成功 ─────────────────
    qb4 = _FakeQBAdd(tracks=[("https://x/announce.php?passkey=1", 2, "")], prog=(1.0, "stalledUP"))
    ok, d = readd_b_torrent(qb4, b"\x00", "b" * 40, "/downloads", dry_run=False, sleep=no_sleep)
    chk("④ 正常加回 ⇒ ok", ok, True)
    chk("④ 真 add 了一次", len(qb4.added), 1)
    chk("④ save_path 传对", qb4.added[0]["save_path"], "/downloads")

    qb5 = _FakeQBAdd(tracks=[("https://x/announce.php", 5,
                              "You cannot seed the same torrent in the same location from more than 1 client.")])
    ok, d = readd_b_torrent(qb5, b"\x00", "c" * 40, "/downloads", dry_run=False, sleep=no_sleep)
    chk("⑤ same-location ⇒ 失败", ok, False)
    chk("⑤ detail 标 same-location", "same-location" in d, True)

    # ── ⑥ progress<1 ⇒ 失败 ─────────────────────────────────────
    qb6 = _FakeQBAdd(tracks=[("https://x/announce.php", 2, "")], prog=(0.3, "downloading"))
    ok, d = readd_b_torrent(qb6, b"\x00", "d" * 40, "/downloads", dry_run=False, sleep=no_sleep)
    chk("⑥ progress<1 ⇒ 失败", ok, False)

    # ── ⑦ ★ 身份：qB 里 hash 不同 ⇒ b_present 判"不在"（不误认）──
    qb7 = _FakeQBAdd(has={"9" * 40})
    chk("⑦ 不同 hash ⇒ 判不在", b_present([("t", qb7)], "8" * 40)[0], False)
    chk("⑦ 同 hash ⇒ 判在", b_present([("t", qb7)], "9" * 40)[0], True)

    # ── ⑧ save_path 解析：多文件种取父目录 ───────────────────────
    class _QBA:
        def torrents_info(self, torrent_hashes=None, **kw):
            return [_FakeInfo((1.0, "x"), "a", content_path="/downloads/Album X")]
    chk("⑧ save_path=父目录", resolve_save_path(_QBA(), "a" * 40), "/downloads")
    chk("⑧ 查不到 ⇒ fallback", resolve_save_path(_QBA(), "z" * 40), "/downloads")

    print()
    if fails:
        print(f"❌ 自检失败 {len(fails)} 项：{fails}")
        return 1
    print("✅ site_readd 自检全绿")
    return 0


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    sys.exit("用法：python site_readd.py --selftest（库；CLI 见 tools/）")
