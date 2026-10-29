#!/usr/bin/env python3
"""把代码、测试和文档里的机器人名「小助手」换成你的机器人微信昵称（群里 @ 它时用的那个名字）。
默认只预览；加 --apply 才写入。改完跑一遍 tests/run_all.sh。
  python scripts/rename_bot.py 阿福            # 预览
  python scripts/rename_bot.py 阿福 --apply    # 写入
"""
import sys
from pathlib import Path

OLD = "小助手"
BASE = Path(__file__).resolve().parent.parent
SKIP = {".venv", "__pycache__", "data", "resources", ".git"}
EXT = {".py", ".md", ".json", ".sh", ".example", ".txt", ".service"}


def files():
    for p in BASE.rglob("*"):
        if p.is_file() and not (SKIP & set(p.relative_to(BASE).parts)) and (p.suffix in EXT or p.name.endswith(".example")):
            if p.name != "rename_bot.py":
                yield p


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) != 1 or not args[0].strip() or args[0] == OLD:
        raise SystemExit(__doc__)
    new, apply = args[0].strip(), "--apply" in sys.argv
    total = 0
    for p in files():
        s = p.read_text(encoding="utf-8", errors="ignore")
        n = s.count(OLD)
        if n:
            total += n
            print(f"{n:4d}  {p.relative_to(BASE)}")
            if apply:
                p.write_text(s.replace(OLD, new), encoding="utf-8")
    print(f"共 {total} 处「{OLD}」→「{new}」", "已写入" if apply else "（预览，加 --apply 写入）")


if __name__ == "__main__":
    main()
