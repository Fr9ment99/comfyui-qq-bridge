# -*- coding: utf-8 -*-
"""QQ 机器人宿主插件。

只负责生命周期：拉起 / 重启 / 停止 qq_comfy_bridge.py 子进程，并把配置和状态
通过 HTTP 路由暴露给前端设置页。生图逻辑全部留在子进程里。
"""

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

import aiohttp
from aiohttp import web
from server import PromptServer

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "qq_bot_config.json"
BRIDGE_FILE = BASE_DIR / "qq_comfy_bridge.py"
WEB_DIRECTORY = "./web"
__all__ = ["WEB_DIRECTORY"]

STATUS_URL = "http://127.0.0.1:8189/health"
STATUS_APP = "qq-comfy-bridge"
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

LOG = logging.getLogger("qq_bridge")

_proc = None
_routes = PromptServer.instance.routes


def load_config():
    cfg = {"appid": "", "appsecret": "", "openids": [], "workflow": "", "autostart": True}
    if CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
        except ValueError as e:
            LOG.warning("[QQBot] 读取配置失败：%s", e)
    return cfg


def save_config(cfg):
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def running():
    return _proc is not None and _proc.poll() is None


def _pump_output(proc):
    for line in proc.stdout:
        LOG.info("[QQBot] %s", line.rstrip())
    LOG.info("[QQBot] 子进程退出，退出码 %s", proc.poll())


def start():
    """启动子进程。返回是否真的拉起了新进程。"""
    global _proc
    if running():
        return False
    cfg = load_config()
    if not (cfg.get("appid") and cfg.get("appsecret")):
        LOG.info("[QQBot] 未配置 AppID / AppSecret，跳过启动")
        return False

    env = os.environ.copy()
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        env.pop(key, None)

    _proc = subprocess.Popen(
        [sys.executable, "-u", str(BRIDGE_FILE)],
        cwd=str(BASE_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        creationflags=CREATE_NO_WINDOW,
    )
    threading.Thread(target=_pump_output, args=(_proc,), daemon=True).start()
    LOG.info("[QQBot] 子进程已启动 pid=%s", _proc.pid)
    return True


def stop(timeout=5):
    global _proc
    proc = _proc
    if proc is None or proc.poll() is not None:
        return False
    proc.terminate()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)
    LOG.info("[QQBot] 子进程已停止")
    return True


async def fetch_health(session):
    try:
        async with session.get(STATUS_URL, timeout=aiohttp.ClientTimeout(total=2)) as r:
            data = await r.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("app") == STATUS_APP else None


async def kill_orphan(session):
    """杀掉不是本插件拉起、但占着状态端口的旧实例。"""
    data = await fetch_health(session)
    pid = (data or {}).get("pid")
    if not pid:
        return False
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=CREATE_NO_WINDOW)
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    LOG.info("[QQBot] 已结束旧实例 pid=%s", pid)
    return True


async def restart_bridge():
    await asyncio.to_thread(stop)
    async with aiohttp.ClientSession(trust_env=False) as session:
        if await kill_orphan(session):
            for _ in range(20):
                await asyncio.sleep(0.25)
                if not await fetch_health(session):
                    break
    start()


@_routes.get("/qq_bridge/status")
async def qq_bridge_status(request):
    cfg = load_config()
    async with aiohttp.ClientSession(trust_env=False) as session:
        health = await fetch_health(session)
    return web.json_response({
        "managed": running(),
        "pid": _proc.pid if running() else None,
        "configured": bool(cfg.get("appid") and cfg.get("appsecret")),
        "appid": cfg.get("appid", ""),
        "openids": cfg.get("openids", []),
        "autostart": bool(cfg.get("autostart", True)),
        "health": health,
    })


@_routes.post("/qq_bridge/config")
async def qq_bridge_config(request):
    data = await request.json()
    cfg = load_config()
    for key in ("appid", "appsecret", "workflow"):
        if key in data:
            cfg[key] = str(data[key]).strip()
    if "autostart" in data:
        cfg["autostart"] = bool(data["autostart"])
    save_config(cfg)
    await restart_bridge()
    return web.json_response({"ok": True})


@_routes.post("/qq_bridge/openids/clear")
async def qq_bridge_clear_openids(request):
    cfg = load_config()
    cfg["openids"] = []
    save_config(cfg)
    await restart_bridge()
    return web.json_response({"ok": True})


@_routes.post("/qq_bridge/start")
async def qq_bridge_start(request):
    started = start()
    return web.json_response({"ok": True, "started": started})


@_routes.post("/qq_bridge/stop")
async def qq_bridge_stop(request):
    stopped = await asyncio.to_thread(stop)
    return web.json_response({"ok": True, "stopped": stopped})


@_routes.post("/qq_bridge/restart")
async def qq_bridge_restart(request):
    await restart_bridge()
    return web.json_response({"ok": True})


async def _on_startup(app):
    if not load_config().get("autostart", True):
        return
    async with aiohttp.ClientSession(trust_env=False) as session:
        if await fetch_health(session):
            LOG.info("[QQBot] 已有实例在运行，跳过自动启动")
            return
    start()


async def _on_cleanup(app):
    await asyncio.to_thread(stop, 3)


PromptServer.instance.app.on_startup.append(_on_startup)
PromptServer.instance.app.on_cleanup.append(_on_cleanup)
