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

★ 为什么**照旧**放在 /data（= 宿主 compose 目录）而不是 tools/：

  ⚠️ 这段原先写的是「`tools/` 既不在 Dockerfile 的 COPY 里、也不在 deploy.sh
     白名单里 ⇒ 容器里根本没有 tools/」—— **这句已经过期，别再照抄**
     （`ERR-META-04`：改了行为要同时改文档；这里改的是**部署行为**）。

  事实分两半，**必须都说清**，否则会从"以为没有"翻车成"以为有了"：

  · **代码侧已修**（2026-09-26，`ERR-DOCKER-08`）：`.dockerignore` 原先有一行
    裸 `tools` ⇒ 整个目录不进构建上下文 ⇒ `COPY tools/ ./tools/` **静默失效**。
    现在 `.dockerignore` 只排 `tools/doc-audit` 与两个本机状态文件，
    `Dockerfile` 里有 `COPY tools/ ./tools/`，`deploy.sh` 白名单也有 `tools`。
  · **但镜像得重建才生效**：代码进了仓库**不等于**容器里有那个目录。
    ⇒ 在**没跑过 `up -d --build` 的容器**里，`tools/` **仍然不存在**。

  ★ 判据（别猜，去问）：`sudo docker compose run --rm tools ls tools/`
    有输出 ⇒ 走 `tools/...` 也行；报 `No such file or directory` ⇒ 走 `/data/...`。
  ★ 本脚本仍推荐 `/data/...`：它对**两种容器状态都成立**，不依赖"镜像新不新"。
    临时诊断件放 compose 目录也是既有惯例（`.dockerignore` 有 `_probe_*.py` 规则）。

★ 本脚本已验证能跑（2026-09-26 实测）：两次探测共 10 个 id，
  8 个老号段回「找不到 takeedit 表单」、1 个新号段（15972）打得开且标题对得上。

换 id 列表: 加 -e PROBE_IDS="14611,14612,14622"

★★ 未结项（2026-09-26，别当成已解决）：实测 `check_login()` 两次都返回
  **uid = 10427**，而本脚本下面那行判据写的是「应为 85068」（`ERR-SITE-02`）。
  两者不一致 ⇒ **要么我们在用别人的账号、要么 85068 那个判据本身过期了**
  —— 这两种情况的修法**完全不同**。在用户确认「10427 是不是本人的号」之前，
  **不要**据此改动任何东西，也不要把它读成"权限不足"（那只是其中一种可能）。
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
    # ★★ 2026-09-26：这里原先无条件断言「应为 85068」—— 那是**把判据当结论**。
    #    实测两次都是 **10427**，而脚本却仍印「应为 85068」，会让人以为
    #    "差得远 ⇒ 一定是权限问题"。★ 实际有两种可能，修法完全不同：
    #      (a) 我们在用**别人的**账号（严重，该停手）
    #      (b) 85068 那个判据**本身过期**（换过账号，判据没跟着改）
    #    ⇒ 现在只**如实报数**，把"这属于哪种"交给用户确认（`ERR-SITE-02`）。
    if str(uid) == "85068":
        print("    （与 ERR-SITE-02 记的 85068 一致）")
    else:
        print("    ⚠️ 与 ERR-SITE-02 记的 85068 **不一致** —— 有两种可能，"
              "**修法不同**：")
        print("       (a) 本 cookie 是**别人的账号** ⇒ 严重，先停手查凭据来源")
        print("       (b) 85068 那条判据**过期**（换过账号）⇒ 改判据即可")
        print("       ★ 在确认之前**别**把它读成『就是权限不足』（那只是其中一种）。")
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
