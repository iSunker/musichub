"""
★★ 发种交棒（#92 新流程，2026-10-07）—— 复制成品 HDD→SSD / 判据 / 交棒 3040→3020 / 清 SSD。

用户 2026-10-07 定稿的流程（逐字意图）：
    ① 发种前：把成品从 HDD(`/volume1/video/music/<专辑>`，= 3020 的 `/downloads`)
       **复制**到 **3040 看得见的 SSD 发种区**（`<PUBLISH_CONTAINER_DIR>/<专辑>`，
       宿主 = `/volume2/docker_ssd/qbittorrent-publish/Download/<专辑>`）。
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


def enqueue(info_hash, name, save_path, target_save_path, note="", state="PENDING",
            category="", target_category=""):
    """把一条待交棒的种写进队列。**幂等**（同 infohash 已在 ⇒ 直接返回 True）。

    ★ 本步**不复制数据** —— 复制是发种前的事（见 `copy_to_publish_area`），
      交棒本身是零拷贝。这里只记"这条种到了 3020 该落在哪个 save_path"。
    ★★ `category` = 该种在 **3040** 上的分类（隔离判据的来源，见 `is_own_project`）；
      `target_category` = 交棒到 **3020** 后打哪个分类（★ 只能是白名单之一）。
    """
    item = {
        "infohash": info_hash,
        "name": name,
        "save_path": save_path,                 # 3040 上的路径（发种区）
        "target_save_path": target_save_path,  # 3020 上的路径（HDD 原文件）
        "category": category,                   # ★ 3040 上的分类（隔离判据）
        "target_category": target_category,     # ★ 3020 上要打的分类（白名单之一）
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
    ★ 已存在且非空 ⇒ 视为已复制（幂等），不重复拷。
    ★★ 只用 `copytree`/`copyfile` **单向** 复制；**绝不**反过来动 `src_path`（HDD 原文件）。
    """
    dst = publish_target_dir(album_name)
    if not os.path.isdir(src_path):
        raise FileNotFoundError(f"源成品不存在: {src_path}")
    if os.path.isdir(dst) and os.listdir(dst):
        print(f"ℹ️  [handoff] 发种区已存在（跳过复制）: {dst}")
        return dst
    if dry_run:
        print(f"🅿️  [handoff][dry-run] 将复制 {src_path} → {dst}")
        return dst
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.isdir(src_path):
        shutil.copytree(src_path, dst, dirs_exist_ok=True)
    else:
        os.makedirs(dst, exist_ok=True)
        shutil.copy2(src_path, os.path.join(dst, os.path.basename(src_path)))
    print(f"✅ [handoff] 已复制到发种区: {dst}")
    return dst


def begin_handoff_for_task(src_path, album_name, info_hash, task=None,
                           source_category="", target_category="",
                           dry_run=None):
    """★ 发种流程的**接线点**：复制成品到 3040 发种区 + 入队（供之后交棒）。

    这一步把用户要的「数据复制要由本项目代码自动化完成」接进发布流：
        ① 复制 `src_path`(HDD 原文件) → `<PUBLISH_CONTAINER_DIR>/<专辑>`（3040 看得见）；
        ② 把这条种写进交棒队列，记下 3040 分类 / 3020 目标分类 / 目标 save_path。

    ★★ 只在 `ENABLE_HANDOFF=true` 时做（**默认关** ⇒ 老行为一字不改，ERR-ENV-02）。
    ★★ 幂等：已在队列（同 infohash）⇒ 直接返回（不重复复制/入队）。
    ★ `task` 是 publish 侧的 task dict（可选，用于备注名）。
    ★ 返回 `(成功, 说明)`。**绝不 raise**（发布主流程不能被它带崩）。
    """
    if str(config.ENABLE_HANDOFF).strip().lower() not in ("1", "true", "yes", "y", "on"):
        return False, "ENABLE_HANDOFF 未开（跳过复制+入队）"
    dry = (config.HANDOFF_DRY_RUN == "true") if dry_run is None else dry_run
    try:
        ssd_dir = copy_to_publish_area(src_path, album_name, dry_run=dry)
    except Exception as e:                                        # noqa: BLE001
        return False, f"复制到发种区失败: {type(e).__name__}: {e}"
    # ★ 目标 save_path = 3020 的父目录（HDD 原文件所在）= config.HANDOFF_TARGET_SAVE_ROOT
    target_save = config.HANDOFF_TARGET_SAVE_ROOT
    # ★ 目标分类：默认沿用来源分类（乌托邦→Musopia / 蟹黄堡→CrabPT），须在白名单内。
    tcat = target_category or source_category or config.PUBLISH_CATEGORY
    name = (task or {}).get("formatted_title") or album_name
    ok = enqueue(info_hash, name, ssd_dir, target_save,
                 note="auto from publish", category=source_category,
                 target_category=tcat)
    return ok, f"已复制到 {ssd_dir} 并入队（3040分类={source_category or '∅'}→3020分类={tcat}）"


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


# ==================== 交棒 3040→3020（步③，零拷贝）====================

def export_torrent_bytes(qb, info_hash):
    """从 3040 导出该种的 .torrent 字节（交棒到 3020 用）。"""
    data = qb.torrents_export(torrent_hash=info_hash)
    if isinstance(data, str):
        return data.encode("utf-8")
    return bytes(data)


def handoff_to_3020(qb_src, qb_dst, info_hash, target_save_path, dry_run=True,
                   target_category=None, own_categories=None):
    """把种的**任务**从 3040 交棒到 3020：export → add(3020, save_path) → recheck。

    ★★ **零拷贝**：不搬数据 —— 3020 的 `/downloads` 就是 `/volume1/video/music`，
      所以只要给对 `save_path`（HDD 原文件所在），3020 自己校验即可。
    ★★★ **隔离闸门（第一道）**：交棒前先查 3040 上该种的 `category`，
      不命中白名单（`is_own_project`）⇒ **拒绝交棒**，返回 `(False, 原因)`。
      ★ 为什么在这里也查一遍：队列可能被外部篡改/残留旧项 ⇒ 不只信入队时的判据。
    ★ `target_category` = 交棒到 3020 后打哪个分类。★ **只能取白名单之一**；
      缺省 = `config.PUBLISH_CATEGORY`。传给非白名单值 ⇒ 拒（防污染 3020 分类体系）。
    ★ `dry_run=True` ⇒ 只打印计划，不 add、不 recheck。
    ★ 返回 `(成功, 原因)`。**不删任何东西**（删是下一步 `finalize_source`）。
    """
    # ---- 隔离闸门：先确认"这是本项目种的分类"（查 3040 实况）----
    cat, gate_err = _source_category(qb_src, info_hash, dry_run=dry_run)
    if gate_err:
        return False, gate_err
    if not is_own_project(cat, own_categories):
        return False, (f"拒交棒：3040 分类 {cat!r} 不在本项目白名单 "
                       f"{config.HANDOFF_OWN_CATEGORIES}（疑似非本项目种，不碰）")
    # ---- 目标分类只能是白名单之一（否则污染 3020 的分类体系）----
    tcat = target_category if target_category else (cat or config.PUBLISH_CATEGORY)
    if not is_own_project(tcat, own_categories):
        return False, f"拒交棒：目标分类 {tcat!r} 不在本项目白名单"
    if dry_run:
        print(f"🅿️  [handoff][dry-run] 将交棒 {info_hash[:12]}… → 3020 "
              f"save_path={target_save_path} category={tcat}（3040分类={cat}）")
        return True, "dry-run"
    try:
        content = export_torrent_bytes(qb_src, info_hash)
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
        qb_dst.torrents_recheck(torrent_hashes=info_hash)
    except Exception as e:                                        # noqa: BLE001
        name = type(e).__name__
        # 409 = 3020 上已有这条种（多半是别的容器辅种抢先加了）⇒ 视为"已在做种"。
        if name == "Conflict409Error":
            print(f"ℹ️  [handoff] 3020 已有该种（409）= 视为已交棒: {info_hash[:12]}…")
            return True, "409 已存在"
        return False, f"add 3020 失败: {name}: {e}"
    print(f"✅ [handoff] 已交棒到 3020（待 3020 校验）: {info_hash[:12]}… category={tcat}")
    return True, "added+recheck"


def _source_category(qb_src, info_hash, dry_run=False):
    """查 3040 上该种的 `category`（隔离判据的来源）。返回 `(分类, 错误)`。

    ★ 查不到该种 ⇒ 返回错误（**不当成"没分类"**），因为"查不到"与"未分类"必须分开
      —— 前者说明种不在 3040（还没发/已被删），此时**绝不能**往下走。
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
        info = qb_src.torrents_info(torrent_hashes=info_hash)
    except Exception as e:                                        # noqa: BLE001
        return "", f"查 3040 分类失败: {type(e).__name__}: {e}"
    if not info:
        return "", f"3040 上找不到该种 {info_hash[:12]}…（已删？）⇒ 不交棒"
    return str(_torrent_field(info[0], "category", "") or ""), ""


def verify_3020_seeding(qb_dst, info_hash):
    """查 3020 上该种是否已"校验通过且在做种"。返回 `(是否就绪, 明细)`。

    ★ 判据（与 NAS-92 §1.5 一致）：`progress >= 1.0` 且 state ∈
      {stalledUP, uploading, forcedUP, queuedUP}。★ 只在这些都成立时才允许删源。
    ★★ 这是"删 SSD"前的**唯一**安全门 —— 门没通过 ⇒ 绝不进 `finalize_source`。
    """
    try:
        info = qb_dst.torrents_info(torrent_hashes=info_hash)
    except Exception as e:                                        # noqa: BLE001
        return False, f"查 3020 失败: {type(e).__name__}: {e}"
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


def finalize_source(qb_src, info_hash, ssd_dir, dry_run=True, own_categories=None):
    """删除 3040 任务 + 删 SSD 副本。**只有在 3020 已确认做种后**才可调用本函数。

    ★★ 严格时序（NAS-92 §1.5）：① 3020 add+recheck 成功 → ② 删 3040 任务
      → ③ 删 SSD 文件。**绝不能"先删 SSD 再交棒"**（3040 做种的文件没了 ⇒ 校验失败）。
      ★ 本函数**假定①已完成**（调用方 `process_one` 用 `verify_3020_seeding` 把门）。
    ★★★ **隔离闸门（第三道）**：删之前**再查一遍** 3040 上该种的分类，
      不命中白名单 ⇒ **拒删**。★ 为什么在删之前还要查：队列可能被外部篡改/残留旧项，
      而删是**唯一不可逆**的一步 ⇒ 宁可多查一次，也不误删用户的种。
    ★ `deleteFiles=False` 删 3040 任务 —— **不**让 3040 顺手删盘；盘由 `_safe_rmtree` 管，
      这样"删任务"和"删盘"是**两个显式动作**，中间有日志可查。
    """
    # ---- 隔离闸门：删之前再确认"这是本项目种的分类"（查 3040 实况）----
    cat, gate_err = _source_category(qb_src, info_hash, dry_run=dry_run)
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
        print(f"🅿️  [handoff][dry-run] 将删 3040 任务 {info_hash[:12]}… + SSD 副本 {ssd_dir}")
        return True
    try:
        qb_src.torrents_delete(delete_files=False, torrent_hashes=info_hash)
        print(f"✅ [handoff] 已删 3040 任务: {info_hash[:12]}…")
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
    ★ 本函数**绝不 raise**（daemon 线程调用）。
    """
    dry_run = config.HANDOFF_DRY_RUN == "true" if dry_run is None else dry_run
    info_hash = item.get("infohash", "")
    ssd_dir = item.get("save_path") or ""
    target = item.get("target_save_path") or ""
    target_cat = item.get("target_category") or ""
    try:
        # ---- 交棒（零拷贝；含隔离闸门）----
        if item.get("state") in ("PENDING", "HANDOFF_ADDED"):
            ok, why = handoff_to_3020(qb_src, qb_dst, info_hash, target, dry_run=dry_run,
                                     target_category=target_cat or None)
            if not ok:
                update_item(info_hash, state="FAILED", last_error=why,
                            first_failed_at=item.get("first_failed_at") or _now())
                return "FAILED"
            update_item(info_hash, state="HANDOFF_ADDED")
            if dry_run:
                return "HANDOFF_ADDED"
            item = dict(item, state="HANDOFF_ADDED")
        # ---- 校验 3020 是否已做种 ----
        ready, why = (True, "dry-run") if dry_run else verify_3020_seeding(qb_dst, info_hash)
        if not ready:
            # 3020 还没校验完 ⇒ 停在这一步，下轮再看（**不**删源）
            update_item(info_hash, state="HANDOFF_RECHECK", last_error=why)
            return "HANDOFF_RECHECK"
        update_item(info_hash, state="HANDOFF_SEEDING")
        # ---- 清盘（★ 只有 3020 已做种才走到这）----
        if finalize_source(qb_src, info_hash, ssd_dir, dry_run=dry_run):
            if not dry_run:
                remove_item(info_hash)
            update_item(info_hash, state="DONE")
            return "DONE"
        update_item(info_hash, state="FAILED", last_error="清盘失败")
        return "FAILED"
    except Exception as e:                                        # noqa: BLE001
        update_item(info_hash, state="FAILED", last_error=f"{type(e).__name__}: {e}")
        return "FAILED"
