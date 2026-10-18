"""
★★ 发种交棒（#92 新流程，2026-10-07）—— 复制成品 HDD→SSD / 判据 / 交棒 3040→3020 / 清 SSD。

用户 2026-10-07 定稿的流程（逐字意图）：
    ① 发种前：把成品从 HDD(`/volume1/video/music/<专辑>`，= 3020 的 `/downloads`)
       **复制**到 **3040 看得见的 SSD 发种区**（`<PUBLISH_CONTAINER_DIR>/<专辑>`，
       宿主 = `/volume2/docker_ssd/qbit-publish/Download/<专辑>`）。
    ② 3040(publish，SSD) 用**站点 .torrent** 加种 + 校验 + 发种 + 做种。
    ③ 满足「**出种≥N** 或 **做种≥D 天**」⇒ 把**种子任务**从 3040 交棒到 3020。
    ④ 清盘：删 3040 任务 + 删 SSD 副本。
    ★★ HDD 原数据**全程不动** —— 3020 的 `/downloads` 就是 `/volume1/video/music`，
      所以交棒是**零拷贝的任务迁移**（export .torrent → add 3020 → recheck），不搬数据。

★★★ 落点订正（2026-10-07 #133/#92-f1，**这是修 bug**）：
    原先复制目标是 `/publish`（= **musopia 容器自己**的挂载点，宿主 `opencd2others/publish_data`），
    但 **3040 的 `/downloads` 是另一个宿主目录** ⇒ 复制到 `/publish` 的东西 **3040 根本看不见**
    （三条种补救时不得不手工 cp 就是此故）。现在 `PUBLISH_CONTAINER_DIR` 钩到
    **3040 的 Download 目录**上；3040 加种时 `save_path` 给**父目录**（`/downloads`），
    数据在 `/downloads/<专辑>/`（父目录语义实测，见 NAS-92 §2）。

★★★ 隔离铁律（用户 2026-10-07：「**仅限本项目的音乐种子**才移动到 3040 交接做种，
    这个 3020qb 我可能还要发其他种子的」）：
    · 本流程**只能**作用于"本项目音乐种" —— 判据 = **3040 上的分类命中白名单**
      `config.HANDOFF_OWN_CATEGORIES`（默认 `{Musopia, CrabPT}`）。
    · 三道闸门：入队前 / 交棒前 / 清盘前各过一遍（`is_own_project`）。
    · 3040 上**用户手加的、或分类不在白名单的**种 ⇒ **绝不**交棒、**绝不**清盘。
    · 交棒到 3020 时用的 `category` **只能**取白名单之一（便于日后"只清本项目"），
      绝不占用 3020 用户手发种用的分类。

★★★ 本模块的设计纪律（照抄 `readd_queue.py`，别发明自己的方言）：
    · **写/读分离但同一份实现**：本项目的人/脚本写队列、daemon 线程读队列，
      两边 import 同一份函数 ⇒ 不会"两处各写一份判据"。
    · **状态落盘**（`/data/handoff_queue.json`），不靠内存 —— 容器重启后还在。
    · **原子写**：`.tmp` + `os.replace`。
    · **幂等键 = infohash**：同一条种不会重复入队。

★★★ 安全铁律（`rm` 的边界，违反 = 灾难）：
    · 成品只能落在 `PUBLISH_CONTAINER_DIR`（= **3040 的下载根**）**下面**；本模块所有 `rm`
      都**只删 `PUBLISH_CONTAINER_DIR/<专辑>` 这一个具体子目录**。
    · **绝不** `rm -rf` 本目录本身（它现在**就是 3040 的 `/downloads`**，删它 = 清空 3040）。
      见 `_safe_rmtree()` 的多重护栏。
"""

import base64
import io
import json
import os
import shutil
import time

import config

QUEUE_FILE = os.path.join(config.DATA_DIR, "handoff_queue.json")
QUEUE_VERSION = 1


# ==================== 队列（结构照抄 readd_queue.py）====================

def _now():
    return time.time()


def load_queue():
    """读队列；任何异常都回一个**空队列**（绝不 raise —— 调用方是 daemon 线程）。"""
    try:
        with open(QUEUE_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {"version": QUEUE_VERSION, "items": []}
    except Exception as e:                                        # noqa: BLE001
        print(f"⚠️  [handoff] 读队列失败（按空处理）: {e}")
        return {"version": QUEUE_VERSION, "items": []}
    if not isinstance(data, dict) or data.get("version") != QUEUE_VERSION:
        print(f"⚠️  [handoff] 队列版本不匹配（按空处理）: "
              f"{data.get('version') if isinstance(data, dict) else type(data).__name__}")
        return {"version": QUEUE_VERSION, "items": []}
    data.setdefault("items", [])
    return data


def save_queue(q):
    """原子写队列。**绝不 raise**（返回 True/False）。"""
    tmp = QUEUE_FILE + ".tmp"
    try:
        os.makedirs(os.path.dirname(QUEUE_FILE), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(q, f, indent=2, ensure_ascii=False)
        os.replace(tmp, QUEUE_FILE)
        return True
    except OSError as e:
        print(f"⚠️  [handoff] 写队列失败: {e}")
        return False


# ==================== ★★★ "漏做种"账本（让"复制没跑"不再静默）====================

MISSING_LOG = os.path.join(config.DATA_DIR, "handoff_missing_data.jsonl")


def record_missing_data(info_hash, album, site, reason):
    """★★★ 记一条「已发布但发种区没有数据 ⇒ 这条不会做种」到**追加式账本**。

    ★★ 为什么要有（2026-10-07 实测踩到，5 条种静默变下载）：发布时若**复制没跑成**
      （拿不到 3020 源 / 复制抛异常 / 复制后核对不过），`publish.py` 会 `return` 跳过
      回加 —— 这是**对的**（避免变下载），但**当时只有一行 print**。一旦 daemon 日志被
      轮转/重启冲掉，"这条漏了"就**再也查不到**（本轮就是：16:00–18:14 的 5 条，
      日志全没了、只能靠人肉比对 3040 与发种区才发现）。⇒ **失败必须落到盘上**。
    ★ 形态：**JSONL 追加**（一行一条），不覆盖、不解析整文件 ⇒ 并发/异常都安全，
      也**绝不 raise**（发布主流程不能被它带崩）。
    ★ 用途：事后用一行命令 `grep handoff_missing_data.jsonl` 就能列全"漏做种的种"，
      据此补复制 + `recheck`（见 README「漏做种补救」）。
    """
    rec = {"at": _now(), "infohash": str(info_hash or ""), "album": str(album or ""),
           "site": str(site or ""), "reason": str(reason or "")}
    try:
        os.makedirs(os.path.dirname(MISSING_LOG), exist_ok=True)
        with open(MISSING_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as e:                                          # noqa: BLE001
        # ★ 账本写不了**也不能崩发布流程**；但要说出来（同 `save_queue` 的纪律）。
        print(f"⚠️  [handoff] 写漏做种账本失败（本条仍会跳过回加）: {e}")


def enqueue(info_hash, name, save_path, target_save_path, note="", state="PENDING",
            category="", target_category="", src_path="", album=""):
    """把一条待交棒的种写进队列。**幂等**（同 infohash 已在 ⇒ 直接返回 True）。

    ★ 本步**不复制数据** —— 复制是发种前的事（见 `copy_to_publish_area`），
      交棒本身是零拷贝。这里只记"这条种到了 3020 该落在哪个 save_path"。
    ★★ `category` = 该种在 **3040** 上的分类（隔离判据的来源，见 `is_own_project`）；
      `target_category` = 交棒到 **3020** 后打哪个分类（★ 只能是白名单之一）。
    ★ `src_path`/`album` = 成品 HDD 源与专辑名；交棒前用它复核"SSD 副本还在不在"
      （`verify_publish_area_complete`，#134）。★ 加字段是**兼容变更**，不 bump 版本。
    """
    item = {
        "infohash": info_hash,
        "name": name,
        "save_path": save_path,                 # 3040 上的路径（发种区）
        "target_save_path": target_save_path,  # 3020 上的路径（HDD 原文件）
        "category": category,                   # ★ 3040 上的分类（隔离判据）
        "target_category": target_category,     # ★ 3020 上要打的分类（白名单之一）
        "src_path": src_path,                   # ★ HDD 源（复核 SSD 副本用）
        "album": album,                         # ★ 专辑名（复核 SSD 副本用）
        "note": note,
        "state": state,                         # PENDING / PUBLISHING / ROUTING /
                                                # HANDOFF_ADDED / HANDOFF_RECHECK /
                                                # HANDOFF_SEEDING / DONE / FAILED
        "enqueued_at": _now(),
        "attempts": 0,
        "first_failed_at": None,
        "last_error": "",
    }
    q = load_queue()
    if info_hash and any(it.get("infohash") == info_hash for it in q["items"]):
        print(f"ℹ️  [handoff] 已在队列（幂等跳过）: {info_hash[:12]}…")
        return True
    q["items"].append(item)
    ok = save_queue(q)
    if ok:
        print(f"➕ [handoff] 入队: {info_hash[:12]}… {name} → {target_save_path}"
              f"（3040分类={category or '∅'}）")
    return ok


def update_item(info_hash, **fields):
    """就地改一条的字段（用于状态机推进）。找不到 ⇒ 返回 False。"""
    q = load_queue()
    for it in q["items"]:
        if it.get("infohash") == info_hash:
            it.update(fields)
            return save_queue(q)
    return False


def remove_item(info_hash):
    """把一条从队列移除（交棒完成/放弃后）。"""
    q = load_queue()
    keep = [it for it in q["items"] if it.get("infohash") != info_hash]
    q["items"] = keep
    return save_queue(q)


def items_by_state(*states):
    """取指定状态的所有条目（无参 = 全部）。"""
    q = load_queue()
    if not states:
        return list(q["items"])
    want = set(states)
    return [it for it in q["items"] if it.get("state") in want]


# ==================== ★★★ 隔离闸门：只有"本项目音乐种"才可交棒/清盘 ====================

def is_own_project(category, own_categories=None):
    """这条种是不是"本项目管的"？判据 = **分类命中白名单**。

    ★★ 用户 2026-10-07 指令：「**仅限本项目的音乐种子**才移动到 3040 交接做种，
       这个 3020qb 我可能还要发其他种子的」⇒ 本流程**绝不许误碰**非本项目种。
    ★ 判据（用户已拍板）= **3040 上的分类** ∈ `config.HANDOFF_OWN_CATEGORIES`
      （默认 `{Musopia, CrabPT}`）。理由：3040 是**本项目专用的发种实例**，分类即来源。
    ★★ 阴性对照（本函数**必须能"说不"**）：
      · `category=""`（未分类 —— 用户手加的种常见）⇒ **False**。
      · `category="SomeOther"` ⇒ **False**。
      · `category="Musopia"` / `"CrabPT"` ⇒ **True**。
    """
    own = config.HANDOFF_OWN_CATEGORIES if own_categories is None else own_categories
    return str(category or "").strip() in set(own)


# ==================== 复制成品 HDD→SSD（步①）====================

def publish_target_dir(album_name):
    """某专辑在发种区（SSD）的落点。★ 只拼一层子目录，防 `..` 逃逸。

    ★ 落点 = `<PUBLISH_CONTAINER_DIR>/<专辑>`，而 `PUBLISH_CONTAINER_DIR` 现在
      钩到 **3040 的 `/downloads`**（#133 订正）⇒ 复制到这里 3040 就看得见。
    """
    safe = os.path.basename(str(album_name).rstrip("/\\"))
    if not safe or safe in (".", ".."):
        raise ValueError(f"非法专辑名: {album_name!r}")
    return os.path.join(config.PUBLISH_CONTAINER_DIR, safe)


def copy_to_publish_area(src_path, album_name, dry_run=True):
    """把成品 `src_path`(HDD 源) 复制到发种区 `<PUBLISH_CONTAINER_DIR>/<专辑>`。

    ★★ 落点 = **3040 看得见的 SSD 目录**（#133 订正：原先是 `/publish`，3040 看不见）。
      ⇒ 复制完成 = 3040 的 `/downloads/<专辑>/` 里有了数据，加种即可校验（父目录语义）。
    ★ `dry_run=True` ⇒ 只报"要复制什么到哪"，**不落盘**。
    ★★★ 判"已复制完成"用 **逐字节核对**（`verify_publish_area_complete`：文件数+总字节），
      **绝不**用"目录非空"—— 那条判据把 3040 的 qB **自己写进来的半截数据**当成了
      "已复制好"⇒ 跳过复制 ⇒ 变下载（2026-10-07 `Beyond` 实测，`ERR-PUBLISH-06`）。
    ★ 已完整 ⇒ 秒回（只 walk 列名+大小，不读文件内容），幂等不重复拷；
      不完整/缺失 ⇒ `copytree(dirs_exist_ok=True)` **补齐/覆盖**。
    ★★ 只用 `copytree`/`copyfile` **单向** 复制；**绝不**反过来动 `src_path`（HDD 原文件）。
    """
    dst = publish_target_dir(album_name)
    if not os.path.isdir(src_path):
        raise FileNotFoundError(f"源成品不存在: {src_path}")
    if dry_run:
        print(f"🅿️  [handoff][dry-run] 将复制 {src_path} → {dst}")
        return dst
    # ★ 完成判据 = 逐字节核对（不是"非空"）。已完整 ⇒ 秒回；否则补齐后核对由调用方裁决。
    done, _why = verify_publish_area_complete(src_path, album_name)
    if done:
        print(f"ℹ️  [handoff] 发种区已完整（逐字节核对通过，跳过复制）: {dst}")
        return dst
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.isdir(src_path):
        shutil.copytree(src_path, dst, dirs_exist_ok=True)
    else:
        os.makedirs(dst, exist_ok=True)
        shutil.copy2(src_path, os.path.join(dst, os.path.basename(src_path)))
    print(f"✅ [handoff] 已复制到发种区: {dst}")
    return dst


def _dir_total_bytes(path):
    """递归求目录（或单文件）的字节总数；出错/不存在 ⇒ 返回 -1（当作"算不出来"）。"""
    try:
        if os.path.isfile(path):
            return os.path.getsize(path)
        if not os.path.isdir(path):
            return -1
        total = 0
        for root, _dirs, files in os.walk(path):
            for fn in files:
                try:
                    total += os.path.getsize(os.path.join(root, fn))
                except OSError:
                    return -1
        return total
    except OSError:
        return -1


# ==================== 逐片校验（`S6` / `ERR-PUBLISH-23`）====================
#
# ★★ 为什么"文件数 + 总字节数"不够（`ERR-PUBLISH-23`，`21811` 实测）：
#   那条种在站点上 44 个文件、盘上 44 个文件、**逐文件字节数一个不差**、
#   `du -sb` 两边同为 `954748123` —— 按"文件数+字节数"判据**完全就位**。
#   但按 torrent 自己声明的顺序逐片校验：**ok=49 / bad=407**，qB 永远 `progress<1`。
#   ⇒ **字节数相同是必要条件，不是充分条件**：同名同长、内容可以完全不同；
#     更隐蔽的是**拼接顺序**不同 —— 每个文件都对，拼起来的分片全错位。
# ⇒ 唯一的强判据是**逐片比 hash**（片长由 torrent 自己声明，别假设）。
#   ★ 这一步是纯本地读盘，但要读**全部字节** ⇒ 只在"文件数+字节数已过"之后做，
#     即"已经很像对了"的时候才花这个钱（顺序不能反：先用便宜判据筛，再用贵的确认）。


def _file_piece_hashes(path, piece_length, *, _chunk=1 << 20):
    """按 1 MiB 读这个文件、产出它与前文**同一个跨文件哈希流**的片段。

    ★ 为什么要做成**生成器**而不是"返回 sha1"：bittorrent 的分片是**跨文件连续**的
      （一片可以横跨两个文件）。⇒ 必须让调用方按 torrent 声明的**文件顺序**把
      所有字节串进同一个 hasher，中途只在"凑满一片"时切一刀。
      ⇒ 本函数只负责吐字节块，**切片的时机归调用方**（顺序错在调用方，不该在这里藏）。
    ★ 读不出 ⇒ 抛 `OSError`（调用方转成"校验不了"，绝不静默当通过）。
    """
    with open(path, "rb") as fh:
        while True:
            b = fh.read(_chunk)
            if not b:
                return
            yield b


def verify_pieces(src_path, torrent_bytes, *, max_pieces=None):
    """按 `torrent_bytes` 自己声明的**文件顺序 + 片长**逐片比对 `src_path` 的字节。返回 `(ok, 说明)`。

    ★★ 这是 `ERR-PUBLISH-23` 落地的那条强判据：**文件集合/字节数相同推不出分片相同**。
      ⇒ 判据 MUST 是「按 torrent 的顺序把字节串起来，逐片算 sha1，与 torrent 里的
        `pieces` 逐片比对」，全对才算"这个目录能做出这个种"。

    ★★★ 路径拼接的两个坑（同一个坑踩了三次，`ERR-PUBLISH-23`）：
      ① `torf` 的 `str(f)` **已含顶层目录名**；`metainfo['info']['files'][i]['path']`
         **不含**。本函数用 `info['files']` 的 `path` 列表 ⇒ 需要 `join(root, name, *path)`。
      ② 单文件种没有 `info['files']`，路径就是 `join(root, info['name'])`。
      ⇒ 两种形状分开处理，**不许**用"试着找一下"的模糊逻辑（那会把"找不到"变成"猜对了"）。

    :param src_path: **发种区的根** —— 即 `join(PUBLISH_CONTAINER_DIR, album_name)`
        （★ 不是 HDD 源；源的路径形状可能完全不同，本函数只验"发种区这份能不能做出这个种"）。
    :param torrent_bytes: `.torrent` 的原始字节（需要 `pieces`/`piece length`/`name`/`files`）。
    :param max_pieces: 只验前 N 片（`None` = 全验）。★ 调试/大种时用来限时；
        **生产必须传 `None`**（验一半的判据不是判据）。
    :return: `(True, "…")` 全部对上；`(False, "…")` 有错/读不出 —— 说明里带**第一个错片的下标**。
    """
    import hashlib

    import torf
    try:
        t = torf.Torrent.read_stream(io.BytesIO(torrent_bytes))
    except Exception as e:                                   # noqa: BLE001
        return False, f"解析种子失败：{type(e).__name__}: {e}"
    info = t.metainfo.get("info") or {}
    try:
        piece_length = int(info["piece length"])
        pieces = info["pieces"]
        name = info["name"]
    except (KeyError, TypeError, ValueError) as e:
        return False, f"种子里缺必要字段：{e}"
    if piece_length <= 0:
        return False, f"片长为非正数：{piece_length}"

    # ---- 按 torrent 自己声明的顺序，列出 (绝对路径, 字节数) ----
    files = info.get("files")
    entries = []
    if files:
        for i, f in enumerate(files):
            parts = f.get("path") or []
            length = f.get("length")
            if length is None:
                # ★ BEP47 padding 文件（`attr: p`）没有 length？实际有；这里只挡住畸形。
                return False, f"文件表第 {i} 项没有 length ⇒ 无法校验"
            entries.append((os.path.join(src_path, name, *parts), int(length)))
    else:
        entries.append((os.path.join(src_path, name), int(info.get("length") or 0)))

    # ---- 先把"文件在不在、长度对不对"过一遍（便宜判据先走）----
    for abspath, length in entries:
        if not os.path.isfile(abspath):
            return False, f"发种区缺文件：{abspath}"
        try:
            got = os.path.getsize(abspath)
        except OSError as e:
            return False, f"读不到文件大小（{abspath}）：{e}"
        if got != length:
            return False, (f"文件长度不符：{abspath}（种 {length} / 盘 {got}）"
                           f" ⇒ 拼接起来必然错片")

    # ---- 逐片比对 ----
    want = len(pieces) // 20
    if max_pieces is not None:
        want = min(want, max(0, int(max_pieces)))
    buf = bytearray()
    idx = 0
    h = hashlib.sha1()
    for abspath, _length in entries:
        try:
            for chunk in _file_piece_hashes(abspath, piece_length):
                pos = 0
                while pos < len(chunk):
                    need = piece_length - len(buf)
                    take = chunk[pos:pos + need]
                    buf += take
                    h.update(take)
                    pos += len(take)
                    if len(buf) == piece_length:
                        if h.digest() != pieces[idx * 20:(idx + 1) * 20]:
                            return False, (f"第 {idx} 片对不上（torrent 声明片长 {piece_length}）"
                                           f" ⇒ 这个目录做不出这个种")
                        idx += 1
                        h = hashlib.sha1()
                        buf = bytearray()
                        if idx >= want:
                            label = (f"前 {idx} 片" if max_pieces is not None else f"{idx} 片")
                            return True, f"逐片校验通过（{label}）"
        except OSError as e:
            return False, f"读文件失败（{abspath}）：{e}"
    # 尾部不满一片的情况：bittorrent 也把它算作最后一片
    if buf:
        if idx >= want:
            return True, f"逐片校验通过（{idx} 片，尾部 {len(buf)} 字节未验）"
        if h.digest() != pieces[idx * 20:(idx + 1) * 20]:
            return False, f"末片（第 {idx} 片，{len(buf)} 字节）对不上 ⇒ 这个目录做不出这个种"
        idx += 1
    if want > 0 and idx < want:
        return False, f"只读到 {idx} 片，种子声明 {want} 片 ⇒ 数据不完整"
    return True, f"逐片校验通过（{idx} 片）"



def verify_publish_area_complete(src_path, album_name, *, torrent_file=None):
    """★★★ 判据：`src_path`(HDD 源) 是否**完整**已复制到发种区 `<PUBLISH_CONTAINER_DIR>/<专辑>`。

    ★ 两级判据，**便宜的先走、贵的后走**（顺序不能反）：
      · **一级（必要条件）**：文件数相同 且 总字节相同 —— 挡"只拷了头几个文件"。
      · **二级（强判据，`S6` 新增）**：给了 `torrent_file` 就再**逐片比 hash**
        —— 挡"字节数全对但内容/拼接顺序不同"（`ERR-PUBLISH-23`：`21811` 44 文件、
        逐文件字节数一个不差、`du -sb` 完全一致，逐片却是 **ok=49 / bad=407**）。
      ★★ 为什么二级是**可选**（`torrent_file=None` 时只有一级）：本函数被 `begin_handoff_for_task`
        在**复制刚结束时**调用，那一刻 `.torrent` 未必已生成（`publish.py` 里制种与交棒顺序
        不保证）⇒ 强制要求会把整条交棒链打挂（`ERR-ENV-02` 的味道：新判据不许改变旧调用方的行为）。
        ⇒ 调用方**有**种子时必须传；没传就在说明里留一句"只做了一级"，免得读日志的人
        以为"已逐片验过"（`ERR-META-01`：判据覆盖面要写清楚）。

    ★★ 为什么必须有这一步（2026-10-07 #134 实测踩到，**这就是那两个种的病根**）：
      原先 `begin_handoff_for_task` 复制完就 `return True`，**从不核对**复制结果；
      调用方（`publish.py`）又**丢弃**了它的返回值 ⇒ 复制**失败/半截**时照样把任务
      推进 3040 ⇒ qB 去 `<save_path>/<album>` 找不到文件 ⇒ **progress=0 变下载**
      （实测：`☆ac8.13f` 在 SSD 发种区**根本没有目录**，3040 上却是 `stalledDL`、10 leechers）。
    ★ 返回 `(是否完整, 说明)`。`src_path` 不存在 ⇒ `(False, …)`（**绝不当成"没得比"而放行**）。
    """
    if not os.path.exists(src_path):
        return False, f"源成品不存在: {src_path}"
    if not os.path.isdir(src_path):
        # 单文件源：比文件字节
        dst_file = os.path.join(publish_target_dir(album_name), os.path.basename(src_path))
        src_sz, dst_sz = _dir_total_bytes(src_path), _dir_total_bytes(dst_file)
        if not (src_sz >= 0 and src_sz == dst_sz):
            return False, f"单文件未就位（源 {src_sz} / 发种区 {dst_sz} 字节）"
        _ok, _why = True, f"单文件已就位（{src_sz} 字节）"
        _publish_root = publish_target_dir(album_name)
    else:
        dst = publish_target_dir(album_name)
        if not os.path.isdir(dst):
            return False, f"发种区目录不存在: {dst}"
        src_bytes, dst_bytes = _dir_total_bytes(src_path), _dir_total_bytes(dst)
        if src_bytes < 0 or dst_bytes < 0:
            return False, f"无法统计字节（源 {src_bytes} / 发种区 {dst_bytes}）"
        if src_bytes != dst_bytes:
            return False, (f"字节不符（源 {src_bytes} / 发种区 {dst_bytes}，"
                           f"差 {src_bytes - dst_bytes}）⇒ 复制未完成")
        _ok, _why = True, f"已逐字节就位（{src_bytes} 字节）"
        _publish_root = dst

    # ---- 二级：逐片校验（给了 .torrent 才做）----
    if not torrent_file:
        return _ok, _why + "；★ 未做逐片校验（调用方没给 .torrent）"
    try:
        with open(torrent_file, "rb") as fh:
            tb = fh.read()
    except OSError as e:
        # ★ 种子读不出 ⇒ **不**放行二级、但也**不**把一级的绿色推翻：
        #   如实报"二级没做成"，让调用方/看日志的人知道覆盖面。
        return _ok, _why + f"；★ 逐片校验**未做**（读 .torrent 失败：{e}）"
    # ★★ 根目录的口径：`verify_pieces` 自己会把 `info['name']`（= 专辑目录名）拼进去，
    #   ⇒ 传它的**父层**。传 `_publish_root` 本身会变成 `<专辑>/<专辑>/…`（实测踩到）。
    p_ok, p_why = verify_pieces(os.path.dirname(_publish_root), tb)
    if not p_ok:
        return False, f"{_why}，但**逐片校验不过**：{p_why}"
    return True, f"{_why}，且 {p_why}"



def begin_handoff_for_task(src_path, album_name, info_hash, task=None,
                           source_category="", target_category="",
                           dry_run=None):
    """★ 发种流程的**接线点**：复制成品到 3040 发种区 + 入队（供之后交棒）。

    这一步把用户要的「数据复制要由本项目代码自动化完成」接进发布流：
        ① 复制 `src_path`(HDD 原文件) → `<PUBLISH_CONTAINER_DIR>/<专辑>`（3040 看得见）；
        ② **★ 核对复制确实完整**（`verify_publish_area_complete`，逐字节）；
        ③ 把这条种写进交棒队列，记下 3040 分类 / 3020 目标分类 / 目标 save_path。

    ★★ 只在 `ENABLE_HANDOFF=true` 时做（**默认关** ⇒ 老行为一字不改，ERR-ENV-02）。
    ★★ 幂等：已在队列（同 infohash）⇒ 直接返回（不重复复制/入队）。
    ★ `task` 是 publish 侧的 task dict（可选，用于备注名）。
    ★★★ 返回 `(成功, 说明)`。**成功 = 数据确已就位**（复制完成 + 逐字节核对通过）。
      ⇒ 调用方（`publish.py`）**必须**据返回值决定要不要回加：
        `False` ⇒ **绝不能**把任务推进 3040（否则必变下载，见 `verify_publish_area_complete`）。
      ★ 原先本函数"复制没抛异常就返回 True"、调用方又丢弃返回值 ⇒ 半截/失败也照推
        （#134 实测的病根）。
    ★ `dry_run=True` ⇒ 只报"要复制什么到哪"，**不落盘也不核对**（返回 True，别在演练里误拦）。
    ★ **绝不 raise**（发布主流程不能被它带崩）。
    """
    if str(config.ENABLE_HANDOFF).strip().lower() not in ("1", "true", "yes", "y", "on"):
        return False, "ENABLE_HANDOFF 未开（跳过复制+入队）"
    dry = (config.HANDOFF_DRY_RUN == "true") if dry_run is None else dry_run
    try:
        ssd_dir = copy_to_publish_area(src_path, album_name, dry_run=dry)
    except Exception as e:                                        # noqa: BLE001
        return False, f"复制到发种区失败: {type(e).__name__}: {e}"
    if dry:
        # ★ dry-run 不落盘 ⇒ 无可核对；如实说明，返回 True 以免演练里假拦（闸门才是有意义的演练对象）。
        return True, f"[dry-run] 将复制 {src_path} → {ssd_dir} 并入队（未核对）"
    # ★★ 关键闸门：数据没逐字节就位 ⇒ 不返回成功（调用方据此**拒绝回加**）。
    ok_copy, why_copy = verify_publish_area_complete(src_path, album_name)
    if not ok_copy:
        return False, f"发种区数据未就位，拒绝回加: {why_copy}"
    # ★ 目标 save_path = 3020 的父目录（HDD 原文件所在）= config.HANDOFF_TARGET_SAVE_ROOT
    target_save = config.HANDOFF_TARGET_SAVE_ROOT
    # ★ 目标分类：默认沿用来源分类（乌托邦→Musopia / 蟹黄堡→CrabPT），须在白名单内。
    tcat = target_category or source_category or config.PUBLISH_CATEGORY
    name = (task or {}).get("formatted_title") or album_name
    ok = enqueue(info_hash, name, ssd_dir, target_save,
                 note="auto from publish", category=source_category,
                 target_category=tcat, src_path=src_path, album=album_name)
    return ok, (f"已复制到 {ssd_dir} 并**逐字节核对通过**并入队"
                f"（3040分类={source_category or '∅'}→3020分类={tcat}）")


# ==================== 交棒判据（步③ 的触发）====================

def judge(num_complete, added_on, now=None,
          seeders_min=None, seed_days=None):
    """判断一条种是否够格交棒。返回 `(是否交棒, 原因字符串)`。

    ★★ "出种≥N"的口径（这里最容易被读错，逐字说明）：
      qB 的 `num_complete` = **swarm 里做种者总数**，它**把本机自己算进去**。
      ⇒ 用户要的"出种 3 个以上"（= **别人**也开始做种了）应判
        `num_complete - 1 >= seeders_min`（**减掉自己**）。
      ★ 若直接拿 `num_complete >= 3` 去比，则"**只有自己在做种**"(num_complete=1)
        还不算数，那还好；但"自己 + 2 个"=3 会被当成"出种 3" —— **少数一个**。
        这属于"判据提到了对的词、却问了个差一的问题"（ERR-META 一族）⇒ 明确减 1。

    ★ 阴性对照（本函数必须能"说不"）：
      · 只有自己做种（num_complete=1）、且做种 0 天 ⇒ **(False, …)**，不交棒。
      · 自己 + 2 人（num_complete=3）⇒ 别人=2 < 3 ⇒ **False**（除非天数够）。
      · 自己 + 3 人（num_complete=4）⇒ 别人=3 ⇒ **True**。
    """
    seeders_min = config.HANDOFF_SEEDERS_MIN if seeders_min is None else seeders_min
    seed_days = config.HANDOFF_SEED_DAYS if seed_days is None else seed_days
    now = _now() if now is None else now

    others_seeding = int(num_complete or 0) - 1        # ★ 减掉本机自己
    if others_seeding >= seeders_min:
        return True, f"出种 {others_seeding}≥{seeders_min}（num_complete={num_complete} 含自己）"

    try:
        age_days = (now - float(added_on)) / 86400.0
    except (TypeError, ValueError):
        age_days = 0.0
    if age_days >= seed_days:
        return True, f"做种 {age_days:.2f}≥{seed_days} 天（added_on={added_on}）"

    return False, (f"未达：出种 {others_seeding}<{seeders_min} 且 做种 {age_days:.2f}<{seed_days} 天")


def _torrent_field(t, name, default=None):
    """从 qBittorrentAPI 的 torrent 对象里稳妥取字段（属性或 dict 两种形态）。"""
    if isinstance(t, dict):
        return t.get(name, default)
    return getattr(t, name, default)


def pick_handoff_candidates(torrents, now=None, seeders_min=None, seed_days=None):
    """从 3040 的 `torrents_info()` 结果里挑出够格交棒的。返回 `[(torrent, 原因)]`。

    ★ 纯函数：只读传入的 torrents 对象，不连 qB、不动盘 ⇒ 可离线单测。
    """
    out = []
    for t in torrents:
        # ★ 只认"已做完种（progress=1）且在做种"的；下载中/暂停的不碰。
        progress = _torrent_field(t, "progress", 0) or 0
        if float(progress) < 1.0:
            continue
        ok, why = judge(
            _torrent_field(t, "num_complete", 0),
            _torrent_field(t, "added_on", 0),
            now=now, seeders_min=seeders_min, seed_days=seed_days,
        )
        if ok:
            out.append((t, why))
    return out


def _find_on_3040(qb_src, info_hash, name=None):
    """在 3040 上找一条种，返回 `(torrent|None, how)`。

    ★★★ #45（2026-10-08 实测钉死）：**3040 上挂的是"站点那份 .torrent"(B)，其 infohash
      与 DB 的 `tasks.info_hash`（皇后原种 A）**不同**！原因：站点会重写 announce/passkey
      ⇒ 同一专辑在 3040 的 hash ≠ 队列里记的 hash。
      实测：队列 3 条全 `PENDING` / `3040 上还没有这条种`，而 3040 **明明有**这些专辑
      （`金装小曲` queue=`59810e5b…` vs 3040=`8c36a039…`；`十年经典` `6cb9ffd8…` vs
      `86cd30bc…`；`Massive CircleZ 6` `874b5989…` vs `fa416246…`）⇒ **判据用了错的 hash
      ⇒ 够格种永不交棒**（`已交棒到 3020` 恒 0）。
    ★ 正解：先按 `info_hash` 查；**查不到**再用 **name 精确匹配**（3040 的 `name` 就是
      站点 .torrent 的 `info.name`，= 队列里的 `name`/`album`）⇒ 两种身份都能命中。
      ★ 为什么不下载站点 .torrent 拿 hash：那要连站点、还要 I/O，而**名字就在手上**
        （队列 item 的 `name`），且 3040 的 `torrents_info()` 一次就能拿全表按名索引。
    ★ 返回 `how` ∈ `{"hash","name",""}}`（""=没找到）—— 便于日志区分"按 hash 找到"
      还是"按名字找到"（后者提示身份不一致，值得记一笔）。
    """
    try:
        info = qb_src.torrents_info(torrent_hashes=info_hash)
    except Exception as e:                                        # noqa: BLE001
        raise RuntimeError(f"查 3040 失败: {type(e).__name__}: {e}") from e
    if info:
        return info[0], "hash"
    if name:
        want = str(name).strip()
        for t in qb_src.torrents_info():
            if str(_torrent_field(t, "name", "") or "").strip() == want:
                return t, "name"
    return None, ""


def eligible_on_3040(qb_src, info_hash, name=None, now=None,
                     seeders_min=None, seed_days=None):
    """★★ #136：判"3040 上这条是否**够格交棒**"。返回 `(present, eligible, why)`。

    ★★ 为什么（用户 2026-10-07 拍板）：交棒语义 = **够格才交棒**（出种≥N 或 做种≥D 天），
      **不是**发布后就立即推给 3020。原代码 `process_one` 对 PENDING 条目**立即交棒**，
      而 `judge`/`pick_handoff_candidates` 只在 selftest 里被调 ⇒ "出种≥3 才交棒"从未生效
      （实测：`☆ac8.13f` 20 个别人做种却一直没交棒、整个容器 `已交棒到 3020` 计数=0）。
    ★ 三态（显式区分，同 `ERR-META-14`：不许把"还没轮到"与"够格"混一个词）：
      · `present=False` ⇒ 3040 上**还没这条种**（延迟回加还没到点/失败了）⇒ **等**，不交棒。
      · `present=True, eligible=False` ⇒ 在做种但**还不**够格 ⇒ **等**，不交棒。
      · `present=True, eligible=True` ⇒ 够格 ⇒ 交棒。
    ★★★ #45：`name` 用于"按站点 .torrent 的 name 兜底命中"（hash 用 A/皇后原种、
      3040 挂的是 B/站点种，两者 hash 不同）。见 `_find_on_3040`。
    ★ `qb_src is None` ⇒ 返回 `(False, False, "无 3040 连接")`（dry-run 无客户端时也走这）。
    """
    now = _now() if now is None else now
    if qb_src is None:
        return False, False, "无 3040 连接"
    try:
        t, how = _find_on_3040(qb_src, info_hash, name=name)
    except Exception as e:                                        # noqa: BLE001
        return False, False, str(e)
    if t is None:
        return False, False, "3040 上还没有这条种（延迟回加未到/失败）⇒ 等"
    # ★ 先要 progress≥1（做完种）才有资格谈"出种/做种天数"。
    progress = float(_torrent_field(t, "progress", 0) or 0)
    if progress < 1.0:
        return True, False, f"3040 上还没做完种（progress={progress:.2f}）⇒ 等"
    nc = _torrent_field(t, "num_complete", 0)
    added = _torrent_field(t, "added_on", 0)
    ok, why = judge(nc, added, now=now, seeders_min=seeders_min, seed_days=seed_days)
    if how == "name":
        # 提示身份不一致（值得记一笔）：hash 没找到、按名字才找到。
        why = f"[按名字命中 3040] {why}"
    return True, ok, why


# ==================== 交棒 3040→3020（步③，零拷贝）====================

def export_torrent_bytes(qb, info_hash):
    """从 3040 导出该种的 .torrent 字节（交棒到 3020 用）。"""
    data = qb.torrents_export(torrent_hash=info_hash)
    if isinstance(data, str):
        return data.encode("utf-8")
    return bytes(data)


def handoff_to_3020(qb_src, qb_dst, info_hash, target_save_path, dry_run=True,
                   target_category=None, own_categories=None, name=None):
    """把种的**任务**从 3040 交棒到 3020：export → add(3020, save_path) → recheck。

    ★★ **零拷贝**：不搬数据 —— 3020 的 `/downloads` 就是 `/volume1/video/music`，
      所以只要给对 `save_path`（HDD 原文件所在），3020 自己校验即可。
    ★★★ **隔离闸门（第一道）**：交棒前先查 3040 上该种的 `category`，
      不命中白名单（`is_own_project`）⇒ **拒绝交棒**，返回 `(False, 原因)`。
      ★ 为什么在这里也查一遍：队列可能被外部篡改/残留旧项 ⇒ 不只信入队时的判据。
    ★ `target_category` = 交棒到 3020 后打哪个分类。★ **只能取白名单之一**；
      缺省 = `config.PUBLISH_CATEGORY`。传给非白名单值 ⇒ 拒（防污染 3020 分类体系）。
    ★ `dry_run=True` ⇒ 只打印计划，不 add、不 recheck。
    ★★★ #45：`name` 用于"按站点 .torrent 的 name 兜底命中"（3040 挂 B/站点种，其 hash
      与 `info_hash`(A/皇后原种) **不同**）。**export 与 recheck 必须用 3040 上的真实 hash**
      （`real_hash`），否则 export 用 A ⇒ 导出的是皇后原种（报 open.cd）⇒ 交棒给它 = 交错了种。
    ★ 返回 `(成功, 原因)`。**不删任何东西**（删是下一步 `finalize_source`）。
    """
    # ---- 隔离闸门：先确认"这是本项目种的分类"（查 3040 实况）----
    cat, gate_err = _source_category(qb_src, info_hash, dry_run=dry_run, name=name)
    if gate_err:
        return False, gate_err
    # ★ #45：拿 3040 上的**真实 hash**（可能是 B，与 info_hash=A 不同）。
    real_hash = info_hash
    try:
        _t, _how = _find_on_3040(qb_src, info_hash, name=name)
        if _t is not None:
            real_hash = str(_torrent_field(_t, "hash", "") or info_hash)
    except Exception:                                             # noqa: BLE001
        _how = ""
    if not is_own_project(cat, own_categories):
        return False, (f"拒交棒：3040 分类 {cat!r} 不在本项目白名单 "
                       f"{config.HANDOFF_OWN_CATEGORIES}（疑似非本项目种，不碰）")
    # ---- 目标分类只能是白名单之一（否则污染 3020 的分类体系）----
    tcat = target_category if target_category else (cat or config.PUBLISH_CATEGORY)
    if not is_own_project(tcat, own_categories):
        return False, f"拒交棒：目标分类 {tcat!r} 不在本项目白名单"
    if dry_run:
        print(f"🅿️  [handoff][dry-run] 将交棒 {real_hash[:12]}… → 3020 "
              f"save_path={target_save_path} category={tcat}（3040分类={cat}）")
        return True, "dry-run"
    try:
        content = export_torrent_bytes(qb_src, real_hash)
    except Exception as e:                                        # noqa: BLE001
        return False, f"从 3040 导出 .torrent 失败: {type(e).__name__}: {e}"
    if not content:
        return False, "从 3040 导出 .torrent 为空"
    try:
        # ★ 与 readd 同款：不传 layout/is_skip_checking ⇒ 让 3020 自己校验。
        qb_dst.torrents_add(
            torrent_files=content,
            save_path=target_save_path,
            is_paused=False,
            category=tcat,
        )
        qb_dst.torrents_recheck(torrent_hashes=real_hash)
    except Exception as e:                                        # noqa: BLE001
        name_e = type(e).__name__
        # 409 = 3020 上已有这条种（多半是别的容器辅种抢先加了）⇒ 视为"已在做种"。
        if name_e == "Conflict409Error":
            print(f"ℹ️  [handoff] 3020 已有该种（409）= 视为已交棒: {real_hash[:12]}…")
            return True, "409 已存在"
        return False, f"add 3020 失败: {name_e}: {e}"
    print(f"✅ [handoff] 已交棒到 3020（待 3020 校验）: {real_hash[:12]}… category={tcat}")
    return True, "added+recheck"


def _source_category(qb_src, info_hash, dry_run=False, name=None):
    """查 3040 上该种的 `category`（隔离判据的来源）。返回 `(分类, 错误)`。

    ★ 查不到该种 ⇒ 返回错误（**不当成"没分类"**），因为"查不到"与"未分类"必须分开
      —— 前者说明种不在 3040（还没发/已被删），此时**绝不能**往下走。
    ★★★ #45：`name` 兜底 —— 3040 挂的是站点种(B)，hash 与 `info_hash`(A) 不同 ⇒ 按名字找。
    ★★ `dry_run=True` 时**仍然真查**（NAS-92g 实测修）：本查询是**只读 GET**，dry-run 下
      做了也**不改任何状态**；而闸门 `is_own_project(cat)` 依赖它拿到真分类。
      ★★★ 原先 `dry_run` 短路返回 `("","")` ⇒ 闸门拿空串比白名单 ⇒ **恒判 False**
        ⇒ dry-run 下**每条种都在闸门这一步被"拒交棒"**（日志像"非本项目种"，其实不是）。
        这使 dry-run **失去演练意义**（永远看不到"本会交棒"那一步）。故改为：
        dry-run 也查，只有**真的没有客户端**（`qb_src is None`）时才跳过。
    """
    if qb_src is None:
        return "", ""
    try:
        t, how = _find_on_3040(qb_src, info_hash, name=name)
    except Exception as e:                                        # noqa: BLE001
        return "", f"查 3040 分类失败: {type(e).__name__}: {e}"
    if t is None:
        return "", f"3040 上找不到该种 {info_hash[:12]}…（已删？）⇒ 不交棒"
    return str(_torrent_field(t, "category", "") or ""), ""


def verify_3020_seeding(qb_dst, info_hash, name=None):
    """查 3020 上该种是否已"校验通过且在做种"。返回 `(是否就绪, 明细)`。

    ★ 判据（与 NAS-92 §1.5 一致）：`progress >= 1.0` 且 state ∈
      {stalledUP, uploading, forcedUP, queuedUP}。★ 只在这些都成立时才允许删源。
    ★★ 这是"删 SSD"前的**唯一**安全门 —— 门没通过 ⇒ 绝不进 `finalize_source`。
    ★★★ #45：`name` 兜底 —— 交棒到 3020 的是站点种(B)，其 hash 与 `info_hash`(A) 不同
      ⇒ 按 hash 查不到时，必须按 3020 上的 **name** 再找一次，否则会误判"3020 上还没有"
      ⇒ 永远卡在 HANDOFF_RECHECK（交棒了却永不 DONE）。
    """
    try:
        info = qb_dst.torrents_info(torrent_hashes=info_hash)
    except Exception as e:                                        # noqa: BLE001
        return False, f"查 3020 失败: {type(e).__name__}: {e}"
    if not info and name:
        want = str(name).strip()
        try:
            for t in qb_dst.torrents_info():
                if str(_torrent_field(t, "name", "") or "").strip() == want:
                    info = [t]
                    break
        except Exception:                                         # noqa: BLE001
            pass
    if not info:
        return False, "3020 上还没有这条种"
    t = info[0]
    progress = float(_torrent_field(t, "progress", 0) or 0)
    state = str(_torrent_field(t, "state", "") or "")
    seeding_states = {"stalledUP", "uploading", "forcedUP", "queuedUP", "checkingUP"}
    if progress >= 1.0 and state in seeding_states:
        return True, f"3020 做种中（progress={progress:.2f} state={state}）"
    return False, f"3020 未就绪（progress={progress:.2f} state={state}）"


# ==================== 清 SSD 副本（步④，严格时序）====================

def _safe_rmtree(path):
    """★ 只在 `PUBLISH_CONTAINER_DIR` **之下**删一个具体子目录。

    多重护栏（任何一条不满足 ⇒ 拒删并返回 False）：
      ① path 必须**严格位于** `PUBLISH_CONTAINER_DIR` 之下（不是它本身）；
      ② 规范化后不得等于 publish 根；
      ③ 必须是目录且不是符号链接（防 `rm` 顺着软链删到别处）。
    ★★ 绝不 `rm -rf` 本目录本身 —— 它现在**就是 3040 的 `/downloads`**（#133 订正后），
      删它 = **清空 3040**。护栏① 就是为此。
    """
    root = os.path.realpath(config.PUBLISH_CONTAINER_DIR)
    real = os.path.realpath(path)
    if real == root:
        print(f"❌ [handoff] 拒删：目标是发种区根目录本身 ({real}) —— "
              f"★ 这就是 3040 的 /downloads，删它=清空 3040")
        return False
    if not real.startswith(root + os.sep):
        print(f"❌ [handoff] 拒删：目标不在发种区之下 ({real} ∉ {root})")
        return False
    if os.path.islink(path):
        print(f"❌ [handoff] 拒删：目标是符号链接 ({path})")
        return False
    if not os.path.isdir(path):
        print(f"ℹ️  [handoff] 待删目录不存在（已清理？）: {path}")
        return True
    shutil.rmtree(path)
    print(f"🗑️  [handoff] 已删 SSD 副本: {path}")
    return True


def finalize_source(qb_src, info_hash, ssd_dir, dry_run=True, own_categories=None,
                    name=None):
    """删除 3040 任务 + 删 SSD 副本。**只有在 3020 已确认做种后**才可调用本函数。

    ★★ 严格时序（NAS-92 §1.5）：① 3020 add+recheck 成功 → ② 删 3040 任务
      → ③ 删 SSD 文件。**绝不能"先删 SSD 再交棒"**（3040 做种的文件没了 ⇒ 校验失败）。
      ★ 本函数**假定①已完成**（调用方 `process_one` 用 `verify_3020_seeding` 把门）。
    ★★★ **隔离闸门（第三道）**：删之前**再查一遍** 3040 上该种的分类，
      不命中白名单 ⇒ **拒删**。★ 为什么在删之前还要查：队列可能被外部篡改/残留旧项，
      而删是**唯一不可逆**的一步 ⇒ 宁可多查一次，也不误删用户的种。
    ★ `deleteFiles=False` 删 3040 任务 —— **不**让 3040 顺手删盘；盘由 `_safe_rmtree` 管，
      这样"删任务"和"删盘"是**两个显式动作**，中间有日志可查。
    ★★★ #45：`name` 兜底 —— 3040 上的是站点种(B)，其 hash 与 `info_hash`(A) 不同 ⇒
      **删 3040 任务必须用 3040 上的真实 hash**（否则删不掉 B，留下"已交棒但 3040 还在种"）。
    """
    # ---- 隔离闸门：删之前再确认"这是本项目种的分类"（查 3040 实况）----
    cat, gate_err = _source_category(qb_src, info_hash, dry_run=dry_run, name=name)
    # ★ #45：解析 3040 上的真实 hash（B），删它（而不是 A）。
    del_hash = info_hash
    try:
        _t, _ = _find_on_3040(qb_src, info_hash, name=name)
        if _t is not None:
            del_hash = str(_torrent_field(_t, "hash", "") or info_hash)
    except Exception:                                             # noqa: BLE001
        pass
    if gate_err:
        # ★ 查不到该种：可能是"已经被删了"（幂等）⇒ 只要盘在，仍可安全清盘。
        print(f"⚠️  [handoff] 清盘前查分类失败: {gate_err}")
        # 不直接拒 —— 但若原因不是"找不到"（例如连不上），保守拒删。
        if "找不到该种" not in gate_err:
            return False
    elif not is_own_project(cat, own_categories):
        print(f"❌ [handoff] 拒清盘：3040 分类 {cat!r} 不在本项目白名单 "
              f"{config.HANDOFF_OWN_CATEGORIES}（疑似非本项目种，不碰）")
        return False
    if dry_run:
        print(f"🅿️  [handoff][dry-run] 将删 3040 任务 {del_hash[:12]}… + SSD 副本 {ssd_dir}")
        return True
    try:
        qb_src.torrents_delete(delete_files=False, torrent_hashes=del_hash)
        print(f"✅ [handoff] 已删 3040 任务: {del_hash[:12]}…")
    except Exception as e:                                        # noqa: BLE001
        print(f"⚠️  [handoff] 删 3040 任务失败（继续删盘？不 —— 先停）: "
              f"{type(e).__name__}: {e}")
        return False
    return _safe_rmtree(ssd_dir)


# ==================== 编排一条（daemon 线程用）====================

SEEDING_STATES = {"stalledUP", "uploading", "forcedUP", "queuedUP"}


def process_one(qb_src, qb_dst, item, now=None, dry_run=None):
    """推进队列里**一条**的状态机（PENDING→…→DONE）。返回新的 state 字符串。

    ★ 每步都幂等：3020 已存在(409) ⇒ 视为已交棒；SSD 目录已不在 ⇒ 视为已清。
    ★ 任一步失败 ⇒ 置 FAILED 并记 `last_error`（不删任何东西，下轮可重试）。
    ★★★ #136（2026-10-07 实测 `217848`）：`FAILED` **必须重新走交棒**，**绝不**
      落到 `verify_3020_seeding` + `finalize_source`。原代码的交棒块只认
      `("PENDING", "HANDOFF_ADDED")` ⇒ 一条 `FAILED` 条目会**跳过交棒**、直接
      往下走到清盘：若此时 3020 那边**恰好已有该种在做种**（例如辅种工具抢先加了），
      `verify_3020_seeding` 判就绪 ⇒ `finalize_source` 把**3040 还没回加、发种区
      副本还在用**的 SSD 目录删掉 ⇒ 延迟回加(15min后)指向一个刚被删的目录 ⇒
      变下载（`queuedDL progress=0`）。⇒ 现在把 `FAILED` 也纳入交棒块：要么**重试
      交棒**（幂等，409=已在）后正常推进，要么**再判 FAILED 并提前 return** ——
      **两条路都不会 fall-through 到 finalize**。同族 `ERR-PUBLISH-07`（存在≠完成）。
    ★ 本函数**绝不 raise**（daemon 线程调用）。
    """
    dry_run = config.HANDOFF_DRY_RUN == "true" if dry_run is None else dry_run
    info_hash = item.get("infohash", "")
    ssd_dir = item.get("save_path") or ""
    target = item.get("target_save_path") or ""
    target_cat = item.get("target_category") or ""
    try:
        # ★★★ #136 结构性不变量：**只有"本趟确实走到了已交棒状态"的条目才允许清盘**。
        #   `handed_off` 由交棒块在成功时置 True（或在途状态 HANDOFF_RECHECK/SEEDING 亦然）。
        #   下面 finalize 那步会**再查一次**这个旗标 ⇒ 从结构上堵死"FAILED 跳过交棒 ⇒
        #   直接 finalize"（`217848` 的凶手），而不是靠"恰好没 fall-through"。
        handed_off = item.get("state") in ("HANDOFF_ADDED", "HANDOFF_RECHECK",
                                           "HANDOFF_SEEDING")
        # ---- 交棒（零拷贝；含隔离闸门）----
        # ★★★ `FAILED` 也在内（#136）：FAILED 只代表"上一轮某步没过"，**不代表已交棒**。
        #   让它重走交棒块 ⇒ 失败则提前 return（不 fall-through 到 finalize）。
        if item.get("state") in ("PENDING", "HANDOFF_ADDED", "FAILED"):
            # ★★★ #136（用户 2026-10-07 拍板「够格才交棒」）：**只在 PENDING（新条目）
            #   上判"够格"** —— 在途态（HANDOFF_ADDED）与 FAILED（重试）已经过了这道门，
            #   不再重复判（否则交棒到一半的条目会被打回等待）。
            #   ★ 为什么必须有这道门：原代码对 PENDING **立即交棒**，`judge` 是死代码 ⇒
            #     "出种≥N 才交棒"从未生效（实测 `已交棒到 3020` 计数=0）。
            #   ★ 不够格 ⇒ **停在 PENDING**（不交棒、不标 FAILED、不动源）⇒ 下轮再看。
            if item.get("state") == "PENDING" and not dry_run:
                _present, _elig, _ewhy = eligible_on_3040(qb_src, info_hash,
                                                          name=item.get("name") or item.get("album"))
                if not _elig:
                    update_item(info_hash, state="PENDING",
                                last_error=f"等够格交棒: {_ewhy}")
                    return "PENDING"
            # ★★★ #134 第四道闸门：交棒前**确认 SSD 副本还在**。
            #   ★ 为什么：队列条目可能在"数据其实没到位/已被清"的情况下残留（例如某轮
            #     复制失败仍入了队、或人工清过盘）。此时交棒到 3020 会 add 一条**没有
            #     文件的种** ⇒ 3020 也变下载。⇒ 宁可判失败（可重试/人工介入），
            #     也不推一条假种。★ dry-run 下跳过（演练不落盘、无副本可言）。
            if not dry_run:
                ssd_ok, ssd_why = verify_publish_area_complete(
                    item.get("src_path") or "", item.get("album") or "")
                if not ssd_ok and ssd_dir:
                    # 兜底：老条目没有 src_path/album ⇒ 退化为"目录存在且非空"。
                    if not (os.path.isdir(ssd_dir) and os.listdir(ssd_dir)):
                        update_item(info_hash, state="FAILED",
                                    last_error=f"发种区副本不在位，拒交棒: {ssd_why}",
                                    first_failed_at=item.get("first_failed_at") or _now())
                        return "FAILED"
            ok, why = handoff_to_3020(qb_src, qb_dst, info_hash, target, dry_run=dry_run,
                                     target_category=target_cat or None,
                                     name=item.get("name") or item.get("album"))
            if not ok:
                update_item(info_hash, state="FAILED", last_error=why,
                            first_failed_at=item.get("first_failed_at") or _now())
                return "FAILED"
            update_item(info_hash, state="HANDOFF_ADDED")
            handed_off = True
            if dry_run:
                return "HANDOFF_ADDED"
            item = dict(item, state="HANDOFF_ADDED")
        # ---- 校验 3020 是否已做种 ----
        _nm = item.get("name") or item.get("album")
        ready, why = ((True, "dry-run") if dry_run
                      else verify_3020_seeding(qb_dst, info_hash, name=_nm))
        if not ready:
            # 3020 还没校验完 ⇒ 停在这一步，下轮再看（**不**删源）
            update_item(info_hash, state="HANDOFF_RECHECK", last_error=why)
            return "HANDOFF_RECHECK"
        update_item(info_hash, state="HANDOFF_SEEDING")
        # ---- 清盘（★ 只有 3020 已做种才走到这）----
        # ★★★ #136 最后一道结构性闸门：**没交棒过就绝不清盘**。走到这理论上必有
        #   `handed_off=True`（PENDING/FAILED 都过了交棒块，在途态本身也为真）；此处
        #   显式再拦一次 —— 宁可停在 HANDOFF_SEEDING 让人工看，也不删不该删的盘。
        if not handed_off:
            update_item(info_hash, state="FAILED",
                        last_error="未完成交棒却走到清盘（已拦，拒删源）⇒ 需人工核")
            return "FAILED"
        if finalize_source(qb_src, info_hash, ssd_dir, dry_run=dry_run, name=_nm):
            if not dry_run:
                remove_item(info_hash)
            # ★ 清 last_error：进 DONE 是"成功终态"，不该再留上一轮的路过标签
            #   （否则队列/日志里会看到"3020 上还没有这条种"这种**过期**文案，
            #    而状态其实是 DONE —— NAS-92g 实测踩到，纯误导）。
            update_item(info_hash, state="DONE", last_error="")
            return "DONE"
        update_item(info_hash, state="FAILED", last_error="清盘失败")
        return "FAILED"
    except Exception as e:                                        # noqa: BLE001
        update_item(info_hash, state="FAILED", last_error=f"{type(e).__name__}: {e}")
        return "FAILED"
