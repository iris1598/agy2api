"""测试引导：将仓库根目录加入 sys.path，使 `src` / `config` / `log` 可导入。"""
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
