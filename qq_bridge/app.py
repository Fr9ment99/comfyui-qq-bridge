# -*- coding: utf-8 -*-
"""桥接进程入口：配置校验、单实例检测、本地状态服务、启动机器人。

由 qq_comfy_bridge.py 调用，也可以 python -m qq_bridge.app 直接运行。
"""

import asyncio
import sys

import aiohttp
from aiohttp import web

from .bot import QQBot, STATUS_APP, log
from .config import load_config

STATUS_HOST = "127.0.0.1"


def _enable_utf8_stdout():
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass


async def already_running(session, port):
    try:
        async with session.get(
            f"http://{STATUS_HOST}:{port}/health",
            timeout=aiohttp.ClientTimeout(total=2),
        ) as response:
            data = await response.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return False
    return isinstance(data, dict) and data.get("app") == STATUS_APP


async def serve_status(bot, port):
    async def health(request):
        return web.json_response(bot.stats())

    app = web.Application()
    app.router.add_get("/health", health)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner, STATUS_HOST, port).start()
    except OSError:
        await runner.cleanup()
        raise
    return runner


async def main():
    _enable_utf8_stdout()

    cfg = load_config()
    if not cfg.get("appid") or not cfg.get("appsecret"):
        log("未配置 AppID / AppSecret，请在 ComfyUI 右下角「QQ 机器人」徽标里填写。", "ERROR")
        raise SystemExit(2)

    port = int(cfg.get("status_port") or 8189)
    log("QQ Bot 桥接启动中…")
    async with aiohttp.ClientSession(trust_env=False) as session:
        if await already_running(session, port):
            log(f"状态端口 {port} 上已有实例在运行，本次退出。", "ERROR")
            return

        bot = QQBot(session, cfg)
        try:
            await bot.comfy.health()
            log("ComfyUI 连接正常")
        except Exception as exc:
            log(f"ComfyUI 当前不可用（{exc}），生成时会再次尝试", "WARN")

        try:
            runner = await serve_status(bot, port)
            log(f"状态服务：http://{STATUS_HOST}:{port}/health")
        except OSError as exc:
            log(f"状态端口 {port} 不可用（{exc}），继续运行但没有状态接口", "WARN")
            runner = None

        try:
            await bot.run()
        except asyncio.CancelledError:
            pass
        finally:
            if runner:
                await runner.cleanup()
