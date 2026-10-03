"""只读诊断：列出站点上「我的发布」里标题含关键词的条目。

★ 临时诊断脚本，用完即删；不进仓库、不在 deploy.sh 白名单。
★★ 它**只发 GET**，不发任何 POST / 不上传任何东西。

为什么需要它：`publish.py` 判「成功」的唯一依据是 takeupload.php 回
`Location: ...id=N`。拿不到就报「失败」—— 但那**只说明我们没解析到 ID**，
不说明站点没建种子。实测拿到的是 `HTTP 200` + **空 body**（0 字节），
而「先 INSERT、后面再 fatal error」也会长成这个形状。
⇒ 必须直接查站点，不能用本地库的 `EXISTS` 数反推（那是 ERR-META-03）。

★ 脚本在 /data（= 宿主 compose 目录），代码在镜像的 /app，
  容器 WORKDIR 是 /app ⇒ 得自己把 /app 补进 sys.path。

在 NAS 上运行（**一行**，先 cd 到 compose 目录）:
    cd /volume2/docker_ssd/opencd2others
    sudo docker compose --profile tools run --rm tools python /data/_probe_uploads.py

换关键词: 加 -e PROBE_KEYWORD=别的
"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")

import _bootstrap  # noqa: E402,F401  —— 把 <repo> 与 <repo>/src 都放进 sys.path（源码已移进 src/）
sys.path.insert(0, os.environ.get("MUSOPIA_APP_DIR", "/app"))

from musopia import MusopiaClient

KEYWORD = os.getenv("PROBE_KEYWORD", "叶惠美")


def main():
    client = MusopiaClient()
    uid = client.check_login()
    print("user_id =", uid)
    print("关键词 =", repr(KEYWORD))
    print()

    # ★★ 第一步：先看**翻页原始回执**，再谈"站点上有几条"。
    #    2026-09-22 定性：`iter_my_uploads()` 本身没有翻页 bug（本地用假响应
    #    喂过三种模型验证过）；当前只产出 2 条的真因在站点那一侧，
    #    需要这份逐页观测来定（见 `musopia.iter_my_uploads` 的 docstring）。
    #    ★ 判据怎么看这三行：
    #      · body=0 字节            → 端点干脆没回内容（同 ERR-SITE-01 那个形状）
    #      · 解析=N 但新增=0         → 站点忽略了 page（每页返同一批）或分页错位
    #      · 解析=0 / 新增=0 于首页  → 回执不是我们认得的列表格式（正则没命中）
    print("=" * 62)
    print("【第一步】逐页原始回执（stderr）")
    print("=" * 62)

    rows = []
    for tid, title in client.iter_my_uploads(debug=True):
        rows.append((int(tid), tid, title))

    print()
    print("=" * 62)
    print(f"站点上「我的发布」共 {len(rows)} 条（★ 若远小于库里 site_torrent_id 的条数，"
          f"见上面三行回执定性）")

    hits = [r for r in rows if KEYWORD in r[2]]
    print(f"标题含 {KEYWORD!r} 的：★ {len(hits)} 条")
    for _, tid, title in sorted(hits):
        print(f"    id={tid}  {title}")

    print("-" * 62)
    print("最近 8 条（按 id 倒序，看这些 id 是不是今天冒出来的）：")
    for _, tid, title in sorted(rows, reverse=True)[:8]:
        print(f"    id={tid}  {title[:90]}")


if __name__ == "__main__":
    main()
