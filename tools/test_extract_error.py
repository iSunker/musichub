"""`musopia._extract_error` 单测 —— 纯离线，零请求、零写库、不碰 /data。

★ 为什么值得单测（2026-10-04，`ERR-SITE-10`）：
  `_extract_error` 的失效是**静默的** —— 它抠不出错误 ⇒ 上层把"**有名字的失败**"
  （页面标题就写着「上传失败！」）兜底成"**无名空壳**"（"未拿到重定向 ID…空壳失败页"）。
  ⇒ 排查的人以为"站点什么都不说"，白白去翻站点 bug。
  本次改动：**加读 `<title>`**（只在标题含失败/错误关键词时才取，避免把正常表单页
  的标题误当错误）。

★★ 判据必须能失败（`ERR-META-32`）：每条都配"反例"注释。阴性对照（删掉读 `<title>`
   那段）⇒ 第 2 条必须报红。

★★ 在 Windows 本机跑要带 UTF-8（中文/emoji）：
      PYTHONIOENCODING=utf-8 python tools/test_extract_error.py
"""
import os
import sys

import _bootstrap  # noqa: E402,F401  —— 把 <repo> 与 <repo>/src 放进 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ★ `musopia.py` 顶部 `import config`（要站点 Cookie 等）—— 本机多半没有；
#   本测试只调纯函数，给个假模块顶替（只需"能 import 成功"）。
import types                                                       # noqa: E402
_fake = types.ModuleType("config")
for _k in ("MUSOPIA_COOKIE", "MUSOPIA_PASSKEY", "MUSOPIA_BASE", "DATA_DIR"):
    setattr(_fake, _k, getattr(_fake, _k, ""))
sys.modules.setdefault("config", _fake)

from musopia import _extract_error  # noqa: E402

FAILS = []


def check(name, cond, why):
    if cond:
        print(f"  ✅ {name}")
    else:
        print(f"  ❌ {name} —— 反例：{why}")
        FAILS.append(name)


# ★ 这两个 HTML 是**真实站点响应**的**最小复刻**（正文只有导航/账号栏、零错误文本）：
REAL_FAIL_PAGE = (
    "<html><head><title>音乐乌托邦 :: 上传失败！ - Powered by NexusPHP</title></head>"
    "<body><table id='info_block'><tr><td class='embedded'>首&nbsp;页 论&nbsp;坛 音&nbsp;乐</td>"
    "<td class='text'>欢迎回来, iSunker [ 退出 ] 魔力值: 685,192.5</td></tr></table></body></html>"
)
NORMAL_FORM_PAGE = (
    "<html><head><title>蟹黄堡 :: 发布 - Powered by NexusPHP</title></head>"
    "<body><td class='embedded'>首&nbsp;页 论&nbsp;坛</td></body></html>"
)
CLASSIC_ERR_PAGE = (
    "<html><body><h2>错误：你没有权限发布种子</h2></body></html>"
)


def test_title_fallback():
    print("[title 兜底] 失败页标题含「上传失败」⇒ 抠得出来")
    got = _extract_error(REAL_FAIL_PAGE)
    # ★ 反例：删掉读 `<title>` 那段 ⇒ 这里会得到 ''（老行为）⇒ 必报红
    check("真失败页(标题)非空", got != "",
          "★抠不出 ⇒ 上层会谎报'空壳失败页'（ERR-SITE-10 的回归）")
    check("含「上传失败」字样", "上传失败" in got,
          "★抠到的不是失败字样 ⇒ 诊断信息不对")


def test_normal_page_not_error():
    print("[不误报] 正常表单页标题（蟹黄堡 :: 发布）⇒ 必须返回空")
    got = _extract_error(NORMAL_FORM_PAGE)
    # ★ 反例：若判据写成"标题有字就返回" ⇒ 这里会返回 '蟹黄堡 :: 发布' ⇒ 必报红
    #   （那会把蟹黄堡的 UNKNOWN 误判成 REJECT —— 语义错误）
    check("正常发布页 = 空", got == "",
          "★把正常页标题当错误 ⇒ 蟹黄堡 UNKNOWN 会被误判成 REJECT（发重伤）")


def test_classic_still_works():
    print("[回归] 老选择器(#outer td.text/h2/…)仍有效")
    got = _extract_error(CLASSIC_ERR_PAGE)
    check("h2 里的错误仍抠得到", "权限" in got,
          "★老判据被这次改动弄坏了 ⇒ 回归")


def test_empty():
    print("[边界] 空 body 不炸")
    try:
        check("空串 = 空", _extract_error("") == "", "")
        check("None = 空", _extract_error(None) == "", "")
    except Exception as e:                                          # noqa: BLE001
        check("空输入不炸", False, f"★抛了 {type(e).__name__} ⇒ drain 式调用会中断")


def main():
    test_title_fallback()
    test_normal_page_not_error()
    test_classic_still_works()
    test_empty()
    print()
    if FAILS:
        print(f"❌ {len(FAILS)} 项失败：{FAILS}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
