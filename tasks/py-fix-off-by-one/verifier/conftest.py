import sys
from pathlib import Path

# 让测试可以 import 工作区根目录下的 pagination 模块
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
