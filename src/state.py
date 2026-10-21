"""去重状态：{repo#mr: version}，落盘到 state_file。

为什么不放内存：Pod 一重启就全忘了，会把所有打开的 MR 重评一遍，
群里跟着刷一遍。落盘之后至少能扛住滚动更新。

不做成数据库是刻意的——单副本 + RWO 盘够用，多一层依赖就多一处会坏的地方。
"""
import json
import os
import tempfile


def load(path):
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        # 文件坏了不能让它起不来，最坏结果是重评一遍
        print("state 读取失败，当作空：%r" % (exc,), flush=True)
        return {}


def save(path, data):
    """先写临时文件再 rename，避免容器被 kill 时留下半个 JSON。"""
    if not path:
        return
    d = os.path.dirname(os.path.abspath(path))
    try:
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d or ".", prefix=".state-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except Exception as exc:
        print("state 写入失败：%r" % (exc,), flush=True)
