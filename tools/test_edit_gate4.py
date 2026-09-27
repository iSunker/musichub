"""★ 回归测试：闸门④ 的 **edit 路副本**（2026-09-27 补的那个洞）。

═══════════════════════════════════════════════════════════════════════════
它测什么（一句话）
═══════════════════════════════════════════════════════════════════════════
**`transfer_rule != 'ALLOW'` 的行，一条都不许进 `edit`（`update_metadata.py`）。**

真实起因（2026-09-27 实测，不是假想）：
  闸门④ 原先**只装在发布队列上**（`db.PENDING_PUBLISH_WHERE`），而 `edit` 走
  `db.get_published()` —— 它的 WHERE 里**只有** `site_status='EXISTS'`，完全不知道
  `transfer_rule` 这回事。⇒ 一条 `DENY`（禁止轉載）的行只要**已在站上**且**本地更新过**，
  `edit` 就会照改不误。
  · 实测受害行：`site_torrent_id=15972`（Mary Black，`transfer_rule='DENY'`）
  · 它和另外 15 条**共享同一个 `updated_at`**（同一次批量标脏）⇒ 一起进了 edit 待办
  · 15 条恰好 `ALLOW`（改了无害），**这 1 条 DENY 被按本地内容重写了站点页面**

═══════════════════════════════════════════════════════════════════════════
★ 为什么要**两个**判据（不能只测一个）
═══════════════════════════════════════════════════════════════════════════
① **选择器**（`get_published(require_allow=True)`）拦住 —— 这是"洞补了没有"。
② **探针**（`db.count_stale()`）**也**拦住 —— 这是最容易漏的一半：
   探针是 daemon 判"edit 还有没有活"用的。若只有选择器带闸门、探针不带，
   就会出现：
       探针**算进了** DENY 行（说有活）→ daemon 按 60s 冷却重跑 edit →
       edit **一条都选不出来** → 探针**还说有活** → 无限高频空转，
       而且**不报任何错**，只表现为站点被反复敲。
   ⇒ 实测过这个形状（`_published_where` 上面那段警告就是为它写的）。
   ⇒ **两个判据必须都测**，只补一边比不补更糟。

═══════════════════════════════════════════════════════════════════════════
★ 阴性对照（为什么这个测试"能证伪"，而不是恒真）
═══════════════════════════════════════════════════════════════════════════
光断言"DENY 选不出来"是**不够**的 —— 万一 SQL 整个坏了（比如 `transfer_rule`
拼错成常量、或 WHERE 组装失败返回空），DENY 也"选不出来"，测试照样绿。
⇒ 必须同时断言 **ALLOW 的行选得出来**（阳性对照），并且断言
   **去掉闸门后 DENY 确实选得出来**（阴性对照 = 证明这个夹具**本来能**看见 DENY）。

用法（本机；不需要 .env 的真密钥）:
    QB_HOST=x QB_USER=x QB_PASS=x MUSOPIA_COOKIE=x MUSOPIA_PASSKEY=x \
      PICGO_API_KEY=x python tools/test_edit_gate4.py

★ 它**只用临时库**（`tempfile`），**不碰生产 data.db**（ERR-SQL-01）。
"""
import os
import sqlite3
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8")

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

import db  # noqa: E402

_fail = 0


def chk(label, cond, detail=""):
    global _fail
    ok = bool(cond)
    print(f"  {'✅' if ok else '❌'} {label}" + (f"   —— {detail}" if detail else ""))
    if not ok:
        _fail += 1


def _fresh_db(config_mod):
    """建一个临时库，**用 `db.init_db()` 建真表**，再插几条覆盖各种 rule 的行。

    ★★ 为什么**不自己手写 `CREATE TABLE`**（第一版就是这么写的，当场被
      `no such column: formatted_title` 打脸）：
        `get_published()` 用 `TASK_FIELDS` 选 13+ 列，而手写的建表语句
        **只覆盖了我当时想到的那几列** ⇒ 夹具一跑就报缺列。
        更要紧的是：那种夹具**会随时间漂** —— 以后 `TASK_FIELDS` 加了新列，
        这个测试**会在一个与生产无关的地方失败**（假故障），或者更坏：
        若我只补了够用的那几列，它就**只测了一个比生产窄的口子**（假通过）。
      ⇒ 判据：**夹具的表结构必须来自生产代码**（`db.init_db()`），不来自我的手。
    """
    fd, path = tempfile.mkstemp(suffix=".db", prefix="test_edit_gate4_")
    os.close(fd)
    orig = config_mod.DB_PATH
    config_mod.DB_PATH = path
    try:
        db.init_db()          # ★ 生产同款建表（幂等，随时可加列）
    finally:
        config_mod.DB_PATH = orig

    con = sqlite3.connect(path)
    # ★ stale 的判据是 `site_synced_at IS NULL OR updated_at > site_synced_at`
    #   ⇒ 下面每行都构造成 **stale**，这样"选不选得出来"只由 rule 决定，
    #     不被 stale 搅混（一次只测一个变量）。
    rows = [
        # (hash, name, status, site_status, sid, synced, updated, rule, cover)
        ("h_allow",   "ALLOW 行（应被 edit 选中）",  "SUCCESS", "EXISTS",
         "1001", "2026-09-01 00:00:00", "2026-09-02 00:00:00", "ALLOW",   "c"),
        ("h_deny",    "DENY 行（**不该**被 edit 选中）", "SUCCESS", "EXISTS",
         "1002", "2026-09-01 00:00:00", "2026-09-02 00:00:00", "DENY",    "c"),
        ("h_null",    "rule 空（= 不知道，不该选中）",  "SUCCESS", "EXISTS",
         "1003", "2026-09-01 00:00:00", "2026-09-02 00:00:00", None,      "c"),
        ("h_needc",   "NEED_CONFIRM（拿不准，不该选中）", "SUCCESS", "EXISTS",
         "1004", "2026-09-01 00:00:00", "2026-09-02 00:00:00", "NEED_CONFIRM", "c"),
        ("h_allow2",  "ALLOW 行 2（阳性对照，应选中）", "SUCCESS", "EXISTS",
         "1005", "2026-09-01 00:00:00", "2026-09-02 00:00:00", "ALLOW",   "c"),
    ]
    con.executemany(
        "INSERT INTO tasks (info_hash, torrent_name, status, site_status, "
        "site_torrent_id, site_synced_at, updated_at, transfer_rule, cover_url, mapped_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,'{}')",
        rows,
    )
    con.commit()
    con.close()
    return path


def _sids(rows):
    return sorted(r["site_torrent_id"] for r in rows)


def main():
    # ★ `db.connect()` 读的是 `config.DB_PATH`（不是 db.DB_PATH）⇒ 要打这个补丁。
    #   ★ 打 `config.DB_PATH` 而不是改 `db.connect` —— 后者要给每个被测函数都塞一份，
    #     而且会掩盖"这些函数走的是不是同一个 connect"这件事（那正是要测的）。
    import config
    path = _fresh_db(config)
    orig = config.DB_PATH
    config.DB_PATH = path
    try:
        print("=" * 72)
        print("★ 闸门④（edit 路）回归测试")
        print("=" * 72)
        print(f"  临时库 = {path}")
        print()

        # ── ① 判据：闸门常量与发布侧同源 -------------------------------
        print("① 闸门常量本身")
        chk("db.EDIT_RULE_GUARD 是白名单口径（= 'ALLOW'，不是 != 'DENY'）",
            db.EDIT_RULE_GUARD == "transfer_rule = 'ALLOW'",
            f"实得 {db.EDIT_RULE_GUARD!r}")
        # ★ 与发布侧那句必须是**同一判据**。发布侧写死在 PENDING_PUBLISH_WHERE 里。
        chk("★ 与发布侧（PENDING_PUBLISH_WHERE）的判据**逐字相同**",
            ("AND " + db.EDIT_RULE_GUARD) in db.PENDING_PUBLISH_WHERE,
            "两处各写一份就会漂（ERR-META-03）")

        # ── ② 选择器：get_published(require_allow=True) ------------------
        print()
        print("② 选择器（edit 实际用的那条路）")
        guarded = db.get_published(only_stale=True, require_allow=True)
        g_sids = _sids(guarded)
        chk("★ 选出来的**全是** ALLOW（DENY / 空 / NEED_CONFIRM 一个都不许有）",
            g_sids == ["1001", "1005"], f"实得 {g_sids}")
        chk("DENY 那条（1002）**不在**里面", "1002" not in g_sids)
        chk("rule 空的那条（1003）**不在**里面", "1003" not in g_sids)
        chk("NEED_CONFIRM 那条（1004）**不在**里面", "1004" not in g_sids)

        # ── ③ ★★ 阴性对照：不带闸门时 DENY **必须**选得出来 --------------
        print()
        print("③ ★★ 阴性对照（证明夹具本身能看见 DENY —— 否则上面②是假通过）")
        raw = db.get_published(only_stale=True)   # require_allow 默认 False
        r_sids = _sids(raw)
        chk("不带闸门时 5 条**全**选得出来（含 DENY/空/NEED_CONFIRM）",
            r_sids == ["1001", "1002", "1003", "1004", "1005"], f"实得 {r_sids}")
        chk("★★ 其中**确实有** DENY（1002）⇒ 上面的『选不出来』是闸门造成的，"
            "不是 SQL 坏了",
            "1002" in r_sids)
        chk("⇒ 两个集合**不相等**（否则闸门没起作用）", set(r_sids) != set(g_sids))

        # ── ④ ★★★ 探针 count_stale() 必须**同样**带闸门 ---------------
        print()
        print("④ ★★★ 探针 count_stale()（daemon 用它判 edit 还有没有活）")
        n_probe = db.count_stale()
        chk("★ count_stale() 数出来 = 2（= 只有 ALLOW 那两条）",
            n_probe == 2, f"实得 {n_probe}")
        chk("★★ 探针数 == 选择器条数（**必须相等**，否则 edit 会高频空转）",
            n_probe == len(guarded),
            f"探针 {n_probe} vs 选择器 {len(guarded)} —— 不等就是那个空转形状")
        chk("★ 探针**没有**把 DENY 算进待办（否则 daemon 会以为还有活）",
            n_probe != len(raw), f"（不带闸门时是 {len(raw)} 条）")

        # ── ⑤ 只读路**不受影响**（防止修洞时把 sync_site 弄瞎）----------
        print()
        print("⑤ 只读调用方必须**不受**这个改动影响")
        miss_a = db.get_published(only_missing_id=True)
        miss_b = db.get_published(only_missing_id=False)
        chk("only_missing_id=True 仍是 **0** 条（本夹具的 EXISTS 行都有 id）",
            len(miss_a) == 0, f"实得 {len(miss_a)}")
        chk("★ only_missing_id=False（sync_site 口径）仍是 **5** 条 —— "
            "**包含 DENY**（sync_site 只是回填 id，不写站点内容，不该被拦）",
            len(miss_b) == 5, f"实得 {len(miss_b)}")
        chk("★ 那条 DENY（1002）在 sync_site 口径里**看得见**",
            "1002" in _sids(miss_b))

        # ── ⑥ 点名路也要过闸门 -----------------------------------------
        print()
        print("⑥ --torrent_id 点名路")
        one = db.get_published(torrent_id="1002", require_allow=True)
        chk("★ 点名一条 DENY ⇒ **选不出来**（点名不是后门）", len(one) == 0,
            f"实得 {len(one)}")
        one_ok = db.get_published(torrent_id="1001", require_allow=True)
        chk("点名一条 ALLOW ⇒ 选得出来（阳性对照）",
            len(one_ok) == 1 and one_ok[0]["site_torrent_id"] == "1001")

        # ── ⑦ 两处判据同源的可执行断言 ---------------------------------
        print()
        print("⑦ 发布侧与 edit 侧的闸门**真的是同一个判据**吗（可执行版）")
        # 发布侧：DENY 行的 mapped_json 非空也进不了队列
        con = sqlite3.connect(path)
        n_pub_deny = con.execute(
            "SELECT count(*) FROM tasks WHERE (" + db.PENDING_PUBLISH_WHERE + ")"
        ).fetchone()[0]
        con.close()
        chk("发布队列（PENDING_PUBLISH_WHERE）里 DENY/空/NEED_CONFIRM **一条都没有**",
            n_pub_deny == 0, f"实得 {n_pub_deny}")
        print("     ★ 注意这个 0 是**发布侧**给的（`site_status != 'EXISTS'` 那条"
              "也会把本夹具全排掉），")
        print("       ⇒ 它**不能**单独证明 edit 侧也拦住了 —— 所以④那条断言才是关键。")

    finally:
        config.DB_PATH = orig
        try:
            os.unlink(path)
        except OSError:
            pass

    print()
    print("=" * 72)
    total = 15
    passed = total - _fail
    print(f"结果: {passed}/{total} 通过")
    print("=" * 72)
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())
