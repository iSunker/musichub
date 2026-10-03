"""tools/ 脚本的路径引导 —— **一行**把仓库根与 `src/` 都放进 `sys.path`。

═══════════════════════════════════════════════════════════════════════════
为什么需要它（2026-10-06）
═══════════════════════════════════════════════════════════════════════════
源码（28 个 .py）已从仓库根移进 `src/`（本机仓库顶层更清爽；`Dockerfile` 用
`COPY src/*.py ./` 保持**镜像内 `/app` 平铺不变**）。于是**本机侧**跑 tools 时，
`sys.path` 里必须**同时**有：

  · `<repo>`       —— 让 `import tools.xxx` / 读 `.deploy.nasrc`、`counts.json` 等根级文件成立；
  · `<repo>/src`   —— 让 `import config` / `import db` / `import descr` / `import opencd` 成立。

旧的 tools 脚本各自只塞了一个根（写法还分三种：`dirname(dirname(abspath(__file__)))`、
`Path(__file__).resolve().parent.parent`、硬编码 `/app`）⇒ 移动后**必然**漏一个。
本模块把"两个都塞"收成一处，免得再各写一套（`ERR-META-03`：同一件事别有多份实现）。

用法（放在任何 `import config/db/descr/...` **之前**）：

    import _bootstrap        # noqa: F401   （tools/ 自己就在 sys.path 上）

★ 容器内（代码在 `/app` 平铺、`tools/` 在 `/app/tools`）跑时，`<repo>/src` 不存在，
  本模块会自动跳过（不报错）—— 那种布局由 `Dockerfile` 的 `COPY src/*.py ./` 保证。
"""
import os
import sys

# tools/_bootstrap.py → tools/ → <repo>
_TOOLS = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_TOOLS)

# 候选根目录：本机（<repo> 与 <repo>/src 分开）与容器（都在 /app 平铺）。
_CANDS = [
    os.environ.get("MUSOPIA_REPO", ""),          # 显式覆盖优先（同 nas_health.py 的思路）
    _REPO,                                       # <repo>
    os.path.join(_REPO, "src"),                  # <repo>/src
    os.environ.get("MUSOPIA_APP_DIR", "/app"),   # 容器内（平铺）
]

for _p in reversed(_CANDS):                      # reversed ⇒ 靠前的最后 insert(0,..) ⇒ 优先
    if _p and os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)
