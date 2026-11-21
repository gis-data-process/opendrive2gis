# -*- coding: utf-8 -*-
"""各条流水线共用的输出文件保护。"""
from contextlib import contextmanager
from pathlib import Path
import os
import tempfile


@contextmanager
def atomic_output(destination):
    """先写同目录临时文件，成功后替换目标；失败或目标被占用时保留旧文件。

    调用方必须在退出上下文前关闭数据库和文件句柄。临时目录与目标位于
    同一文件系统，避免跨盘移动破坏替换操作的原子性。
    """
    target = Path(destination).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{target.stem}-", dir=target.parent) as folder:
        temporary = Path(folder) / target.name
        yield str(temporary)
        if not temporary.is_file() or temporary.stat().st_size == 0:
            raise ValueError(f"没有生成有效的输出文件: {target.name}")
        os.replace(temporary, target)
