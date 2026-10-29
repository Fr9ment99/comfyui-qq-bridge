# -*- coding: utf-8 -*-
"""桥接进程入口。

真正实现在 qq_bridge 包里，这里只负责启动事件循环，保留旧的文件名，
方便已经配置好的 bat / 快捷方式继续可用：python qq_comfy_bridge.py
"""

import asyncio

from qq_bridge.app import main

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
