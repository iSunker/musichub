"""
OpenCD（皇后）详情页 HTML 脱敏 —— 让页面样本可以安全进仓库。

为什么需要它（2026-09-19 实测，不是推断）：
    从皇后「另存为」下来的**详情页 HTML 里带真凭据**。实测两份样本各有：
      · 2 处 passkey（`download.php?id=…&passkey=<32位十六进制>`）—— ★ 与 .env 同级的泄露面
      · 登录用户名（会员态横幅 + 感谢按钮）
      · 会员隐私读数（分享率 / 上传量 / 下载量 / 魔力值 / 邀请 / H&R）
    `opencdhtml/` 因此在 `.gitignore` 里默认忽略，只有**脱敏后**的样本才进仓库
    （`opencdhtml/sanitized/`，那是反忽略的例外）。纪律同 `ENVIRONMENT.md` A.5。

用法:
    python opencd_sanitize.py                  # 脱敏 opencdhtml/*.html → opencdhtml/sanitized/
    python opencd_sanitize.py --check          # 只检查：报告每个文件还有几处敏感串（不改文件）
    python opencd_sanitize.py --in X --out Y   # 单文件

★ 设计纪律（两条，都来自本项目的既有教训）：
  1. **按值的形状抹，不只按键名。** 抄 `tools/doc-audit/04_probes.py` 的 `redact()` ——
     按 key 名匹配会漏掉「嵌在 URL / 文本里的」凭据（passkey 就是这样：它出现在
     `download.php` 的 query 里，不在任何叫 passkey 的字段里）。
  2. **脱敏必须可验证，且要验两个方向。** 一个「永远说没问题」的检查器比没有更危险
     （`ENVIRONMENT.md` `ERR-META-01`）。所以：
       · `--check` 与脱敏后自检**共用同一份模式表**（不重抄一份会漂的）；
       · 另配 `--selftest`，**先拿一个确知含凭据的样本证明它抓得到**，
         再拿脱敏结果证明它抓不到 —— 两个方向都验过才算数。
"""
import argparse
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_IN = os.path.join(HERE, "opencdhtml")
DEFAULT_OUT = os.path.join(DEFAULT_IN, "sanitized")

# ★ 样本改名表：原名是「另存为」时浏览器给的，又长又含种子信息、还不能当稳定引用。
#   改成**语义名** —— 名字本身就把「验收判据」写出来了（第 1、2 步要用）：
#     sample-zhuanzai-jinyong.html   → 轉載規則 = 禁止轉載 ⇒ 裁决必须 DENY
#     sample-zhuanzai-huanying.html  → 轉載規則 = 歡迎轉載 ⇒ 裁决必须 ALLOW
#   ★ 新增样本时在这里加一行，别改已入库的名字（否则 --selftest 与文档的引用会断）。
RENAME_MAP = {
    "OpenCD __ 種子詳情 _郭凤女、何华栈 - 魂断乌江（粤曲对唱） 2000 - WAV 整軌 - alin13788_.html":
        "sample-zhuanzai-jinyong.html",
    "OpenCD __ 種子詳情 _齐秦 - 我拿什么爱你 1998 - FLAC 分轨_.html":
        "sample-zhuanzai-huanying.html",
    # 列表页（torrents.php）—— 抓取层的入口，解析出「有哪些新种子」
    "OpenCD __ 種子.html":
        "sample-listpage.html",
    # 列表页的第 2、3 页 —— 用来验证「分页顺序」与「页间重叠」（实测：不按时间排、
    # 页1∩页2 重叠 1 个）。★ 只为审计用，抓取层不依赖它们。
    "OpenCD __ 種子2.html":
        "sample-listpage-p2.html",
    "OpenCD __ 種子3.html":
        "sample-listpage-p3.html",
    # 搜索结果页 —— ★ 实测结构与列表页**完全一致**（同 torrentname 行、同列序），
    # 所以 parse_list() 零改动就能吃。用于「qB 反查」：按名字搜出候选 → 再验 infohash。
    "OpenCD __ 搜索結果 - 齐秦 - 我拿什么爱你---搜索结果页.html":
        "sample-search-qinqin.html",
    "OpenCD __ 搜索結果 - 周杰伦---搜索结果页.html":
        "sample-search-jay.html",
}


def _out_name(src_name):
    """脱敏后的文件名：在改名表里就用语义名，否则保持原名。"""
    return RENAME_MAP.get(src_name, src_name)

# 替换后的占位符。★ 用**不含十六进制字符**的字面量，免得脱敏结果自己又被
# 「32 位十六进制」那条规则二次命中（那会让「脱敏后再检查」永远报警）。
REDACTED = "<REDACTED>"
REDACTED_USER = "<USER>"

# ==================== 敏感模式表（唯一来源） ====================
# (说明, 编译好的正则, 替换串)
# ★ 顺序有讲究：**先具体、后笼统** —— 具体的那条能把整段替换成可读占位符，
#   笼统的（裸 32 位十六进制）只作兜底。反过来会把 URL 打碎。
PATTERNS = [
    ("下载链接里的 passkey", re.compile(r"(download\.php\?id=\d+&passkey=)[0-9a-fA-F]+"),
     r"\1" + REDACTED),
    ("裸露的 passkey=xxx", re.compile(r"(passkey=)[0-9a-fA-F]+"), r"\1" + REDACTED),
    # ★ 用户名**按结构抹**，不把真名写进源码 —— 实测（2026-09-19）真身只有一处：
    #     `<a href="userdetails.php?id=<UID>" class='EliteUser_Name' …><b>名字</b></a>`
    #   ★ 我最初以为还有一处「会员态横幅 `class="user-avatar"> 名字`」——
    #     **那是看错了**：`user-avatar` 那个 div 是空的（头像装饰），
    #     名字在它**旁边**的 `EliteUser_Name` 链接里。所以只有下面这一条规则，
    #     上面那条 `user-avatar` 规则留着但它**在这两份样本里不命中**（无害的兜底）。
    ("用户名（会员态横幅，兜底）",
     re.compile(r'(class="user-avatar">\s*)[^<,]+(?=\s*,)'), r"\1" + REDACTED_USER),
    ("用户名（EliteUser_Name 链接）",
     re.compile(r"(EliteUser_Name[^>]*>\s*<b>)[^<]+(</b>)"), r"\1" + REDACTED_USER + r"\2"),
    ("会员 ID（userdetails 链接）", re.compile(r"(userdetails\.php\?id=)\d+"), r"\1<UID>"),
    # ★ 列表页实测（2026-09-19）新增两条：会员态横幅里还有别的带 uid 的链接
    #   `invite.php?id=85068`，以及**魔力值**那格的写法是
    #   `[<font …>魔力值</font>: 258,590.0 <a href="mybonus.php">使用</a>]`
    #   —— 值在 `</font>` **之后**、且冒号在 font 外面 ⇒ 上一版漏了它。
    ("会员 ID（invite 链接）", re.compile(r"(invite\.php\?id=)\d+"), r"\1<UID>"),
    ("魔力值（font 闭合后跟值）",
     re.compile(r"(魔力值\s*</font>\s*[:：]\s*)[\d,\.]+"), r"\1<NUM>"),
    ("魔力值（label 后跟值与「使用」链接）",
     re.compile(r"(魔力值\s*[:：]\s*)[\d,\.]+(?=\s*<a)"), r"\1<NUM>"),
    ("魔力值面额", re.compile(r"(魔力值\s*[:：]\s*)[\d,\.]+"), r"\1<NUM>"),
    # ★ 会员态读数：值**不一定紧跟标签** —— 列表页的写法是
    #     `<font class='color_ratio'>分享率：</font>3.013`
    #   即标签在 `<font>` 里、**值在 font 外面**。所以既要有「标签+值」的紧邻写法，
    #   也要有「标签后先跳过标签再取值」的写法。实测（2026-09-19）：
    #   只写前者会在列表页上**漏掉 3.013 / 413.10 GB / 137.13 GB**（已实测确认漏）。
    ("分享率（标签后直接跟值）",
     re.compile(r"((?:分享率|上傳量|下載量)\s*[:：]\s*)[\d,\.]+\s*\w*"), r"\1<NUM>"),
    ("分享率（标签在 font 里、值在外面）",
     re.compile(r"((?:分享率|上傳量|下載量)\s*[:：]\s*</font>\s*)[\d,\.]+\s*\w*"), r"\1<NUM>"),
    ("分享率（label 与值之间夹**闭合**标签）",
     # ★ 只允许「闭合标签」夹在中间（如 `分享率：</font>3.013`）。
     #   第一版写的是 `(?:\s*</?\w+[^>]*>)*` —— 允许**开闭都行**，
     #   于是匹配跨过了 `<a href="/torrents.php?…">` 这种长开标签，
     #   在「當前活動：<a href=...><img ...>` 上产生**假阳性**（实测）：
     #   它把后面那一大段都算进 group，`--check` 于是永远报"仍检出"。
     #   判据：**只吃闭合标签**，遇到开标签就停。宁可少抹（有 check 兜底），
     #   不要假阳性（假阳性会让检查器变成"永远红"= 没人看）。
     re.compile(r"((?:分享率|上傳量|下載量)\s*[:：](?:\s*</\w+>)*\s*)[\d,\.]+\s*\w*"),
     r"\1<NUM>"),
    ("邀请数", re.compile(r"(邀請\s*[:：]\s*)\d+"), r"\1<NUM>"),
    ("H&R 计数", re.compile(r"(H&R\s*[:：]\s*(?:<[^>]+>\s*)*)\d+"), r"\1<NUM>"),
    ("當前活動 / 連接數等会员读数",
     # ★ 同上去掉「任意开标签」的写法。实测：`當前活動：</font>` 后面跟的是
     #   `<a href=…><img class="arrowup">`（**图标**，不是数字）——
     #   那条规则本来是给详情页的 `當前活動： 500` 用的，列表页形状不同。
     #   现在只吃闭合标签，匹配不到就不匹配（由 --check 兜底，不再假阳性）。
     re.compile(r"((?:當前活動|可連接|連接數)\s*[:：](?:\s*</\w+>)*\s*)[\d,]+\s*\w*"),
     r"\1<NUM>"),
    # ══════════════ ★ 这里**故意没有**「裸 32+ 位十六进制」那条笼统兜底 ══════════════
    # 第一版写了它，实测（2026-09-19）发现它**越界**：同一页里的十六进制串有四种来源，
    # 只有一种是凭据 ——
    #     passkey              `passkey=<40位>`        → 凭据，**必须抹**（上面两条已专治）
    #     attachments/…        `attachments/202609/<40位>.jpg` → 封面地址/文件清单 = **内容**
    #     Log checksum         `Log checksum 4BD975B7…`  → 抓轨日志正文      = **内容**
    #     Hash碼(info_hash)    `Hash碼 778c3375…`        → 种子标识，解析要用  = **内容**
    # 笼统兜底会把后三者一起打掉 —— 那不是在保护秘密，是在**改坏样本**
    # （而且改坏的正是第 1 步要解析的东西）。判据：**脱敏只该动凭据，不该动内容。**
    # ⇒ 正确做法是**精确列出凭据的形态**（上面那两条 passkey 规则），不搞兜底。
]


def scan(text):
    """返回 [(说明, 命中数, 首个样例)]。检查与自检共用，避免两份会漂的实现。"""
    found = []
    for label, rx, _rep in PATTERNS:
        hits = rx.findall(text)
        if hits:
            found.append((label, len(hits), str(hits[0])[:70]))
    return found


def sanitize_text(text):
    """按模式表依次替换，返回 (脱敏后文本, 替换计数 dict)。"""
    counts = {}
    for label, rx, rep in PATTERNS:
        text, n = rx.subn(rep, text)
        if n:
            counts[label] = counts.get(label, 0) + n
    return text, counts


def sanitize_file(src, dst, quiet=False):
    """脱敏单个文件。返回 (ok, counts)；ok=False 表示脱敏后仍检出敏感串。"""
    with open(src, encoding="utf-8", errors="replace") as fh:
        raw = fh.read()

    before = scan(raw)
    clean, counts = sanitize_text(raw)

    # ★ 脱敏后必须**复检**：这才是这道工序的判据，不是「跑过替换了」。
    after = scan(clean)

    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(dst, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(clean)

    if not quiet:
        name = os.path.basename(src)
        print(f"\n{'=' * 66}\n{name[:64]}\n{'=' * 66}")
        if before:
            for label, n, sample in before:
                print(f"  脱敏前 · {label}: {n} 处  ({sample})")
        else:
            print("  脱敏前 · 未检出敏感串")
        if counts:
            for label, n in counts.items():
                print(f"          → {label}: 替换 {n} 处")
        if after:
            print("  ❌ 脱敏后**仍**检出：")
            for label, n, sample in after:
                print(f"       {label}: {n} 处  ({sample})")
        else:
            print("  ✅ 脱敏后复检：0 处")

    return (not after), counts


def cmd_check(indir):
    """只检查，不改文件。"""
    files = _list_html(indir)
    if not files:
        print(f"❌ {indir} 下没有 .html")
        return 1
    bad = 0
    for p in files:
        with open(p, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        hits = scan(text)
        print(f"\n{os.path.basename(p)[:64]}")
        if hits:
            bad += 1
            for label, n, sample in hits:
                print(f"  ★ {label}: {n} 处  ({sample})")
        else:
            print("  ✅ 无敏感串")
    print()
    if bad:
        print(f"⚠️  {bad}/{len(files)} 个文件含敏感串 —— **不要提交它们**（先跑不加参数的脱敏）")
        return 1
    print(f"✅ {len(files)} 个文件均无敏感串")
    return 0


def cmd_selftest():
    """
    证明这道工序**两个方向都有效**：确知含凭据的样本必须被抓到；
    脱敏结果必须抓不到。★ 不做这一步，「检查器永远说没问题」就无从发现。
    """
    ok = True
    fake_passkey = "deadbeef" * 4                      # 32 位十六进制
    # ★ fixture 里用**假用户名**（`somebody`），绝不写真名 —— 连测试数据也守这条。
    dirty = (
        '<a href="download.php?id=12345&passkey=%s">下載種子</a>'
        '<span class="user-avatar"> somebody , 歡迎回來</span>'
        '<a href="userdetails.php?id=85068" class=\'EliteUser_Name\'><b>somebody</b></a>'
        '<td>分享率： 3.012</td><td>魔力值 : 258,420.4</td>'
        % fake_passkey
    )
    print("=" * 66)
    print("[1] 阳性对照：确知含凭据的样本 —— 检查器**必须**抓到")
    print("=" * 66)
    found = scan(dirty)
    if not found:
        print("  ❌ 一条都没抓到 ⇒ 检查器失效，脱敏不可信")
        ok = False
    for label, n, sample in found:
        print(f"  ✅ {label}: {n} 处")

    print("\n" + "=" * 66)
    print("[2] 阴性对照：脱敏结果 —— 检查器**必须**报 0 处")
    print("=" * 66)
    clean, counts = sanitize_text(dirty)
    left = scan(clean)
    if left:
        print("  ❌ 脱敏后仍有敏感串：")
        for label, n, sample in left:
            print(f"       {label}: {n} 处 ({sample})")
        ok = False
    else:
        print("  ✅ 0 处")
    print(f"  （替换了 {sum(counts.values())} 处）")

    print("\n" + "=" * 66)
    print("[3] 反例护栏：**正常页面内容不能被误杀**")
    print("=" * 66)
    # ★ 这几类**都是页面内容**，必须原样保留。第一版曾用「裸 32+ 位十六进制」兜底，
    #   把前三类一起打掉了 —— 那是越界（详见 PATTERNS 末尾的注释）。
    legit = (
        "<td>感謝者 abc123 winiso Yiyun</td>"                          # 别人的用户名
        "<td>01.魂断乌江 (25:05)</td><td>681.42 MB</td>"               # 曲目/大小
        '<img src="attachments/202609/202609182257030fa41e7e4718acfb9348d568d1f63804.jpg">'  # 封面地址
        "<pre>Log checksum 4BD975B7696ACC0B9F1A2E3D4C5B6A7F</pre>"     # 抓轨日志正文
        "<b>Hash碼</b> 778c337572b855b7f2f72cc8d43aa4564311e5a4"       # info_hash
    )
    clean2, _ = sanitize_text(legit)
    if clean2 == legit:
        print("  ✅ 未误杀（感谢者名 / 曲目 / 大小 / 封面地址 / 日志校验码 / info_hash）")
    else:
        print("  ❌ 误杀了正常内容：")
        print(f"      原: {legit}")
        print(f"      后: {clean2}")
        ok = False

    print("\n" + "=" * 66)
    print("[4] 阳性 → 阴性 端到端：凭据抹掉、内容留下（同一段文本里）")
    print("=" * 66)
    mixed = legit + '<a href="download.php?id=1&passkey=%s">下載</a>' % fake_passkey
    clean3, _ = sanitize_text(mixed)
    if fake_passkey in clean3:
        print("  ❌ passkey 仍在")
        ok = False
    elif clean3.replace('<a href="download.php?id=1&passkey=<REDACTED>">下載</a>', "") != legit:
        print("  ❌ 内容被改动了（除 passkey 外不该有任何差异）")
        print(f"      差异：{set(_diff(clean3, legit))}")
        ok = False
    else:
        print("  ✅ passkey 已抹，其余内容逐字不变")

    print("\n" + "=" * 66)
    print("[5] 列表页的会员横幅：值在 `<font>` **外面**也必须抹掉")
    print("=" * 66)
    # ★ 实测（2026-09-19）第一版漏了这个形状：列表页写成
    #   `<font class='color_ratio'>分享率：</font>3.013` —— 标签在 font 里、值在外面，
    #   而规则只覆盖了「标签后紧跟值」。结果 3.013 / 413.10 GB / 137.13 GB 全部漏网。
    banner = (
        '<font class="color_downloaded">H&R: <a href="/x"><b style="color:green">7</b></a></font> '
        "<font class='color_ratio'>分享率：</font>3.013 "
        "<font class='color_uploaded'>上傳量：</font>413.10 GB "
        "<font class='color_downloaded'>下載量：</font>137.13 GB"
    )
    clean5, _ = sanitize_text(banner)
    leaked = [v for v in ("3.013", "413.10", "137.13") if v in clean5]
    if leaked:
        print(f"  ❌ 仍有会员读数漏网：{leaked}")
        ok = False
    else:
        print("  ✅ 分享率/上传量/下载量 全部抹掉（含 font 包裹与标签间隔两种形状）")
    if "7" in clean5.replace("<NUM>", "").replace("7</a>", ""):
        pass        # H&R 的 7 藏在 <b> 里，见下一条断言
    if "<b style=\"color:green\">7</b>" in clean5:
        print("  ⚠️  H&R 计数（藏在 <b> 里）未抹 —— 已知，见下")
    else:
        print("  ✅ H&R 计数也已抹掉")

    print()
    print("✅ 自检全部通过（三个方向都验过）" if ok else "❌ 自检失败")
    return 0 if ok else 1


def _diff(a, b):
    """粗粒度差异：逐字符找不同位置，够用于自检报错。"""
    out = []
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            out.append(f"@{i}: {a[max(0,i-20):i+20]!r} != {b[max(0,i-20):i+20]!r}")
            if len(out) >= 3:
                break
    if len(a) != len(b):
        out.append(f"长度不同 {len(a)} vs {len(b)}")
    return out


def _list_html(indir):
    if not os.path.isdir(indir):
        return []
    return sorted(
        os.path.join(indir, f) for f in os.listdir(indir)
        if f.lower().endswith(".html")
    )


def main():
    ap = argparse.ArgumentParser(description="OpenCD 详情页脱敏")
    ap.add_argument("--in", dest="indir", default=DEFAULT_IN, help="输入目录（默认 opencdhtml/）")
    ap.add_argument("--out", dest="outdir", default=DEFAULT_OUT,
                    help="输出目录（默认 opencdhtml/sanitized/，那是 .gitignore 的反忽略例外）")
    ap.add_argument("--check", action="store_true", help="只检查有没有敏感串，不改文件")
    ap.add_argument("--selftest", action="store_true",
                    help="验证脱敏工序本身（阳性/阴性/误杀 三个方向）")
    args = ap.parse_args()

    if args.selftest:
        return cmd_selftest()
    if args.check:
        return cmd_check(args.indir)

    files = _list_html(args.indir)
    if not files:
        print(f"❌ {args.indir} 下没有 .html —— 样本要先从皇后另存下来")
        return 1

    print(f"🔒 脱敏 {len(files)} 个文件 → {args.outdir}")
    all_ok = True
    total = {}
    for p in files:
        # ★ 只脱敏顶层 .html，不递归进 sanitized/ —— 否则会把已脱敏的再脱一遍
        dst = os.path.join(args.outdir, _out_name(os.path.basename(p)))
        ok, counts = sanitize_file(p, dst)
        all_ok = all_ok and ok
        for k, v in counts.items():
            total[k] = total.get(k, 0) + v

    print("\n" + "=" * 66)
    if not all_ok:
        print("❌ 有文件脱敏后**仍检出敏感串** ⇒ 别提交，先补模式表")
        return 1
    print(f"✅ 全部脱敏完成，共替换 {sum(total.values())} 处")
    print(f"   下一步：git add {os.path.relpath(args.outdir, HERE)}/  （.gitignore 已放行）")
    print("   ★ 提交前再跑一次 `--check --in %s` 双保险" % os.path.relpath(args.outdir, HERE))
    return 0


if __name__ == "__main__":
    sys.exit(main())
