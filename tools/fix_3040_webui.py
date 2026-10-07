#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只动 3040 的 WebUI 段：加白名单 + 关失败封禁 + 清掉非真 key 的 APIKey 占位符。
幂等。★ 不含任何凭据；不打印密码。用法（NAS 上）：python3 fix_3040_webui.py <conf路径>"""
import sys

ADD = {
    r"WebUI\AuthSubnetWhitelist": "192.168.0.0/24, 127.0.0.1/32, 172.16.0.0/12",
    r"WebUI\AuthSubnetWhitelistEnabled": "true",
    r"WebUI\MaxAuthenticationFailCount": "0",
}
# 删掉的键：占位 key（非真）；白名单三键先删再插保证幂等
DROP = {r"WebUI\APIKey",
        r"WebUI\AuthSubnetWhitelist",
        r"WebUI\AuthSubnetWhitelistEnabled",
        r"WebUI\MaxAuthenticationFailCount"}

path = sys.argv[1]
with open(path, encoding="utf-8") as f:
    lines = f.read().splitlines()

out, seq, inserted = [], [], False
for ln in lines:
    key = ln.split("=", 1)[0] if "=" in ln else ln
    if key in DROP:
        continue
    out.append(ln)
    if ln.strip() == r"WebUI\Address=*":
        for k, v in ADD.items():
            out.append(f"{k}={v}")
        inserted = True

if not inserted:
    sys.exit("ERR: 没找到 WebUI\\Address=* 锚点，未改动")

with open(path, "w", encoding="utf-8") as f:
    f.write("\n".join(out) + "\n")

# 回读证明
chk = open(path, encoding="utf-8").read()
print("WROTE ok")
for k in ADD:
    print(" ", k, "=", (k + "=") in chk)
print(" APIKey_dropped=", (r"WebUI\APIKey=" not in chk))
print(" Password_PBKDF2_kept=", (r"WebUI\Password_PBKDF2=" in chk))
print(" Username_kept=", (r"WebUI\Username=" in chk))
