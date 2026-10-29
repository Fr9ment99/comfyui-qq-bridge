# -*- coding: utf-8 -*-
"""桥接进程入口。

真正实现在 qq_bridge 包里，这里只负责启动事件循环。
一般由 ComfyUI 的徽标自动拉起，也可以手动运行：python qq_comfy_bridge.py
"""

import asyncio

from qq_bridge.app import main

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
