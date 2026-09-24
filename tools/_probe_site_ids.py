"""只读诊断：验证 `tasks.site_torrent_id` 那个号段到底指向站点上的哪些种子。

═══════════════════════════════════════════════════════════════════════════
要回答的问题（2026-09-23）
═══════════════════════════════════════════════════════════════════════════
本地库里 269 条 `site_torrent_id` 落在号段 **14611–17100**，
而皇后真实号段是 **196636–201008**（见 `SESSION-NOTES.md`）。

⇒ 那 14611 那个号**在站点上到底是什么**？
   · 若是我们那条「黄清元 - 金钻名曲珍藏」⇒ 号段可信，`edit` 那条路能走；
   · 若是**别的种子**/已被回收 ⇒ 那 222 条**绝不能**送去 `edit`（不可逆的对外写）。

★ 本机做不到：实测 `www.musopia.vip` **全站要登录**，
  `details.php?id=1` 与 `id=99999999` 回的都是同一个登录页（字节数几乎一样）⇒ 判据不可用。
  ⇒ 只能带着**生产 Cookie** 在容器内做。

═══════════════════════════════════════════════════════════════════════════
★★ 只读保证（本脚本绝不改站点）
═══════════════════════════════════════════════════════════════════════════
- 只调 `client.get(...)`（走 `MusopiaClient`），即**只发 GET**；
- 复用的是 `fetch_edit_form()` —— 它**只读** `edit.php`，**不提交** `takeedit.php`；
  ★ 注意：`fetch_edit_form()` 的返回值（fields）**本脚本只读不用**，绝不 POST 出去。
- **不写本地库**：不调 db.save_task / set_site_id / 任何写口。
- 每个请求之间由 `MusopiaClient` 自带的 `_throttle()` 限速（默认 1.5s）。

在 NAS 上运行（**一行**，先 cd 到 compose 目录）:
    cd /volume2/docker_ssd/musopia_script2
    sudo docker compose --profile tools run --rm tools python /data/_probe_site_ids.py

★ 为什么要放在 /data（= 宿主 compose 目录）而不是 tools/：
  `tools/` 既不在 Dockerfile 的 COPY 里、也不在 deploy.sh 白名单里
  ⇒ 容器里根本没有 tools/ 这个目录。而 `.dockerignore` 已有 `_probe_*.py` 规则，
    所以这类**临时诊断件**照惯例直接放 compose 目录、运行时走 `/data/...`。

换 id 列表: 加 -e PROBE_IDS="14611,14612,14622"
"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, os.environ.get("MUSOPIA_APP_DIR", "/app"))

import config
import db
from musopia import MusopiaClient, SiteError, LoginExpired

# 默认探这几个：
#   14611  —— 最早那条（库里写 黄清元 - 金钻名曲珍藏 1998 FLAC）
#   14612  —— 库里写 群星 - 醉民乐 2011
#   14622  —— ★ 被**两条不同种子共用**的那个（库里是 Hana Hope - blue hour 两个限定盘）
#   17096  —— 库里写 周杰伦 - 叶惠美（提示词里"是否重复"那条盯的就是它）
#   17100  —— 该号段的最大值
DEFAULT_IDS = "14611,14612,14622,17096,17100"
IDS = [s.strip() for s in os.getenv("PROBE_IDS", DEFAULT_IDS).split(",") if s.strip()]


def main():
    print("=" * 74)
    print("★ 只读诊断：site_torrent_id 号段在站点上指向什么")
    print("=" * 74)
    print(f"  站点 MUSOPIA_URL = {config.MUSOPIA_URL}")
    print(f"  探测 id 列表     = {IDS}")
    print("  ★ 全程只发 GET，不提交任何表单、不改本地库")
    print()

    client = MusopiaClient()
    try:
        uid = client.check_login()
    except (SiteError, LoginExpired) as e:
        sys.exit(f"❌ 登录失败：{e}")
    print(f"  ★ check_login() 返回 uid = {uid}")
    print(f"    （★ 判据：应为 85068 —— 那才是我们真正的皇后 uid；"
          f"会话里出现过 10427 是**别人的号**，见 ERR-SITE-02）")
    print()

    # 本地库里这些 id 记的是什么（对照用；只读）
    conn = db.connect()
    rows = {}
    for tid in IDS:
        r = conn.execute(
            "SELECT info_hash, formatted_title, torrent_name, opencd_tid, "
            "site_status, site_synced_at FROM tasks WHERE site_torrent_id = ?",
            (tid,)).fetchall()
        rows[tid] = r
    conn.close()

    print("=" * 74)
    print("逐 id 对照：本地库里记的标题  vs  站点 edit.php 里的标题")
    print("=" * 74)
    for tid in IDS:
        local = rows.get(tid) or []
        print(f"\n── id={tid} " + "─" * 52)
        if not local:
            print("   本地库：**没有**任何行用这个 site_torrent_id")
        for r in local:
            print(f"   本地库：hash={r[0][:12]}  opencd_tid={r[3] or '-'}")
            print(f"           标题={ (r[1] or r[2] or '')[:66] }")

        # ★ 只读：GET edit.php，把表单值解析出来（**不提交**）
        # ★★ `fetch_edit_form()` 返回的是 **`(fields, snapshot)` 二元组**，不是 dict
        #    （这个 bug 是**在本机先用假表单试过**才抓到的 —— 否则会在 NAS 上崩掉、
        #      白费你一轮往返。判据要先拿已知样本校验，ERR-META-01）
        try:
            fields, _snapshot = client.fetch_edit_form(tid)
        except LoginExpired as e:
            sys.exit(f"❌ Cookie 失效：{e}")
        except SiteError as e:
            print(f"   站点  ：❌ {e}")
            print("           ⇒ 这个 id 要么不存在、要么无编辑权限")
            continue

        site_name = fields.get("name") or ""
        print(f"   站点  ：✅ edit.php 打得开")
        print(f"           name(标题) = {site_name[:70]!r}")
        print(f"           id 字段回显 = {fields.get('id')!r}")

    print()
    print("=" * 74)
    print("★ 判据怎么读")
    print("=" * 74)
    print("  · 站点 name 与本地库标题**对得上** ⇒ 号段可信，edit 那条路能走")
    print("  · 站点 name 是**别的种子** ⇒ 那 222 条绝不能送去 edit（会改错东西）")
    print("  · id=14622 那两条共用的行，站点只会回**一个**标题 ⇒ 说明两行指向同一粒种子")
    print()
    print("★ 本脚本没有修改任何东西（站点与本地库都是只读）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
