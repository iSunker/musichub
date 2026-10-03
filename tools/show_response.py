"""读取 publish_probe.py 落盘的站点响应体，打印去标签文本 + 定位错误文案。

★ 临时诊断脚本，用完即删。
★ 同 publish_probe.py：脚本在 `/data`、代码在 `/app`，得自己把 /app 补进 sys.path。

在 NAS 上运行（**一行**，先 cd 到 compose 目录）:
    cd /volume2/docker_ssd/opencd2others
    sudo docker compose --profile tools run --rm tools python /data/_probe_show.py
"""
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, os.environ.get("MUSOPIA_APP_DIR", "/app"))

import config

PATH = os.path.join(config.DATA_DIR, "last_upload_response.html")

if not os.path.exists(PATH):
    sys.exit(f"★ 文件不存在: {PATH}\n"
             f"  说明 tools/publish_probe.py 还没成功跑过 —— 先跑它。")

body = open(PATH, encoding="utf-8", errors="replace").read()
plain = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body)).strip()

print("=" * 66)
print(f"落盘文件: {PATH}")
print(f"原始长度: {len(body)} 字符")
print("=" * 66)

print("\n--- 去标签全文（前 3000 字）---")
print(plain[:3000])

print("\n--- 含关键词的片段（前后各 80 字）---")
pat = re.compile(r".{0,80}(错误|Error|失败|抱歉|請|请|无权|權限|重复|重複|已有|通知).{0,80}")
hits = [(m.start(), re.sub(r"\s+", " ", m.group(0))) for m in pat.finditer(plain)]
if hits:
    for pos, s in hits[:25]:
        print(f"  @{pos}: {s}")
else:
    print("  （无命中）")

print("\n--- 结构判据：这是错误页还是上传表单页？---")
low = body.lower()
print(f"  含 <form ... takeupload: {'takeupload.php' in low}")
print(f"  含 upload.php:            {'upload.php' in low}")
print(f"  含 takelogin:             {'takelogin' in low}")
print(f"  含 details.php:           {'details.php' in low}")
print(f"  含 '错误' 字样:            {'错误' in body}")
print(f"  含 '禁止' 字样:            {'禁止' in body}")
print(f"  含 '權限'/'权限' 字样:      {('權限' in body) or ('权限' in body)}")

print("\n--- 若像表单页：列出它回填了哪些字段名（看站点要什么）---")
try:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(body, "html.parser")
    f = soup.find("form", action=re.compile("takeupload", re.I))
    if f:
        names = []
        for el in f.find_all(["input", "select", "textarea"]):
            n = el.get("name")
            if n and n not in names:
                names.append(n)
        print("  表单字段:", names)
        # 哪些字段带了 value（= 站点回填的现值）
        withval = [(el.get("name"), (el.get("value") or "")[:60])
                   for el in f.find_all("input") if el.get("name") and el.get("value")]
        print("  回填了值的字段:")
        for n, v in withval[:30]:
            print(f"    {n} = {v!r}")
    else:
        print("  （响应里没有 takeupload 表单 ⇒ 不是表单退回）")
except ImportError:
    print("  （缺 bs4）")
