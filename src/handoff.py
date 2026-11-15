"""发种数据流转与做种交棒模块。

管理成品媒体数据从 HDD 存储目录至 SSD 发种区的复制、
以及做种达标（出种数或做种时长满足阈值）后由发种器向常规做种器交棒并安全清理 SSD 副本的完整生命周期。
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
            category="", target_category="", target_tags=None, src_path="", album=""):
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
        "target_tags": list(target_tags or []), # ★ 3020 上要打的标签（星湾等不建分类的站点）
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

def is_own_project(category, own_categories=None, tags=None, own_tags=None):
    """这条种是不是"本项目管的"？判据 = **分类或标签命中白名单**。

    ★★ 用户 2026-10-07 指令：「**仅限本项目的音乐种子**才移动到 3040 交接做种，
       这个 3020qb 我可能还要发其他种子的」⇒ 本流程**绝不许误碰**非本项目种。
    ★ 判据（用户已拍板）= **3040 上的分类** ∈ `config.HANDOFF_OWN_CATEGORIES`
      （默认 `{Musopia, CrabPT, PTer}`），**或**该种的标签 ∩
      `config.HANDOFF_OWN_TAGS`（默认 `{Xingwan}` —— 星湾不建分类，只打标签）。
    ★★ 阴性对照（本函数**必须能"说不"**）：
      · `category=""` 且无标签（用户手加的种常见）⇒ **False**。
      · `category="SomeOther"` 且标签不命中 ⇒ **False**。
      · `category="Musopia"` / `"CrabPT"` ⇒ **True**。
      · 标签含 `"Xingwan"`（分类为空）⇒ **True**。
    """
    own = config.HANDOFF_OWN_CATEGORIES if own_categories is None else own_categories
    if str(category or "").strip() in set(own):
        return True
    own_t = getattr(config, "HANDOFF_OWN_TAGS", []) if own_tags is None else own_tags
    have = {str(t).strip() for t in (tags or []) if str(t or "").strip()}
    return bool(have & {str(t).strip() for t in (own_t or []) if str(t or "").strip()})


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
      "已复制好"⇒ 跳过复制 ⇒ 变下载（2026-10-07 `Beyond` 实测，）。
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


def ensure_hardlinks(src_dir, dst_dir, dry_run=False):
    """将源目录/文件递归硬链接（os.link）到目标目录中，实现零磁盘冗余做种。

    - 支持单文件或整目录递归处理；
    - 目标父目录自动创建；
    - 幂等性：目标文件已存在且与源文件同一 inode 时，直接跳过；
    - 若目标文件已存在但为普通文件且 inode 不同，则安全替换；
    - 返回 (成功布尔值, 详情信息)。
    """
    if not os.path.exists(src_dir):
        return False, f"源路径不存在: {src_dir}"

    if dry_run:
        return True, f"[dry-run] 将硬链接 {src_dir} → {dst_dir}"

    try:
        if os.path.isfile(src_dir):
            os.makedirs(os.path.dirname(dst_dir), exist_ok=True)
            if os.path.exists(dst_dir):
                if os.path.samefile(src_dir, dst_dir):
                    return True, "单文件硬链接已存在且一致"
                os.remove(dst_dir)
            os.link(src_dir, dst_dir)
            return True, f"已完成单文件硬链接: {dst_dir}"

        if not os.path.isdir(src_dir):
            return False, f"源路径不是有效文件或目录: {src_dir}"

        os.makedirs(dst_dir, exist_ok=True)
        linked_count = 0
        skipped_count = 0

        for root, _dirs, files in os.walk(src_dir):
            rel_dir = os.path.relpath(root, src_dir)
            cur_dst_dir = os.path.join(dst_dir, rel_dir) if rel_dir != "." else dst_dir
            os.makedirs(cur_dst_dir, exist_ok=True)

            for fn in files:
                s_file = os.path.join(root, fn)
                d_file = os.path.join(cur_dst_dir, fn)
                if os.path.exists(d_file):
                    if os.path.samefile(s_file, d_file):
                        skipped_count += 1
                        continue
                    os.remove(d_file)
                os.link(s_file, d_file)
                linked_count += 1

        return True, f"硬链接建立完成 (新增: {linked_count}, 跳过已有: {skipped_count})"
    except Exception as e:
        return False, f"建立硬链接异常: {type(e).__name__}: {e}"


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


#
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

    ★★ 这是  落地的那条强判据：**文件集合/字节数相同推不出分片相同**。
      ⇒ 判据 MUST 是「按 torrent 的顺序把字节串起来，逐片算 sha1，与 torrent 里的
        `pieces` 逐片比对」，全对才算"这个目录能做出这个种"。

    ★★★ 路径拼接的两个坑（同一个坑踩了三次，）：
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
    #   `begin_handoff_for_task` 判完一级就 `return`，从不把 torrent 传下来
    #   （见该函数「关键闸门」那段），所以二级逐片校验在生产里**从未跑过一次**。
    #   此处保留形参只为兼容 selftest / 未来的真接线点；**它不会自动生效**。
    """★★★ 判据：`src_path`(HDD 源) 是否**完整**已复制到发种区 `<PUBLISH_CONTAINER_DIR>/<专辑>`。

    ★ 两级判据，**便宜的先走、贵的后走**（顺序不能反）：
      · **一级（必要条件）**：文件数相同 且 总字节相同 —— 挡"只拷了头几个文件"。
      · **二级（强判据，`S6` 新增）**：给了 `torrent_file` 就再**逐片比 hash**
        —— 挡"字节数全对但内容/拼接顺序不同"（：`21811` 44 文件、
        逐文件字节数一个不差、`du -sb` 完全一致，逐片却是 **ok=49 / bad=407**）。
      ★★ 为什么二级是**可选**（`torrent_file=None` 时只有一级）：本函数被 `begin_handoff_for_task`
        在**复制刚结束时**调用，那一刻 `.torrent` 未必已生成（`publish.py` 里制种与交棒顺序
        不保证）⇒ 强制要求会把整条交棒链打挂。
        ⇒ 调用方**有**种子时必须传；没传就在说明里留一句"只做了一级"，免得读日志的人
        以为"已逐片验过"。

    ★★ 为什么必须有这一步（2026-10-07
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
                           target_tags=None, dry_run=None):
    """★ 发种流程的**接线点**：复制成品到 3040 发种区 + 入队（供之后交棒）。

    这一步把用户要的「数据复制要由本项目代码自动化完成」接进发布流：
        ① 复制 `src_path`(HDD 原文件) → `<PUBLISH_CONTAINER_DIR>/<专辑>`（3040 看得见）；
        ② **★ 核对复制确实完整**（`verify_publish_area_complete`，逐字节）；
        ③ 把这条种写进交棒队列，记下 3040 分类 / 3020 目标分类 / 目标 save_path。

    ★★ 只在 `ENABLE_HANDOFF=true` 时做（**默认关** ⇒ 老行为一字不改，）。
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
    #   在本函数的真路径上**不跑**，闸门实际只是"逐字节"。
    #   为什么没顺手接上：`torrent_file` 只在**已发种成功**的分支里有；而被拒后落库的
    #   `torrent_file`（如 `…2011-2011 FLAC 分轨.torrent` 实际是 `…Multi File.torrent`）
    #   **指向的文件根本不存在** ⇒ 接上就会"读 .torrent 失败"，把一级的绿**如实判红**
    #   ⇒ 已上站的种一条都回加不了。要接必须先保证该路径可靠，或改用
    #   `/data/output_torrents/` 下的 DB `info_hash` 去索引 —— 见 BACKLOG §4e W4。
    ok_copy, why_copy = verify_publish_area_complete(src_path, album_name)
    if not ok_copy:
        return False, f"发种区数据未就位，拒绝回加: {why_copy}"
    # ★ 目标 save_path = 3020 的父目录（HDD 原文件所在）= config.HANDOFF_TARGET_SAVE_ROOT
    target_save = config.HANDOFF_TARGET_SAVE_ROOT
    # ★ 目标身份：标签优先（星湾等不建分类）；否则沿用来源分类（乌托邦→Musopia / 蟹黄堡→CrabPT）。
    ttags = [str(t).strip() for t in (target_tags or []) if str(t or "").strip()]
    tcat = "" if ttags else (target_category or source_category or config.PUBLISH_CATEGORY)
    name = (task or {}).get("formatted_title") or album_name
    ok = enqueue(info_hash, name, ssd_dir, target_save,
                 note="auto from publish", category=source_category,
                 target_category=tcat, target_tags=ttags,
                 src_path=src_path, album=album_name)
    return ok, (f"已复制到 {ssd_dir} 并**逐字节核对通过**并入队"
                f"（3040分类={source_category or '∅'}→3020身份="
                f"{('tags:' + ','.join(ttags)) if ttags else ('分类:' + tcat)}）")


# ==================== 交棒判据（步③ 的触发）====================

def judge(num_complete, added_on, now=None,
          seeders_min=None, seed_days=None, published_at=None,
          return_remaining=False):
    """判断一条种是否够格交棒。默认返回 `(是否交棒, 原因字符串)`；若 `return_remaining=True` 则返回 `(是否交棒, 原因字符串, 预计达标所需剩余秒数)`。

    ★★ "出种≥N"的口径（这里最容易被读错，逐字说明）：
      qB 的 `num_complete` = **swarm 里做种者总数**，它**把本机自己算进去**。
      ⇒ 用户要的"出种 3 个以上"（= **别人**也开始做种了）应判
        `num_complete - 1 >= seeders_min`（**减掉自己**）。
      ★ 若直接拿 `num_complete >= 3` 去比，则"**只有自己在做种**"(num_complete=1)
        还不算数，那还好；但"自己 + 2 个"=3 会被当成"出种 3" —— **少数一个**。
        这属于"判据提到了对的词、却问了个差一的问题"⇒ 明确减 1。

    ★★ 做种天数基准（2026-11-03 修复）：`published_at`（上站时间）与 `added_on`
      （种进 3040 的时间）取**较早者**。原因：延迟回加/重新加种会把 `added_on`
      刷新成"现在" ⇒ 若只认 `added_on`，"做种≥3 天"会被**无限重置**（实测 7 条
      PENDING 里 3 条 added_on 仅 0.79~1.80 天即此故）。
      ★ 用 **min** 而不是 max：目标是不让回加重置计时器；取 max 反而会选更晚的时间，
        把重置保留下来。`published_at` 缺失 ⇒ 退回 `added_on`（与修复前一致）。

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
        msg = f"出种 {others_seeding}≥{seeders_min}（num_complete={num_complete} 含自己）"
        return (True, msg, 0) if return_remaining else (True, msg)

    try:
        base = float(added_on)
    except (TypeError, ValueError):
        base = None
    try:
        pub = float(published_at) if published_at else None
    except (TypeError, ValueError):
        pub = None
    if pub and pub > 0 and base and base > 0:
        base = min(base, pub)              # ★ 取较早者 ⇒ 回加不会重置计时
    elif pub and pub > 0 and not base:
        base = pub
    age_days = 0.0 if base is None else (now - base) / 86400.0
    if age_days >= seed_days:
        msg = (f"做种 {age_days:.2f}≥{seed_days} 天"
               f"（基准={base:g}，added_on={added_on}，published_at={published_at}）")
        return (True, msg, 0) if return_remaining else (True, msg)

    remaining_seconds = max(0.0, (float(seed_days) * 86400.0) - (0.0 if base is None else (now - base)))
    msg = f"未达：出种 {others_seeding}<{seeders_min} 且 做种 {age_days:.2f}<{seed_days} 天"
    return (False, msg, remaining_seconds) if return_remaining else (False, msg)


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

    ★★★
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


def _published_at(info_hash):
    """该条种在本项目的**首发上站时间**（epoch 秒）。读不到 ⇒ None（不猜）。

    ★ 只用于交棒判据的计时基准（见 `judge`）；`db` 惰性 import 防止循环依赖，
      且任何异常都收敛成 None ⇒ 退回 `added_on`（与修复前行为一致）。
    """
    try:
        import db
        return db.site_publish_first_at(info_hash)
    except Exception:                                             # noqa: BLE001
        return None


def eligible_on_3040(qb_src, info_hash, name=None, now=None,
                     seeders_min=None, seed_days=None, published_at=None,
                     return_remaining=False):
    """★★ #136：判"3040 上这条是否**够格交棒**"。默认返回 `(present, eligible, why)`；若 `return_remaining=True` 则返回 `(present, eligible, why, rem_sec)`。

    ★★ 为什么（用户 2026-10-07 拍板）：交棒语义 = **够格才交棒**（出种≥N 或 做种≥D 天），
      **不是**发布后就立即推给 3020。原代码 `process_one` 对 PENDING 条目**立即交棒**，
      而 `judge`/`pick_handoff_candidates` 只在 selftest 里被调 ⇒ "出种≥3 才交棒"从未生效
      （实测：`☆ac8.13f` 20 个别人做种却一直没交棒、整个容器 `已交棒到 3020` 计数=0）。
    ★ 三态（显式区分，同 ：不许把"还没轮到"与"够格"混一个词）：
      · `present=False` ⇒ 3040 上**还没这条种**（延迟回加还没到点/失败了）⇒ **等**，不交棒。
      · `present=True, eligible=False` ⇒ 在做种但**还不**够格 ⇒ **等**，不交棒。
      · `present=True, eligible=True` ⇒ 够格 ⇒ 交棒。
    ★★★ #45：`name` 用于"按站点 .torrent 的 name 兜底命中"（hash 用 A/皇后原种、
      3040 挂的是 B/站点种，两者 hash 不同）。见 `_find_on_3040`。
    ★ `qb_src is None` ⇒ 返回 `(False, False, "无 3040 连接")`（dry-run 无客户端时也走这）。
    """
    now = _now() if now is None else now
    if qb_src is None:
        return (False, False, "无 3040 连接", 0) if return_remaining else (False, False, "无 3040 连接")
    try:
        t, how = _find_on_3040(qb_src, info_hash, name=name)
    except Exception as e:                                        # noqa: BLE001
        return (False, False, str(e), 0) if return_remaining else (False, False, str(e))
    if t is None:
        msg = "3040 上还没有这条种（延迟回加未到/失败）⇒ 等"
        return (False, False, msg, 3600) if return_remaining else (False, False, msg)
    # ★ 先要 progress≥1（做完种）才有资格谈"出种/做种天数"。
    progress = float(_torrent_field(t, "progress", 0) or 0)
    if progress < 1.0:
        msg = f"3040 上还没做完种（progress={progress:.2f}）⇒ 等"
        return (True, False, msg, 300) if return_remaining else (True, False, msg)
    nc = _torrent_field(t, "num_complete", 0)
    added = _torrent_field(t, "added_on", 0)
    # ★ 计时基准：优先用调用方给的"上站/入队时间"（队列条目自带 `enqueued_at`，
    #   不受回加重置）；没有再查 `site_publish`。两者都没有 ⇒ 退回 added_on。
    #   ★ 为什么不只查 `site_publish`：队列里的 info_hash 是 **A（原种）**，
    #     `site_publish.info_hash` 是 **B（站点种）** —— 不同 hash 空间（实测零交集），
    #     直接按 hash 查会恒为 None。`enqueued_at` 是发布时写下的、稳定的同一事实。
    pub = published_at or _published_at(info_hash)
    res = judge(nc, added, now=now, seeders_min=seeders_min, seed_days=seed_days,
                published_at=pub, return_remaining=return_remaining)
    if return_remaining:
        ok, why, rem_sec = res
    else:
        ok, why = res
        rem_sec = 0
    if how == "name":
        # 提示身份不一致（值得记一笔）：hash 没找到、按名字才找到。
        why = f"[按名字命中 3040] {why}"
    return (True, ok, why, rem_sec) if return_remaining else (True, ok, why)


# ==================== 交棒 3040→3020（步③，零拷贝）====================

def export_torrent_bytes(qb, info_hash):
    """从 3040 导出该种的 .torrent 字节（交棒到 3020 用）。"""
    data = qb.torrents_export(torrent_hash=info_hash)
    if isinstance(data, str):
        return data.encode("utf-8")
    return bytes(data)


def handoff_to_3020(qb_src, qb_dst, info_hash, target_save_path, dry_run=True,
                   target_category=None, target_tags=None, own_categories=None, name=None,
                   src_path=None, album=None, dst_host=None):
    """把种的任务从 3010 交棒到常规做种实例（如 3220/3240）。

    零开销硬链接机制：
      在向目标 qB 添加任务前，确保源文件（/volume1/.../music3/专辑）
      已通过 ensure_hardlinks 递归硬链接到目标物理目录（/volume1/.../music1或2/专辑）。
      目标 qB 挂载的 /downloads 即可直接对齐做种，零重新校验，零磁盘空间浪费。
    """
    # ---- 隔离闸门：先确认"这是本项目种的分类"（查 3040 实况）----
    cat, gate_err = _source_category(qb_src, info_hash, dry_run=dry_run, name=name)
    if gate_err:
        return False, gate_err
    # ★ #45：拿 3040 上的真实 hash（可能是 B，与 info_hash=A 不同）。
    real_hash = info_hash
    try:
        _t, _how = _find_on_3040(qb_src, info_hash, name=name)
        if _t is not None:
            real_hash = str(_torrent_field(_t, "hash", "") or info_hash)
    except Exception:                                             # noqa: BLE001
        _how = ""
    if not is_own_project(cat, own_categories,
                          tags=_source_tags(qb_src, info_hash, name=name)):
        return False, (f"拒交棒：3040 分类 {cat!r} 与标签都不在本项目白名单 "
                       f"（分类 {config.HANDOFF_OWN_CATEGORIES} / 标签 {config.HANDOFF_OWN_TAGS}）"
                       f"（疑似非本项目种，不碰）")
    # ---- 目标身份：优先标签（星湾不建分类）；否则沿用来源分类且必须是白名单之一 ----
    if target_tags:
        tcat = ""
        ttags = [str(t).strip() for t in target_tags if str(t or "").strip()]
    else:
        ttags = []
        tcat = target_category if target_category else (cat or config.PUBLISH_CATEGORY)
        if not is_own_project(tcat, own_categories):
            return False, f"拒交棒：目标分类 {tcat!r} 不在本项目白名单"

    # ---- 零开销硬链接建立（HDD music3 -> 目标 HDD music1/music2）----
    if dst_host:
        target_root_dir = config.HANDOFF_DIR_MAP.get(dst_host)
        if target_root_dir and album:
            dest_album_dir = os.path.join(target_root_dir, album)
            # 源物理路径优先取 src_path，若为容器内 /downloads 则映射为宿主物理路径
            real_src_path = src_path
            if real_src_path and real_src_path.startswith("/downloads"):
                music3_root = config.HANDOFF_DIR_MAP.get(
                    config.QB_HOST_OPENCD, "/volume1/video/download/music/music3"
                )
                rel = os.path.relpath(real_src_path, "/downloads")
                real_src_path = os.path.join(music3_root, rel) if rel != "." else music3_root

            # 若目标做种目录尚未存在或为空，则必须从皇后源执行硬链接预热
            need_link = True
            if os.path.isdir(dest_album_dir):
                try:
                    if os.listdir(dest_album_dir):
                        need_link = False
                except OSError:
                    pass

            if need_link:
                if real_src_path and os.path.exists(real_src_path):
                    hl_ok, hl_msg = ensure_hardlinks(real_src_path, dest_album_dir, dry_run=dry_run)
                    if not hl_ok:
                        return False, f"交棒前硬链接失败: {hl_msg}"
                    if dry_run:
                        print(f"🅿️  [handoff][dry-run] 硬链接: {hl_msg}")
                    else:
                        print(f"🔗 [handoff] 跨库硬链接建立成功: {dest_album_dir}")
                else:
                    return False, f"未找到皇后源音乐目录，拒绝空跑交棒: {real_src_path}"

    if dry_run:
        print(f"🅿️  [handoff][dry-run] 将交棒 {real_hash[:12]}… → 目标实例 "
              f"save_path={target_save_path} category={tcat or '∅'}"
              f" tags={ttags or '∅'}（3040分类={cat or '∅'}）")
        return True, "dry-run"
    try:
        content = export_torrent_bytes(qb_src, real_hash)
    except Exception as e:                                        # noqa: BLE001
        return False, f"从 3040 导出 .torrent 失败: {type(e).__name__}: {e}"
    if not content:
        return False, "从 3040 导出 .torrent 为空"

    # ★★★ 严格禁止“空跑加种（Skip Checking）”安全门禁：
    #   在向目标做种器添加任务并跳过校验前，必须确保目标物理目录下真实存在媒体文件且非空。
    #   若目标目录不存在或为空，严禁调用 is_skip_checking=True，拒绝产生 0% missingFiles 的僵尸任务。
    if not dry_run and dst_host:
        target_root_dir = config.HANDOFF_DIR_MAP.get(dst_host)
        if target_root_dir and album:
            dest_album_dir = os.path.join(target_root_dir, album)
            if not os.path.isdir(dest_album_dir):
                return False, f"目标做种目录不存在，拒绝空跑加种: {dest_album_dir}"
            try:
                entries = os.listdir(dest_album_dir)
                if not entries:
                    return False, f"目标做种目录为空，拒绝空跑加种: {dest_album_dir}"
            except OSError as e:
                return False, f"读取目标做种目录失败，拒绝空跑加种: {e}"

    # ★★★ 优雅交接停源防碰撞（Graceful Handoff）：
    #   在向目标做种器添加任务前，先让 3010 发送 stop 暂停任务。
    #   这会促使 qB 向站点 Tracker 汇报 &event=stopped，通知 Tracker 立即注销 3010 的 Peer 占用，
    #   彻底杜绝 3220/3240 启动时触发站点 "same torrent more than 1 client" 冲突拒绝。
    stopped_src = False
    try:
        qb_src.torrents_stop(torrent_hashes=real_hash)
        stopped_src = True
        time.sleep(1)  # 给 Tracker 留出处理 stopped 事件的短暂窗口
    except Exception as e:
        print(f"⚠️  [handoff] 暂停 3010 源任务时捕获异常（继续尝试交接）: {e}")

    try:
        # ★ 与 readd 同款：若已有完整硬链接，开启 is_skip_checking=True 避免卡死后台校验队列
        qb_dst.torrents_add(
            torrent_files=content,
            save_path=target_save_path,
            is_paused=False,
            is_skip_checking=True,
            category=tcat,
            tags=ttags or None,
        )
    except Exception as e:                                        # noqa: BLE001
        # 目标端添加失败 ⇒ 安全回滚：把 3010 重新恢复做种
        if stopped_src:
            try:
                qb_src.torrents_start(torrent_hashes=real_hash)
            except Exception:
                pass
        name_e = type(e).__name__
        # 409 = 3020 上已有这条种（多半是别的容器辅种抢先加了）⇒ 视为"已在做种"。
        if name_e == "Conflict409Error":
            print(f"ℹ️  [handoff] 目标做种实例已有该种（409）= 视为已交棒: {real_hash[:12]}…")
            return True, "409 已存在"
        return False, f"add 目标实例失败: {name_e}: {e}"
    print(f"✅ 已优雅交棒到目标做种实例: {real_hash[:12]}… "
          f"category={tcat or '∅'} tags={ttags or '∅'}")
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


def _source_tags(qb_src, info_hash, name=None):
    """查 3040 上该种的 `tags`（列表）。**只读**，查不到/出错一律返回 `[]`。

    ★ 星湾等"不建分类、只打标签"的站点靠它命中 `config.HANDOFF_OWN_TAGS`。
    ★ 与 `_source_category` 分开：分类闸门仍按"查不到就拒"处理，标签只是**加宽**命中面
      （标签查不到 = 没命中，不改变分类闸门的结论）。
    """
    if qb_src is None:
        return []
    try:
        t, _how = _find_on_3040(qb_src, info_hash, name=name)
        if t is None:
            return []
        raw = _torrent_field(t, "tags", []) or []
        if isinstance(raw, str):
            raw = [x for x in raw.split(",") if x.strip()]
        return [str(x).strip() for x in raw if str(x or "").strip()]
    except Exception:                                             # noqa: BLE001
        return []


def verify_3020_seeding(qb_dst, info_hash, name=None):
    """查做种实例上该种是否已"校验通过且在做种"。返回 `(是否就绪, 明细)`。

    ★ 判据（与 NAS-92 §1.5 一致）：`progress >= 1.0` 且 state ∈
      {stalledUP, uploading, forcedUP, queuedUP}。★ 只在这些都成立时才允许删源。
    ★★ 这是"删 SSD"前的**唯一**安全门 —— 门没通过 ⇒ 绝不进 `finalize_source`。
    ★★★ #45：`name` 兜底 —— 交棒到 3020 的是站点种(B)，其 hash 与 `info_hash`(A) 不同
      ⇒ 按 hash 查不到时，必须按 3020 上的 **name** 再找一次，否则会误判"3020 上还没有"
      ⇒ 永远卡在 HANDOFF_RECHECK（交棒了却永不 DONE）。
    ★ 容错增强：若当前 qb_dst 查不到，自动轮询 HANDOFF_MUSIC_HOSTS 中其他做种实例，避免由于动态负载均衡导致找不到种。
    """
    def _check_one(cli):
        try:
            info = cli.torrents_info(torrent_hashes=info_hash)
        except Exception as e:                                    # noqa: BLE001
            return False, f"查目标实例失败: {type(e).__name__}: {e}", None
        if not info and name:
            want = str(name).strip()
            try:
                for t in cli.torrents_info():
                    if str(_torrent_field(t, "name", "") or "").strip() == want:
                        info = [t]
                        break
            except Exception:                                     # noqa: BLE001
                pass
        if not info:
            return False, "目标做种实例上还没有这条种", None
        t = info[0]
        progress = float(_torrent_field(t, "progress", 0) or 0)
        state = str(_torrent_field(t, "state", "") or "")
        seeding_states = {"stalledUP", "uploading", "forcedUP", "queuedUP", "checkingUP"}
        if progress >= 1.0 and state in seeding_states:
            return True, f"做种中（progress={progress:.2f} state={state}）", t
        return False, f"未就绪（progress={progress:.2f} state={state}）", t

    ready, msg, found_t = _check_one(qb_dst)
    if ready:
        return True, msg

    # 若当前实例未找到（例如被分配到了 3220 或 3240 另一台），尝试兜底查验其他音乐实例
    if "还没有这条种" in msg:
        for host in getattr(config, "HANDOFF_MUSIC_HOSTS", []):
            try:
                import types, qb_client
                cfg_alt = types.SimpleNamespace(
                    QB_HOST=host,
                    QB_USER=getattr(config, "QB_USER", "admin"),
                    QB_PASS=config.QB_PASS,
                    QB_API_KEY=getattr(config, "OPENCD_QB_API_KEY", ""),
                )
                alt_cli = qb_client.build(cfg_alt)
                alt_ready, alt_msg, _ = _check_one(alt_cli)
                if alt_ready:
                    return True, f"{host} {alt_msg}"
            except Exception:
                continue

    return ready, msg


# ==================== 清 SSD 副本（步④，严格时序）====================

def _safe_rmtree(path):
    """★ 只在 `PUBLISH_CONTAINER_DIR` **之下**清理一个具体子目录或软链接。

    多重护栏（任何一条不满足 ⇒ 拒删并返回 False）：
      ① path 必须位于 `PUBLISH_CONTAINER_DIR` 之下（不是它本身）；
      ② 规范化后不得等于 publish 根；
      ③ 若为软链接，直接 unlink 软链接本身，绝不跟随软链接递归删除；
      ④ 若为真实目录，递归删除 SSD 副本。
    """
    root = os.path.abspath(config.PUBLISH_CONTAINER_DIR)
    target = os.path.abspath(path)
    if target == root:
        print(f"❌ [handoff] 拒删：目标是发种区根目录本身 ({target})")
        return False
    if not target.startswith(root + os.sep):
        print(f"❌ [handoff] 拒删：目标不在发种区之下 ({target} ∉ {root})")
        return False
    if os.path.islink(path):
        os.unlink(path)
        print(f"🗑️  [handoff] 已删 SSD 软链接: {path}")
        return True
    if not os.path.isdir(path):
        print(f"ℹ️  [handoff] 待删目录不存在（已清理？）: {path}")
        return True
    real = os.path.realpath(path)
    if not real.startswith(root + os.sep):
        # 自身不是软链接但 realpath 逃逸到了外层（如包含外层挂载）
        print(f"❌ [handoff] 拒删：目录真实路径不在发种区之下 ({real} ∉ {root})")
        return False
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
    elif not is_own_project(cat, own_categories,
                            tags=_source_tags(qb_src, info_hash, name=name)):
        print(f"❌ [handoff] 拒清盘：3040 分类 {cat!r} 与标签都不在本项目白名单 "
              f"（分类 {config.HANDOFF_OWN_CATEGORIES} / 标签 {config.HANDOFF_OWN_TAGS}）"
              f"（疑似非本项目种，不碰）")
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

#: `eligible_on_3040` 里"3040 上找不到该种"的原因前缀 —— 只有**身份匹配失败**才计入熔断。
#: ★ 连不上/无客户端等**环境**问题不计入（否则一次 qB 抖动就会把健康条目熔断掉）。
_MATCH_MISS_MARK = "3040 上还没有这条种"


def _mark_failed(item, why):
    """把一条标成 FAILED（计 attempts + 指数退避），达上限 ⇒ ABANDONED。返回新 state。

    ★ 2026-11-03（排查报告
      `pending[:HANDOFF_MAX_PER_ROUND]` ⇒ 队头几条永久失败会**占满名额**，后面健康的
      条目永远轮不到（实测 11-02 入队的 2 条 `attempts=0` 卡住即此故）。
    """
    info_hash = item.get("infohash", "")
    attempts = int(item.get("attempts", 0) or 0) + 1
    first_failed = item.get("first_failed_at") or _now()
    if attempts >= config.HANDOFF_MAX_ATTEMPTS:
        update_item(info_hash, state="ABANDONED", last_error=why,
                    attempts=attempts, first_failed_at=first_failed,
                    next_check_at=None)
        print(f"❌ [handoff] {info_hash[:12]}… 连续失败 {attempts} 次，"
              f"熔断为 ABANDONED（移出 pending）: {why}")
        return "ABANDONED"
    delay = min(config.HANDOFF_RETRY_BASE * (2 ** max(0, attempts - 1)),
                config.HANDOFF_RETRY_CAP)
    update_item(info_hash, state="FAILED", last_error=why,
                attempts=attempts, first_failed_at=first_failed,
                next_check_at=_now() + delay)
    return "FAILED"


def _mark_match_missing(item, why):
    """3040 上"hash 与 name 都匹配不到"的连续计数；超阈值 ⇒ ABANDONED。

    ★ 这类失败**重试再多也无解**（3040 上挂的根本不是这条种的同身份副本），
      故用独立计数器 `match_fail` 而不是 `attempts`，超阈值直接移出 pending。
    """
    info_hash = item.get("infohash", "")
    fails = int(item.get("match_fail", 0) or 0) + 1
    if fails >= config.HANDOFF_MATCH_MAX_FAILS:
        update_item(info_hash, state="ABANDONED",
                    last_error=f"3040 上连续 {fails} 轮匹配不到该种 ⇒ 熔断: {why}",
                    match_fail=fails, next_check_at=None)
        print(f"❌ [handoff] {info_hash[:12]}… 身份匹配连续失败 {fails} 次，"
              f"熔断为 ABANDONED（移出 pending）: {why}")
        return "ABANDONED"
    update_item(info_hash, state="PENDING",
                last_error=f"等 3040 回加/够格（匹配失败 {fails}/"
                           f"{config.HANDOFF_MATCH_MAX_FAILS}）: {why}",
                match_fail=fails)
    return "PENDING"


def process_one(qb_src, qb_dst, item, now=None, dry_run=None, dst_host=None):
    """推进队列里**一条**的状态机（PENDING→…→DONE）。返回新的 state 字符串。

    ★ 每步都幂等：3020 已存在(409) ⇒ 视为已交棒；SSD 目录已不在 ⇒ 视为已清。
    ★ 任一步失败 ⇒ 置 FAILED 并记 `last_error`（不删任何东西，下轮可重试）。
    ★★★
      落到 `verify_3020_seeding` + `finalize_source`。原代码的交棒块只认
      `("PENDING", "HANDOFF_ADDED")` ⇒ 一条 `FAILED` 条目会**跳过交棒**、直接
      往下走到清盘：若此时 3020 那边**恰好已有该种在做种**（例如辅种工具抢先加了），
      `verify_3020_seeding` 判就绪 ⇒ `finalize_source` 把**3040 还没回加、发种区
      副本还在用**的 SSD 目录删掉 ⇒ 延迟回加(15min后)指向一个刚被删的目录 ⇒
      变下载（`queuedDL progress=0`）。⇒ 现在把 `FAILED` 也纳入交棒块：要么**重试
      交棒**（幂等，409=已在）后正常推进，要么**再判 FAILED 并提前 return** ——
      **两条路都不会 fall-through 到 finalize**。同族 （存在≠完成）。
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
            #   上判"够格"** —— 在途态（HANDOFF_ADDED）与 FAILED（重试）已经过了这道门，
            #   不再重复判（否则交棒到一半的条目会被打回等待）。
            #   ★ 为什么必须有这道门：原代码对 PENDING **立即交棒**，`judge` 是死代码 ⇒
            #     "出种≥N 才交棒"从未生效（实测 `已交棒到 3020` 计数=0）。
            #   ★ 不够格 ⇒ **停在 PENDING**（不交棒、不标 FAILED、不动源）⇒ 下轮再看。
            if item.get("state") == "PENDING" and not dry_run:
                _present, _elig, _ewhy, _rem_sec = eligible_on_3040(qb_src, info_hash,
                                                                    name=item.get("name") or item.get("album"),
                                                                    published_at=item.get("enqueued_at"),
                                                                    return_remaining=True)
                if not _present and _MATCH_MISS_MARK in (_ewhy or ""):
                    #   超阈值熔断为 ABANDONED（移出 pending）—— 重试再多也无解。
                    return _mark_match_missing(item, _ewhy)
                if not _present:
                    # ★ 环境问题（qB 无连接/连不上）**不计熔断**，否则一次抖动会误杀健康条目。
                    update_item(info_hash, state="PENDING",
                                last_error=f"等 3040 就绪: {_ewhy}")
                    return "PENDING"
                if item.get("match_fail"):
                    update_item(info_hash, match_fail=0)     # 命中 ⇒ 清零计数
                if not _elig:
                    # ★ 退避机制：设置 next_check_at 为短周期（最高 1800 秒 / 30分钟），避免由于出种人数达标却被冰冻数天
                    delay = min(1800.0, max(300.0, float(_rem_sec or 1800.0)))
                    update_item(info_hash, state="PENDING",
                                last_error=f"等够格交棒: {_ewhy}",
                                next_check_at=_now() + delay)
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
                        return _mark_failed(
                            item, f"发种区副本不在位，拒交棒: {ssd_why}")
            ok, why = handoff_to_3020(qb_src, qb_dst, info_hash, target, dry_run=dry_run,
                                     target_category=target_cat or None,
                                     target_tags=item.get("target_tags") or None,
                                     name=item.get("name") or item.get("album"),
                                     src_path=item.get("src_path"),
                                     album=item.get("album"),
                                     dst_host=dst_host)
            if not ok:
                return _mark_failed(item, why)
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
            attempts = int(item.get("attempts", 0) or 0) + 1
            delay = min(config.HANDOFF_RETRY_BASE * (2 ** max(0, attempts - 1)),
                        config.HANDOFF_RETRY_CAP)
            update_item(info_hash, state="HANDOFF_RECHECK", last_error=why,
                        attempts=attempts, next_check_at=_now() + delay)
            return "HANDOFF_RECHECK"
        update_item(info_hash, state="HANDOFF_SEEDING")
        # ---- 清盘（★ 只有 3020 已做种才走到这）----
        # ★★★ #136 最后一道结构性闸门：**没交棒过就绝不清盘**。走到这理论上必有
        #   `handed_off=True`（PENDING/FAILED 都过了交棒块，在途态本身也为真）；此处
        #   显式再拦一次 —— 宁可停在 HANDOFF_SEEDING 让人工看，也不删不该删的盘。
        if not handed_off:
            return _mark_failed(item, "未完成交棒却走到清盘（已拦，拒删源）⇒ 需人工核")
        if finalize_source(qb_src, info_hash, ssd_dir, dry_run=dry_run, name=_nm):
            if not dry_run:
                remove_item(info_hash)
            # ★ 清 last_error：进 DONE 是"成功终态"，不该再留上一轮的路过标签
            #   （否则队列/日志里会看到"3020 上还没有这条种"这种**过期**文案，
            #    而状态其实是 DONE —— NAS-92g 实测踩到，纯误导）。
            update_item(info_hash, state="DONE", last_error="")
            return "DONE"
        return _mark_failed(item, "清盘失败")
    except Exception as e:                                        # noqa: BLE001
        return _mark_failed(item, f"{type(e).__name__}: {e}")

