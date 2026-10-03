"""一次性诊断：真发一条 takeupload，把站点原始响应体落盘并粗筛错误文案。

★ 临时诊断脚本，用完即删；不进仓库、不在 deploy.sh 白名单。
★ 它在**被显式运行时**才发请求（无 import 副作用），且只发一条。

★ 脚本本体在 `/data`（= 宿主 compose 目录，compose 里 bind mount 过来的），
  而代码在镜像的 `/app`。容器 WORKDIR 是 `/app`，`python /data/xxx.py` 时
  Python 只把**脚本所在目录**（/data）放进 sys.path，**不会**把 cwd 放进去
  ⇒ 必须自己把 /app 补上，否则 `import config` 直接 ModuleNotFoundError。

在 NAS 上运行（**一行**，先 cd 到 compose 目录）:
    cd /volume2/docker_ssd/opencd2others
    sudo docker compose --profile tools run --rm tools python /data/_probe_publish.py
"""
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")

# ★ 必须在 import config 之前：让脚本能从 /data 跑起来还能 import 到 /app 的模块
import _bootstrap  # noqa: E402,F401  —— 把 <repo> 与 <repo>/src 都放进 sys.path（源码已移进 src/）
sys.path.insert(0, os.environ.get("MUSOPIA_APP_DIR", "/app"))

import config
import db
import descr
from musopia import MusopiaClient

KEYWORD = os.getenv("PROBE_KEYWORD", "叶惠美")


def main():
    client = MusopiaClient()
    print("Cookie user_id =", client.check_login())

    rows = [t for t in db.get_pending_publish()
            if KEYWORD in (t.get("formatted_title") or "")]
    if not rows:
        sys.exit(f"待发布队列里没有含 {KEYWORD!r} 的记录")
    task = rows[0]
    print("目标:", task["formatted_title"])

    fields = descr.build_form_fields(task)
    tpath = task["torrent_file"]
    print("种子文件:", tpath, "| 存在:", os.path.exists(tpath))

    with open(tpath, "rb") as fh:
        files = {"file": (os.path.basename(tpath), fh, "application/x-bittorrent")}
        r = client.post("/takeupload.php", data=fields, files=files,
                        headers={"Referer": client._url("/upload.php"),
                                 "Origin": client.base},
                        allow_redirects=False)

    print("=" * 62)
    print("HTTP", r.status_code)
    print("Location:", repr(r.headers.get("Location")))
    print("Content-Type:", repr(r.headers.get("Content-Type")))
    body = r.text or ""
    print("响应体长度:", len(body))
    print("=" * 62)

    out = os.path.join(config.DATA_DIR, "last_upload_response.html")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(body)
    print("已落盘:", out)

    # ---------- 粗筛候选文案 ----------
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(body, "html.parser")
    print("\n--- 候选文案 ---")
    seen = set()
    for sel in ("#outer table td.text", "td.embedded", "h1", "h2", "h3",
                ".error", "#message", "font[color=red]"):
        for el in soup.select(sel):
            tx = re.sub(r"\s+", " ", el.get_text(" ", strip=True))
            if tx and tx not in seen:
                seen.add(tx)
                print(f"  [{sel}] {tx[:220]}")

    plain = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body)).strip()
    print("\n--- 去标签前 1200 字（判「错误页」还是「上传表单页」）---")
    print(plain[:1200])

    # ---------- 判据：是错误页还是表单页？ ----------
    print("\n--- 形态判据 ---")
    has_form = bool(soup.find("form", action=re.compile("takeupload", re.I)))
    print(f"  含 takeupload 表单: {has_form}  "
          f"（True ⇒ 站点把上传表单退回给你 = 校验没过，不是权限问题）")


if __name__ == "__main__":
    main()
