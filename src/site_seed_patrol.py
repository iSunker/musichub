"""只读巡检：**每条"DB 说发过"的行，站点种(B) 现在到底有没有在做种**（#39）。

═══════════════════════════════════════════════════════════════════════════
它存在的理由（一句话）
═══════════════════════════════════════════════════════════════════════════
C 类"页面 0 做种"的根因（`#36`/`#43`/`#44` 实测钉死，见 `.scratch/C_group_rootcause.md`）：
**给站点上报做种的那条 `.torrent`(B) 丢了** —— 只剩皇后原种 A（`cat=opencd`，只报
`tracker.open.cd`）。站点从没收到我们的上报 ⇒ 页 0 做种。
⇒ 需要一个**只读**巡检：**在缺陷扩大前**把"哪条 B 没了"照出来（`#38` 是事后补救；
本模块是**事前的眼睛**，`#49` 才拿它驱动自动加回）。

═══════════════════════════════════════════════════════════════════════════
★★★ 身份铁律：**B ≠ A**（拿错 hash 查 = 全部假报"丢失"）
═══════════════════════════════════════════════════════════════════════════
站点收下种子后会**重写** announce/passkey ⇒ **infohash 变**。
⇒ DB 的 `site_publish.info_hash` = **皇后原种 A**；站点 `download.php` 那份 = **站点种 B**；
   而 **qB 里在做种的是 B** ⇒ **判据必须用 B**、**必须在 qB 里按 B 查**
   （拿 A 去查会查不到 ⇒ 把**正常在做种**的种报成 MISSING，全是假阳性）。
   —— `#45` 就是在这条上栽的（交棒按 A 查 3040 ⇒ 找不到 ⇒ 永不交棒）。

★ 那"B 在哪些 qB 上"？**唯一**判据 = **infohash 相等**（`qB.hash == info_hash(B)`，
  qB 的 `hash`/`infohash_v1` 字段）—— 这是把"就是这条 B"与"别的种"分开的**唯一**办法。
  ★★★ `#39` 实测教训（初版写错、被自己抓出来）：初版拿 **`category ∈ HANDOFF_OWN_CATEGORIES`**
    当命中判据 ⇒ 3020 里有 **648 条本项目别的专辑**（598 Musopia + 50 CrabPT）也被算进来
    ⇒ 每条种都报"命中 649 条 ⇒ AMBIGUOUS"，**全是假阳性**。
    **category 是"专辑级"的标签，不是"种级"的身份** —— 拿它认种 = 拿门牌号认人。
  ★ 正解：**hash 相等才是身份**；`category` 只作**旁证**（`how` 里标"hash+自分类/外分类"，
    用来提示"这条 B 在做种、但分类被人改过"这种旁支异常），**不作命中条件**。
  ★ 名字同理：只作旁证，不作命中（同专辑多版本共享 `info.name`，`#136` 的形态）。

═══════════════════════════════════════════════════════════════════════════
★★ 三态（"查不到" ≠ "没在做种"， 的形状）
═══════════════════════════════════════════════════════════════════════════
    · `NO_SITE_TORRENT` —— 有没有 B 都没**定论**：缺 `torrent_id` / 下载失败 / 站点壳页。
                             ★ **不是** MISSING（绝不许把"不知道"记成"丢了"）。
    · `MISSING`         —— 有 B，但 qB 里**查不到任何**本项目名下的该 B ⇒ 真的丢了。
    · `PRESENT`         —— qB 里**在**，但 `progress < 1`（还没校验完 / 从零在下载）。
    · `SEEDING`         —— qB 里在，`progress ≥ 1` 且 `state ∈ 做种族` ⇒ 健康。
    · `AMBIGUOUS`       —— 命中多条同 B ⇒ 有隐患（同位置多客户端 / 交棒残留）。

★ 本模块**只读**：不写库、不改 qB、不发种、不删页。要么给 `db.connect()`（只读库），
  要么调用方**显式**给 `sf_rows`（便于自检完全脱离生产）。

═══════════════════════════════════════════════════════════════════════════
自检：`python site_seed_patrol.py --selftest`（**不需要 `.env`** —— 全用假件）
═══════════════════════════════════════════════════════════════════════════
"""
import io
import sys

import site_verify as sv

# ==================== 判定常量 ====================

SEEDING = "SEEDING"            # 在、校验完、在做种  ⇒ 健康
PRESENT = "PRESENT"            # 在、但还没校验完/仍在下载
MISSING = "MISSING"            # 有 B，但 qB 里查不到 ⇒ 丢了（= #38/#49 要治的）
AMBIGUOUS = "AMBIGUOUS"        # 命中多条同 B ⇒ 隐患（同位置 / 残留）
NO_SITE_TORRENT = "NO_SITE_TORRENT"   # 没有 B 可查（缺 id / 下不动 / 壳页）—— 不是"丢了"

# 有缺陷的判定（决定 `tools/check_site_seeding.py` 的退出码 —— 同 `site_verify.DEFECTIVE` 的用法）
DEFECTIVE = (MISSING, AMBIGUOUS)

# 做种族（含 `checkingUP`：校验在跑也是"在做种"的正常中间态 —— 与 `handoff.verify_3020_seeding` 同款）
SEEDING_STATES = {"stalledUP", "uploading", "forcedUP", "queuedUP", "checkingUP"}

# qB 工具用的哈希定位方式（本模块只用来"制造合理候选"，与名字/分类哪种命中无关）
_LOCATING_HASH_LEN = 40


def _getattr(o, name, default=None):
    """从对象或 dict 里取字段（qB 客户端返回的是对象；自检给的是 dict）。"""
    if isinstance(o, dict):
        return o.get(name, default)
    return getattr(o, name, default)


def _exact_name_eq(a, b):
    """qB 的 `name` 精确相等（两边都是 qB 侧的 `name`，同源 ⇒ 不折空白、不 NFC）。

    ★ 与 `site_verify.normalize_name` **不同**：那里比的是"DB 声称名 vs 站点 info.name"
      （两边可能被不同环节做过空白处理 ⇒ 要折）；这里比的是"两个 qB 实例里的 `name`"
      —— 同一套 qB 生成的 ⇒ 差一个字符就是**真的不同种** ⇒ **必须**精确比。
    """
    if a is None or b is None:
        return False
    return str(a) == str(b)


def classify_qb_state(progress, state, *, seeding_states=None):
    """把 qB 的 `(progress, state)` 收成一态（`SEEDING`/`PRESENT`）。"""
    sts = SEEDING_STATES if seeding_states is None else seeding_states
    try:
        prog = float(progress if progress is not None else 0)
    except (TypeError, ValueError):
        prog = 0.0
    if prog >= 1.0 and str(state or "") in sts:
        return SEEDING
    return PRESENT


def _hash_of(t):
    """取 qB 一条种的 infohash（v1）。★ 用 `hash`/`infohash_v1` —— **不是** name。"""
    for k in ("hash", "infohash_v1"):
        v = _getattr(t, k, None)
        if v:
            return str(v).lower()
    return ""


def _find_in_qb(qb, b_ih, info_name, *, own_categories=None, qb_list=None):
    """在**一个** qB 里**按身份**找 B。返回 `(hits, how)`。

    ★★★ **铁律：判据是 infohash 相等** —— `qB.hash == b_ih`。
      这是唯一能把"就是这条 B"与"别的种"分开的判据。**name/category 都不是身份**
      （`#39` 实测教训：初版按 `category ∈ 白名单` 判 ⇒ 3020 里 648 条本项目的**别的**
        专辑全被算进来 ⇒ 每条都报 `AMBIGUOUS`（649 条），**全是假阳性**）。
      ⇒ name/category 只作**旁证**（`how`/`note` 里标注），不作命中条件。

    返回 `(hits, how)`：
      · `hits` = 该 qB 里 hash == b_ih 的种（通常 0 或 1 —— 若 >1 那是同一 infohash
        加了两次，qB 通常不允许；真出现就是隐患 ⇒ 上层报 AMBIGUOUS）。
      · `how` ∈ `{"hash","hash+自分类","hash+外分类","hash+无名"}` —— 旁证备注（不影响命中）。
    """
    if own_categories is None:
        own_categories = set()
    own = {str(c).strip() for c in own_categories if str(c or "").strip()}
    if qb_list is None:
        all_t = qb.torrents_info()
    else:
        all_t = qb_list(qb)

    want = str(b_ih or "").lower()
    hits, cats = [], set()
    if want:
        for t in all_t:
            if _hash_of(t) == want:
                hits.append(t)
                cats.add(str(_getattr(t, "category", "") or "").strip())
    if not hits:
        return [], ""
    if cats & own:
        how = "hash+自分类"
    elif cats - {""}:
        how = "hash+外分类"       # ★ 旁证异常：B 在做种，但挂的是别站的分类（该报出来）
    else:
        how = "hash"
    return hits, how


def patrol_row(row, *, download=None, qb_3020=None, qb_3040=None,
               own_categories=None, qb_list=None, site_verify_mod=sv, qb_extra=None,
               qb_publish=None):
    """巡检**一条** `site_publish`(EXISTS) 行。只读。返回结果 dict（**不抛**）。

    :param row: dict，至少含 `site`/`info_hash`/`torrent_id`/`db_name`（见 `iter_exists_rows`）。
    :param download: `callable(torrent_id) -> bytes|None` —— 从站点下 B 的字节。
      ★ 自检传假件；生产由 `tools/check_site_seeding.py` 传对应站点客户端的 `download_torrent`。
      ★ 为 `None` ⇒ 直接 `NO_SITE_TORRENT`（没 B 可查）。
    :param qb_3020: 皇后原种库客户端（3020/music3）。
    :param qb_publish / qb_3040: 发种实例客户端（目标做种端，生产实值 = 3010）。
    :param own_categories: 本项目分类白名单；`None` ⇒ `config.HANDOFF_OWN_CATEGORIES`。
      ★ 只有 `None`（**未显式传集合**）时才 import config —— 自检显式传 ⇒ 不碰 `.env`。
    :param qb_list: `callable(qb) -> list` —— 取某 qB 的全表种（**默认 `qb.torrents_info()`**）。
      ★ 生产由 CLI 传一个**带重试**的取数器（3020 WebUI 有间歇 SSL 抖动；`#43` 实测）
        ⇒ 否则**一次 SSL 抖动**会把整行误判成 `NO_SITE_TORRENT`（看着像"没 B"，其实是"连不上"）。
      ★ 自检传假件 ⇒ 直接调 `qb.torrents_info()`。

    结果 dict = row 的字段 + `b_ih`/`info_name`/`found_in`/`how`/`matches`/`verdict`/`note`。
    ★ **qB 连不上** ⇒ 抛 `RuntimeError`（**不**收成行判定）—— 那是**环境**问题不是"这条种的问题"，
      必须让上层（CLI）以退出码 2 明确报出，绝不能伪装成 `NO_SITE_TORRENT`。
    """
    if qb_list is None:
        def _default_lister(_qb):
            return _qb.torrents_info()
        qb_list = _default_lister
    res = dict(row)
    res.update(b_ih=None, info_name=None, found_in=[], how="", matches=0, note="")

    tid = row.get("torrent_id")
    if not tid:
        res["verdict"] = NO_SITE_TORRENT
        res["note"] = "该行没有站点页面 id（site_publish.torrent_id 空）⇒ 无 B 可查"
        return res
    if download is None:
        res["verdict"] = NO_SITE_TORRENT
        res["note"] = "调用方未提供 download ⇒ 无法取 B（不猜）"
        return res

    # ① 取站点那份 .torrent(B) 的字节
    try:
        raw = download(tid)
    except Exception as e:                                    # noqa: BLE001
        res["verdict"] = NO_SITE_TORRENT
        res["note"] = f"下载站点种失败：{type(e).__name__}: {e}"
        return res
    if not raw:
        res["verdict"] = NO_SITE_TORRENT
        res["note"] = "download.php 未返回 .torrent（非 bencode/无权限/壳页）"
        return res

    # ② 从 B 里取权威 info.name 与 infohash
    info_name, why = site_verify_mod.extract_info_name(raw, torf_mod=None)
    if info_name is None:
        res["verdict"] = NO_SITE_TORRENT
        res["note"] = f"站点副本读不出 info.name：{why}"
        return res
    res["info_name"] = info_name
    try:
        import torf
        b_ih = str(torf.Torrent.read_stream(io.BytesIO(raw)).infohash)
    except Exception as e:                                    # noqa: BLE001
        res["verdict"] = NO_SITE_TORRENT
        res["note"] = f"算 B 的 infohash 失败：{type(e).__name__}: {e}"
        return res
    res["b_ih"] = b_ih

    # ③ 在 qB 里找"本项目名下的 B"
    #    ★ 统一做种端：qb_publish 为生产发种目标实例（3010），qb_3020 为原种库。
    if own_categories is None:
        import config
        own_categories = getattr(config, "HANDOFF_OWN_CATEGORIES", None)
    target_pub = qb_publish if qb_publish is not None else qb_3040
    clients = [("3020", qb_3020), ("3010", target_pub)] + list(qb_extra or [])
    hits = []                      # [(which, torrent, how)]
    for which, qb in clients:
        if qb is None:
            continue
        try:
            ms, how = _find_in_qb(qb, b_ih, info_name, own_categories=own_categories,
                                  qb_list=qb_list)
        except Exception as e:                                # noqa: BLE001
            # ★ qB 连不上 ⇒ **抛**（环境问题），绝不折成行判定（否则假报 MISSING/NO_SITE_TORRENT）
            # ★ 扩展实例（`qb_extra`）除外：它是**加严**而非基线，连不上时只跳过该实例，
            #   不能因为一个可选实例挂了就把整轮巡检作废。
            if which not in ("3020", "3010", "3040"):
                res["note"] = (res.get("note") or "") + \
                    f" [扩展实例 {which} 查询失败，已跳过: {type(e).__name__}]"
                continue
            raise RuntimeError(f"查 {which} 的种列表失败：{type(e).__name__}: {e}") from e
        for t in ms:
            hits.append((which, t, how))
    res["matches"] = len(hits)
    res["found_in"] = [w for w, _, _ in hits]
    res["how"] = hits[0][2] if hits else ""

    if not hits:
        res["verdict"] = MISSING
        res["note"] = f"qB 里查不到本项目名下的 B（ih={b_ih[:12]}…, name={info_name!r}）⇒ 做种上报已断"
        return res
    if len(hits) > 1:
        res["verdict"] = AMBIGUOUS
        res["note"] = (f"命中 {len(hits)} 条同 B（{res['found_in']}）⇒ 同位置多客户端/交棒残留"
                       f"（`#43` 的隐患形状）")
        return res

    which, t, how = hits[0]
    prog = _getattr(t, "progress", 0)
    state = _getattr(t, "state", "")
    res["verdict"] = classify_qb_state(prog, state)
    res["note"] = (f"{which} 命中（how={how}）state={state} progress={prog}"
                   + ("" if res["verdict"] == SEEDING else " —— ★ 还没校验完/仍在下载"))
    return res


# ==================== 选行（★ 不手写 SQL 判据）====================

def iter_exists_rows(conn, *, site=None, torrent_id=None, info_hash=None, limit=0):
    """挑出 (site, info_hash, torrent_id, db_name) 待巡检行。

    ★ 判据来自 `site_publish`（不是 `tasks.site_status` —— 那**只**指乌托邦侧，拿去核
      蟹黄堡会串；见 `db.py` 的警告、`verify_hosted_edition.py:50` 同款）。
    ★ 与 `#38`/`verify_hosted_edition` 同口径：`status='EXISTS' AND torrent_id 非空`。
    """
    import db
    sites = [site] if site else getattr(db, "ALL_SITES", [
        db.SITE_MUSOPIA, db.SITE_CRABPT, db.SITE_PTER, db.SITE_XINGWAN,
        db.SITE_HDKYLIN, db.SITE_TANGPT, db.SITE_MUXUEGE
    ])
    out = []
    for st in sites:
        sql = (
            "SELECT sp.site, sp.info_hash, sp.torrent_id, t.torrent_name "
            "  FROM site_publish sp LEFT JOIN tasks t ON t.info_hash = sp.info_hash "
            " WHERE sp.site = ? AND sp.status = 'EXISTS' AND sp.torrent_id IS NOT NULL "
            "   AND sp.torrent_id != ''")
        params = [st]
        if torrent_id:
            sql += " AND sp.torrent_id = ?"
            params.append(str(torrent_id))
        if info_hash:
            sql += " AND sp.info_hash LIKE ?"
            params.append(str(info_hash) + "%")
        sql += " ORDER BY CAST(sp.torrent_id AS INTEGER)"
        for r in conn.execute(sql, params):
            out.append({"site": r[0], "info_hash": r[1],
                        "torrent_id": r[2], "db_name": r[3]})
    if limit:
        out = out[:limit]
    return out


# ==================== 自检（不需要 .env）====================

class _FakeQB:
    """假 qB：只实现 `torrents_info()`（本模块只调这一个）。"""
    def __init__(self, torrents):
        self._t = list(torrents)

    def torrents_info(self, **kw):
        return list(self._t)


class _FakeT:
    def __init__(self, name, category="", progress=1.0, state="stalledUP", ih=None):
        self.name = name
        self.category = category
        self.progress = progress
        self.state = state
        self.hash = ih if ih is not None else _ih_of_name(name)
        self.infohash_v1 = self.hash


def _bencode(obj):
    """最小 bencode 编码器（只为造一份含 `info.name` 的合法 .torrent，避免依赖 torf 的校验）。"""
    if isinstance(obj, int):
        return b"i" + str(obj).encode() + b"e"
    if isinstance(obj, bytes):
        return str(len(obj)).encode() + b":" + obj
    if isinstance(obj, str):
        return _bencode(obj.encode())
    if isinstance(obj, list):
        return b"l" + b"".join(_bencode(x) for x in obj) + b"e"
    if isinstance(obj, dict):
        out = b"d"
        for k in sorted(obj.keys()):
            out += _bencode(k) + _bencode(obj[k])
        return out + b"e"
    raise TypeError(f"不能 bencode: {type(obj)}")


def _mk_torrent_bytes(info_name):
    """造一份最小合法 .torrent（含 `info.name` + 必需的 `piece length`/`pieces`/`length`）。

    ★ 手写 bencode：`torf.Torrent(name=...)` 默认 `piece length` 是 `None` ⇒ `write_stream`
      会因校验失败（实测）⇒ 自己拼一份，保证 `torf.Torrent.read_stream` 能读出 `info.name`。
    ★ `pieces` 由 `info_name` 派生 ⇒ **不同 info_name 得不同 infohash**（自检要按 hash 认种，
      若所有假种同 hash 就分不开了）。
    """
    import hashlib
    salt = hashlib.sha1(info_name.encode("utf-8")).digest()[:20]
    info = {
        "name": info_name,
        "piece length": 16384,
        "pieces": salt,                   # 由名字派生 ⇒ 不同名字 ⇒ 不同 infohash
        "length": 1 + (len(info_name) % 7),   # 也带上名字的长度，进一步区分
    }
    return _bencode({"info": info, "announce": "http://fake/announce"})


def _ih_of_name(info_name):
    """假 qB 一条种的 `hash` —— 与 `_mk_torrent_bytes(info_name)` 的 infohash **一致**。

    ★ 走**同一份** `_mk_torrent_bytes` + `torf` ⇒ 自检里"B 的 hash"与"qB 里的 hash"
      天然对齐，不需要手抄常量（手抄常量 = 又一个会漂的第二事实源）。
    """
    import io as _io
    import torf
    return str(torf.Torrent.read_stream(_io.BytesIO(_mk_torrent_bytes(info_name))).infohash).lower()


def _selftest():
    try:                                        # ★ 本机 Windows 控制台是 GBK ⇒ 强制 UTF-8
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:                           # noqa: BLE001
        pass
    fails = []

    def chk(label, got, want):
        ok = got == want
        print(f"  {'✅' if ok else '❌'} {label}  got={got!r} want={want!r}")
        if not ok:
            fails.append(label)

    # ── 判定器单测 ────────────────────────────────────────────────
    chk("classify 校验完+做种 ⇒ SEEDING", classify_qb_state(1.0, "stalledUP"), SEEDING)
    chk("classify 校验中 ⇒ SEEDING", classify_qb_state(1.0, "checkingUP"), SEEDING)
    chk("classify 未校验完 ⇒ PRESENT", classify_qb_state(0.3, "downloading"), PRESENT)
    chk("classify progress=1 但暂停 ⇒ PRESENT", classify_qb_state(1.0, "pausedUP"), PRESENT)

    raw = _mk_torrent_bytes("Album X")
    row = {"site": "musopia", "info_hash": "a" * 40, "torrent_id": "123", "db_name": "Album X"}

    # ── ① hash 命中（自分类）⇒ SEEDING ───────────────────────────
    qb = _FakeQB([_FakeT("Album X", category="Musopia")])   # hash 自动 = B 的 hash
    r = patrol_row(row, download=lambda _tid: raw, qb_3020=qb,
                   own_categories={"Musopia", "CrabPT"})
    chk("① hash 命中 ⇒ SEEDING", r["verdict"], SEEDING)
    chk("① 命中在 3020", r["found_in"], ["3020"])
    chk("① how=hash+自分类", r["how"], "hash+自分类")

    # ── ② hash 命中、无分类 ⇒ 仍 SEEDING（hash 才是身份，分类只是旁证）──
    qb2 = _FakeQB([_FakeT("Album X", category="")])
    r = patrol_row(row, download=lambda _tid: raw, qb_3020=qb2,
                   own_categories={"Musopia", "CrabPT"})
    chk("② 无分类、hash 命中 ⇒ SEEDING", r["verdict"], SEEDING)
    chk("② how=hash", r["how"], "hash")

    # ── ③ ★★ 核心阴性对照：**别条的种**（同分类、甚至同名）**不许**算命中 ──
    #    —— 复现 `#39` 初版 bug：3020 有 648 条本项目别的专辑，若按分类/名字判会全假命中。
    qb3 = _FakeQB([
        _FakeT("Album X", category="Musopia", ih="0" * 40),      # 同名、同分类、**别的 hash**
        _FakeT("别的种", category="CrabPT", ih="1" * 40),
    ])
    r = patrol_row(row, download=lambda _tid: raw, qb_3020=qb3,
                   own_categories={"Musopia", "CrabPT"})
    chk("③ 同分类/同名但 hash 不同 ⇒ MISSING（★根治初版假阳性）", r["verdict"], MISSING)
    chk("③ matches=0", r["matches"], 0)

    # ── ④ 同一 hash 命中两条 ⇒ AMBIGUOUS ────────────────────────
    same = _ih_of_name("Album X")
    qb4 = _FakeQB([_FakeT("Album X", ih=same), _FakeT("Album X", ih=same)])
    r = patrol_row(row, download=lambda _tid: raw, qb_3020=qb4,
                   own_categories={"Musopia", "CrabPT"})
    chk("④ 同 hash 两条 ⇒ AMBIGUOUS", r["verdict"], AMBIGUOUS)
    chk("④ matches=2", r["matches"], 2)

    # ── ⑤ 命中但没校验完 ⇒ PRESENT ───────────────────────────────
    qb5 = _FakeQB([_FakeT("Album X", progress=0.4, state="downloading")])
    r = patrol_row(row, download=lambda _tid: raw, qb_3020=qb5,
                   own_categories={"Musopia", "CrabPT"})
    chk("⑤ 未校验完 ⇒ PRESENT", r["verdict"], PRESENT)

    # ── ⑥ 没有 id ⇒ NO_SITE_TORRENT（不是 MISSING！）─────────────
    r = patrol_row({**row, "torrent_id": ""}, download=lambda _t: raw,
                   qb_3020=qb, own_categories={"Musopia"})
    chk("⑥ 缺 id ⇒ NO_SITE_TORRENT", r["verdict"], NO_SITE_TORRENT)

    # ── ⑦ 下载失败 ⇒ NO_SITE_TORRENT（"不知道" ≠ "丢了"）─────────
    r = patrol_row(row, download=lambda _t: None, qb_3020=qb,
                   own_categories={"Musopia"})
    chk("⑦ 下载空 ⇒ NO_SITE_TORRENT", r["verdict"], NO_SITE_TORRENT)

    # ── ⑧ ★ 阴性对照：B 的 hash 命中即 SEEDING（哪怕 DB 的 A 完全是另一个 hash）──
    qbB = _FakeQB([_FakeT("Album X")])
    rB = patrol_row({**row, "info_hash": "b" * 40}, download=lambda _t: raw,
                    qb_3020=qbB, own_categories={"Musopia"})
    chk("⑧ 按 B 的 hash 判 ⇒ SEEDING（不因 A 而假报）", rB["verdict"], SEEDING)

    # ── ⑨ 两个 qB 各命中一条 ⇒ AMBIGUOUS（跨实例残留）────────────
    r = patrol_row(row, download=lambda _t: raw,
                   qb_3020=_FakeQB([_FakeT("Album X")]),
                   qb_3040=_FakeQB([_FakeT("Album X")]),
                   own_categories={"Musopia"})
    chk("⑨ 3020+3040 各一条 ⇒ AMBIGUOUS", r["verdict"], AMBIGUOUS)
    chk("⑨ found_in 两站", r["found_in"], ["3020", "3010"])

    # ── ⑩ 旁证：hash 命中但分类是"外分类" ⇒ how 标出来（提示分类被人改过）──
    qb10 = _FakeQB([_FakeT("Album X", category="SomeOther")])
    r = patrol_row(row, download=lambda _tid: raw, qb_3020=qb10,
                   own_categories={"Musopia", "CrabPT"})
    chk("⑩ 外分类 hash 命中 ⇒ 仍 SEEDING", r["verdict"], SEEDING)
    chk("⑩ how=hash+外分类", r["how"], "hash+外分类")

    print()
    if fails:
        print(f"❌ 自检失败 {len(fails)} 项：{fails}")
        return 1
    print("✅ site_seed_patrol 自检全绿")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    sys.exit("用法：python site_seed_patrol.py --selftest（本模块是库；CLI 见 tools/check_site_seeding.py）")
