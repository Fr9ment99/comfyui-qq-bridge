# -*- coding: utf-8 -*-
"""
QQ 机器人 <-> 本地 ComfyUI 桥接服务

设计要点：
1. 使用 WebSocket 模式：本机主动外连腾讯网关，无需公网 IP / 端口映射，适配校园网。
2. QQ 侧只接收 C2C（单聊）消息，intents = 1 << 25。
3. 生成任务串行执行（单 GPU），队列上限 3。
4. 图片通过富媒体接口以 base64 直传，不依赖公网图片链接。
5. 被动回复：收到消息后用 msg_id + 递增 msg_seq 回复，最多 4 次。

运行方式：由 qq_bot_panel.py 启动，也可直接 python qq_comfy_bridge.py
"""

import asyncio
import base64
import collections
import io
import json
import os
import random
import sys
import time
import traceback
from pathlib import Path

import aiohttp
from aiohttp import web

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "qq_bot_config.json"
PROMPTS_FILE = BASE_DIR / "prompts.json"
DEFAULT_WORKFLOW_NAME = "Z-iMage-turbo基础工作流（文生图）.json"
WORKFLOW_FILE = BASE_DIR / DEFAULT_WORKFLOW_NAME

# ===== ComfyUI =====
COMFYUI_URL = "http://127.0.0.1:8188"
PROMPT_NODE_ID = "6"      # CLIPTextEncode：正向提示词
SEED_NODE_ID = "3"        # KSampler：种子
SIZE_NODE_ID = "13"       # EmptySD3LatentImage：尺寸
SAVE_NODE_ID = "9"        # SaveImage：输出
WIDTH, HEIGHT = 768, 1280
GEN_TIMEOUT = 240         # 秒；被动回复窗口约 5 分钟，留出余量

# ===== QQ =====
API_BASE = "https://api.bot.qq.com"
TOKEN_URL = "https://api.bot.qq.com/app/getAppAccessToken"
INTENTS_C2C = 1 << 25     # 群与单聊事件
MAX_IMAGE_BYTES = 3_500_000  # base64 后会膨胀约 1/3，控制在 5MB 以内
QUEUE_LIMIT = 3
MAX_REPLY_PER_MSG = 4     # 同一 msg_id 最多回复次数

# ===== 本地状态服务 =====
STATUS_HOST = "127.0.0.1"
STATUS_PORT = 8189        # 同时充当单实例锁
STATUS_APP = "qq-comfy-bridge"


def log(msg, level="INFO"):
    print(f"[{time.strftime('%H:%M:%S')}] [{level}] {msg}", flush=True)


class QQAPIError(Exception):
    pass


class ComfyUIError(Exception):
    pass


def load_config():
    cfg = {"appid": "", "appsecret": "", "openids": []}
    if CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
        except Exception as e:
            log(f"读取配置失败：{e}", "WARN")
    return cfg


def save_config(cfg):
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def resolve_workflow(cfg):
    """配置了 workflow 就用它，否则在自身目录和上级目录里找默认工作流。"""
    custom = str(cfg.get("workflow") or "").strip()
    if custom:
        path = Path(custom)
        if not path.is_absolute():
            path = BASE_DIR / path
        if path.is_file():
            return path.resolve()
        log(f"配置的 workflow 不可用：{path}", "WARN")
    for parent in [BASE_DIR, *list(BASE_DIR.parents)[:4]]:
        candidate = parent / DEFAULT_WORKFLOW_NAME
        if candidate.is_file():
            return candidate
    return BASE_DIR / DEFAULT_WORKFLOW_NAME


def load_prompts():
    if PROMPTS_FILE.exists():
        try:
            text = PROMPTS_FILE.read_text(encoding="utf-8").strip()
            return json.loads(text) if text else {}
        except Exception:
            return {}
    return {}


def save_prompts(data):
    PROMPTS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=4), encoding="utf-8")


class TokenManager:
    """缓存 AppAccessToken，提前 60 秒刷新。"""

    def __init__(self, session, appid, secret):
        self.session = session
        self.appid = appid
        self.secret = secret
        self.token = None
        self.expire_at = 0
        self.lock = asyncio.Lock()

    async def get(self):
        async with self.lock:
            if self.token and time.time() < self.expire_at - 60:
                return self.token
            async with self.session.post(
                TOKEN_URL,
                json={"appId": self.appid, "clientSecret": self.secret},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                text = await r.text()
            try:
                data = json.loads(text)
            except Exception:
                raise QQAPIError(f"获取 access_token 返回非 JSON：HTTP {r.status} {text[:200]}")
            if "access_token" not in data:
                raise QQAPIError(f"获取 access_token 失败：{text[:300]}")
            self.token = data["access_token"]
            self.expire_at = time.time() + int(data.get("expires_in", 7200))
            log("access_token 已刷新")
            return self.token


async def comfy_health(session):
    """返回 ComfyUI 系统信息，失败抛异常。"""
    async with session.get(f"{COMFYUI_URL}/system_stats", timeout=aiohttp.ClientTimeout(total=3)) as r:
        if r.status != 200:
            raise ComfyUIError(f"HTTP {r.status}")
        return await r.json()


async def comfy_generate(session, prompt_text, seed):
    """提交工作流并等待结果，返回 (图片字节, 实际种子)。"""
    workflow = json.loads(WORKFLOW_FILE.read_text(encoding="utf-8"))
    if PROMPT_NODE_ID not in workflow:
        raise ComfyUIError(f"工作流缺少节点 {PROMPT_NODE_ID}，请确认导出的是 API 格式")

    workflow[PROMPT_NODE_ID]["inputs"]["text"] = prompt_text
    workflow[SEED_NODE_ID]["inputs"]["seed"] = seed
    workflow[SIZE_NODE_ID]["inputs"]["width"] = WIDTH
    workflow[SIZE_NODE_ID]["inputs"]["height"] = HEIGHT
    workflow[SIZE_NODE_ID]["inputs"]["batch_size"] = 1

    try:
        async with session.post(
            f"{COMFYUI_URL}/prompt",
            json={"prompt": workflow},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as r:
            text = await r.text()
            if r.status != 200:
                raise ComfyUIError(f"提交任务失败 HTTP {r.status}：{text[:300]}")
            prompt_id = json.loads(text)["prompt_id"]
    except aiohttp.ClientError as e:
        raise ComfyUIError(f"无法连接 ComfyUI（{e}），请确认它已启动")

    log(f"已提交 ComfyUI 任务 {prompt_id}")
    deadline = time.time() + GEN_TIMEOUT
    while time.time() < deadline:
        await asyncio.sleep(1)
        async with session.get(f"{COMFYUI_URL}/history/{prompt_id}", timeout=aiohttp.ClientTimeout(total=10)) as r:
            history = await r.json()

        if prompt_id not in history:
            continue

        entry = history[prompt_id]
        status = entry.get("status") or {}
        if status.get("status_str") == "error":
            detail = json.dumps(status.get("messages", ""), ensure_ascii=False)[:300]
            raise ComfyUIError(f"ComfyUI 执行出错：{detail}")

        outputs = entry.get("outputs") or {}
        images = (outputs.get(SAVE_NODE_ID) or {}).get("images") or []
        if not images:
            continue

        info = images[0]
        async with session.get(
            f"{COMFYUI_URL}/view",
            params={
                "filename": info["filename"],
                "subfolder": info.get("subfolder", ""),
                "type": info.get("type", "output"),
            },
            timeout=aiohttp.ClientTimeout(total=60),
        ) as r:
            data = await r.read()
        if not data:
            raise ComfyUIError("图片下载为空")
        return data, seed

    raise ComfyUIError(f"生成超时（{GEN_TIMEOUT} 秒）")


def prepare_image(data):
    """保证图片体积满足 QQ 富媒体上传限制，必要时转 JPEG / 缩小。"""
    if len(data) <= MAX_IMAGE_BYTES:
        return data
    try:
        from PIL import Image
    except Exception:
        return data

    img = Image.open(io.BytesIO(data)).convert("RGB")
    for quality in (90, 80, 70, 60):
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=quality, optimize=True)
        if buf.tell() <= MAX_IMAGE_BYTES:
            log(f"图片过大，已压缩为 JPEG(q={quality})，{buf.tell()} 字节")
            return buf.getvalue()

    while True:
        w, h = img.size
        if w <= 512 or h <= 512:
            return data
        img = img.resize((int(w * 0.8), int(h * 0.8)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=85, optimize=True)
        if buf.tell() <= MAX_IMAGE_BYTES:
            log(f"图片过大，已缩放为 {img.size}，{buf.tell()} 字节")
            return buf.getvalue()


HELP_TEXT = (
    "ComfyUI 生图助手（768×1280）\n"
    "直接发送文字即可生成图片\n"
    "/status 查看服务与队列状态\n"
    "/cancel 中断当前生成\n"
    "/save 名字 提示词 收藏提示词\n"
    "/list 收藏列表\n"
    "/get 名字 使用收藏的提示词\n"
    "/del 名字 删除收藏\n"
    "/help 显示本帮助"
)


class QQBot:
    def __init__(self, session, cfg):
        self.session = session
        self.cfg = cfg
        self.token_mgr = TokenManager(session, cfg["appid"], cfg["appsecret"])
        self.queue = asyncio.Queue(maxsize=QUEUE_LIMIT)
        self.seq_map = {}
        self.seen = collections.OrderedDict()
        self.last_seq = None
        self.session_id = None
        self.current_job = None
        self.started_at = time.time()
        self.connected = False
        self.last_error = ""
        self.last_image_at = 0

    def stats(self):
        return {
            "app": STATUS_APP,
            "pid": os.getpid(),
            "connected": self.connected,
            "uptime": int(time.time() - self.started_at),
            "queue": self.queue.qsize(),
            "queue_limit": QUEUE_LIMIT,
            "generating": bool(self.current_job),
            "prompt": (self.current_job or {}).get("prompt", "")[:120],
            "last_error": self.last_error,
            "last_image_at": int(self.last_image_at) or None,
        }

    # ---------- QQ HTTP API ----------
    async def api(self, method, path, **kwargs):
        token = await self.token_mgr.get()
        headers = {"Authorization": f"QQBot {token}", "Content-Type": "application/json"}
        kwargs.setdefault("timeout", aiohttp.ClientTimeout(total=60))
        async with self.session.request(
            method, API_BASE + path, headers=headers, **kwargs
        ) as r:
            text = await r.text()
        if r.status >= 300:
            raise QQAPIError(f"{method} {path} -> HTTP {r.status}: {text[:300]}")
        if not text:
            return {}
        data = json.loads(text)
        if data.get("code") not in (None, 0):
            raise QQAPIError(f"{method} {path} -> {data}")
        return data

    def next_seq(self, msg_id):
        n = self.seq_map.get(msg_id, 0) + 1
        self.seq_map[msg_id] = n
        if len(self.seq_map) > 500:
            self.seq_map.pop(next(iter(self.seq_map)))
        return min(n, MAX_REPLY_PER_MSG)

    async def send_text(self, openid, content, msg_id=None, seq=None):
        payload = {"content": content, "msg_type": 0}
        if msg_id:
            payload["msg_id"] = msg_id
            payload["msg_seq"] = seq or self.next_seq(msg_id)
        await self.api("POST", f"/v2/users/{openid}/messages", json=payload)

    async def upload_image(self, openid, image_bytes):
        file_data = base64.b64encode(image_bytes).decode()
        data = await self.api("POST", f"/v2/users/{openid}/files", json={
            "file_type": 1,
            "url": "",
            "srv_send_msg": False,
            "file_data": file_data,
        })
        info = data.get("file_info")
        if not info:
            raise QQAPIError(f"上传图片未返回 file_info：{str(data)[:300]}")
        return info

    async def send_image(self, openid, file_info, msg_id, content=""):
        payload = {
            "content": content,
            "msg_type": 7,
            "media": {"file_info": file_info},
            "msg_id": msg_id,
            "msg_seq": self.next_seq(msg_id),
        }
        await self.api("POST", f"/v2/users/{openid}/messages", json=payload)

    # ---------- 消息分发 ----------
    def authorized(self, openid):
        allowed = self.cfg.setdefault("openids", [])
        if not allowed:
            allowed.append(openid)
            save_config(self.cfg)
            log(f"首次使用，已自动绑定 OpenID：{openid}", "INFO")
            return True
        return openid in allowed

    async def on_message(self, d):
        msg_id = d.get("id")
        openid = (d.get("author") or {}).get("user_openid", "")
        content = (d.get("content") or "").strip()
        if not msg_id or not openid:
            return

        if msg_id in self.seen:
            return
        self.seen[msg_id] = time.time()
        if len(self.seen) > 500:
            self.seen.popitem(last=False)

        log(f"收到消息 openid={openid[:8]}… 内容={content[:60]!r}")

        if not self.authorized(openid):
            log("非白名单用户，已忽略", "WARN")
            return

        try:
            await self.dispatch(openid, msg_id, content)
        except QQAPIError as e:
            log(f"回复失败：{e}", "ERROR")
        except Exception:
            log("处理消息异常：\n" + traceback.format_exc(), "ERROR")

    async def dispatch(self, openid, msg_id, content):
        if not content:
            return

        if not content.startswith("/"):
            await self.enqueue(openid, msg_id, content)
            return

        cmd, _, rest = content.partition(" ")
        cmd = cmd.lower()
        rest = rest.strip()

        if cmd in ("/help", "/帮助", "/start"):
            await self.send_text(openid, HELP_TEXT, msg_id)

        elif cmd in ("/status", "/状态"):
            await self.send_text(openid, await self.build_status(), msg_id)

        elif cmd in ("/cancel", "/取消"):
            try:
                async with self.session.post(f"{COMFYUI_URL}/interrupt",
                                             timeout=aiohttp.ClientTimeout(total=5)) as r:
                    await r.read()
                await self.send_text(openid, "已请求中断当前生成。", msg_id)
            except Exception as e:
                await self.send_text(openid, f"中断失败：{e}", msg_id)

        elif cmd == "/save":
            name, _, prompt = rest.partition(" ")
            if not name or not prompt.strip():
                await self.send_text(openid, "用法：/save 名字 提示词", msg_id)
                return
            prompts = load_prompts()
            prompts[name] = prompt.strip()
            save_prompts(prompts)
            await self.send_text(openid, f"已收藏：{name}", msg_id)

        elif cmd == "/list":
            prompts = load_prompts()
            if not prompts:
                await self.send_text(openid, "收藏夹是空的，用 /save 名字 提示词 添加。", msg_id)
            else:
                lines = [f"{i + 1}. {k}" for i, k in enumerate(prompts)]
                await self.send_text(openid, "收藏列表：\n" + "\n".join(lines), msg_id)

        elif cmd == "/get":
            if not rest:
                await self.send_text(openid, "用法：/get 名字", msg_id)
                return
            prompts = load_prompts()
            if rest not in prompts:
                await self.send_text(openid, f"找不到收藏：{rest}", msg_id)
                return
            await self.enqueue(openid, msg_id, prompts[rest])

        elif cmd == "/del":
            prompts = load_prompts()
            if rest in prompts:
                prompts.pop(rest)
                save_prompts(prompts)
                await self.send_text(openid, f"已删除：{rest}", msg_id)
            else:
                await self.send_text(openid, f"找不到收藏：{rest}", msg_id)

        else:
            await self.send_text(openid, "未知指令，发送 /help 查看用法。", msg_id)

    async def build_status(self):
        lines = ["运行中"]
        try:
            stats = await comfy_health(self.session)
            devices = stats.get("devices") or []
            dev_name = devices[0].get("name", "未知设备") if devices else "未知设备"
            lines.append(f"ComfyUI：在线（{dev_name}）")
            async with self.session.get(f"{COMFYUI_URL}/queue",
                                        timeout=aiohttp.ClientTimeout(total=5)) as r:
                q = await r.json()
            running = len(q.get("queue_running", []))
            pending = len(q.get("queue_pending", []))
            lines.append(f"ComfyUI 队列：运行 {running} / 等待 {pending}")
        except Exception:
            lines.append("ComfyUI：离线")
        lines.append(f"机器人队列：{self.queue.qsize()} 个等待，上限 {QUEUE_LIMIT}")
        if self.current_job:
            lines.append(f"当前任务：{self.current_job['prompt'][:30]}…")
        lines.append(f"已运行：{int(time.time() - self.started_at)} 秒")
        return "\n".join(lines)

    async def enqueue(self, openid, msg_id, prompt):
        if self.queue.full():
            await self.send_text(openid, "队列已满（3 个任务等待），请稍后再试。", msg_id)
            return
        await self.queue.put({"prompt": prompt, "openid": openid, "msg_id": msg_id})
        waiting = self.queue.qsize() - 1
        note = f"前面还有 {waiting} 个任务，" if waiting > 0 else ""
        await self.send_text(openid, f"已收到，{note}开始生成（768×1280）…", msg_id)
        log(f"任务入队，等待数={waiting}")

    # ---------- 生成 worker ----------
    async def worker(self):
        while True:
            job = await self.queue.get()
            try:
                await self.process(job)
            except Exception:
                log("生成任务异常：\n" + traceback.format_exc(), "ERROR")
            finally:
                self.current_job = None
                self.queue.task_done()

    async def process(self, job):
        prompt, openid, msg_id = job["prompt"], job["openid"], job["msg_id"]
        self.current_job = job
        seed = random.randint(1, 2 ** 32 - 1)
        t0 = time.time()
        log(f"开始生成 seed={seed} prompt={prompt[:60]!r}")

        try:
            image, _ = await comfy_generate(self.session, prompt, seed)
        except ComfyUIError as e:
            log(f"生成失败：{e}", "ERROR")
            await self.send_text(openid, f"生成失败：{e}", msg_id)
            return
        except Exception as e:
            log("生成异常：\n" + traceback.format_exc(), "ERROR")
            await self.send_text(openid, f"生成异常：{e}", msg_id)
            return

        image = prepare_image(image)
        log(f"生成完成，用时 {time.time() - t0:.1f}s，图片 {len(image)} 字节")

        try:
            file_info = await self.upload_image(openid, image)
            caption = f"完成（{time.time() - t0:.0f}s，seed {seed}）"
            await self.send_image(openid, file_info, msg_id, caption)
            self.last_image_at = time.time()
            log("图片已发送")
        except QQAPIError as e:
            log(f"发送图片失败：{e}", "ERROR")
            await self.send_text(openid, f"图片发送失败：{e}", msg_id)

    # ---------- WebSocket ----------
    async def run(self):
        asyncio.create_task(self.worker())
        fail = 0
        while True:
            try:
                await self.connect_once()
                fail = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                fail += 1
                self.last_error = str(e)
                log(f"连接断开：{e!r}", "ERROR")
            delay = min(30, 3 * max(fail, 1))
            log(f"{delay} 秒后重连…")
            await asyncio.sleep(delay)

    async def connect_once(self):
        data = await self.api("GET", "/gateway")
        url = data.get("url")
        if not url:
            raise QQAPIError(f"获取网关失败：{data}")
        log(f"连接网关 {url}")

        async with self.session.ws_connect(url, heartbeat=None, max_msg_size=0,
                                           timeout=20, autoping=True) as ws:
            hello = await ws.receive_json(timeout=20)
            interval = (hello.get("d") or {}).get("heartbeat_interval", 40000) / 1000
            log(f"网关握手完成，心跳间隔 {interval:.0f}s")

            token = await self.token_mgr.get()
            await ws.send_json({
                "op": 2,
                "d": {
                    "token": f"QQBot {token}",
                    "intents": INTENTS_C2C,
                    "shard": [0, 1],
                    "properties": {},
                },
            })

            hb = asyncio.create_task(self.heartbeat(ws, interval))
            try:
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        payload = json.loads(msg.data)
                        reconnect = await self.on_payload(payload)
                        if reconnect:
                            log("服务端要求重新连接", "WARN")
                            break
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        log(f"连接关闭：{ws.close_code}", "WARN")
                        break
            finally:
                self.connected = False
                hb.cancel()

    async def heartbeat(self, ws, interval):
        while True:
            await asyncio.sleep(interval)
            try:
                await ws.send_json({"op": 1, "d": self.last_seq})
            except Exception:
                return

    async def on_payload(self, payload):
        op = payload.get("op")
        seq = payload.get("s")
        if isinstance(seq, int):
            self.last_seq = seq

        if op == 0:
            t = payload.get("t")
            d = payload.get("d") or {}
            if t == "READY":
                self.session_id = d.get("session_id")
                self.connected = True
                self.last_error = ""
                user = d.get("user") or {}
                log(f"机器人已上线：{user.get('username', '未知')} session={self.session_id}")
            elif t == "C2C_MESSAGE_CREATE":
                asyncio.create_task(self.on_message(d))
        elif op == 7:
            return True
        elif op == 9:
            log("session 无效，将重新 identify", "WARN")
            return True
        elif op == 11:
            pass
        return False


async def serve_status(bot):
    """本地状态服务：给插件面板查状态，同时用端口占用充当单实例锁。"""

    async def health(request):
        return web.json_response(bot.stats())

    app = web.Application()
    app.router.add_get("/health", health)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, STATUS_HOST, STATUS_PORT).start()
    log(f"状态服务：http://{STATUS_HOST}:{STATUS_PORT}/health")
    return runner


async def already_running(session):
    try:
        async with session.get(f"http://{STATUS_HOST}:{STATUS_PORT}/health",
                               timeout=aiohttp.ClientTimeout(total=2)) as r:
            data = await r.json(content_type=None)
    except Exception:
        return False
    return data.get("app") == STATUS_APP


async def main():
    global WORKFLOW_FILE
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    cfg = load_config()
    WORKFLOW_FILE = resolve_workflow(cfg)
    appid = (cfg.get("appid") or "").strip()
    secret = (cfg.get("appsecret") or "").strip()
    if not appid or not secret:
        log("未配置 AppID / AppSecret，请在面板中填写并保存。", "ERROR")
        raise SystemExit(2)

    if not WORKFLOW_FILE.exists():
        log(f"找不到工作流文件：{WORKFLOW_FILE}", "ERROR")
        return

    log("QQ Bot 服务启动中…")
    async with aiohttp.ClientSession(trust_env=False) as session:
        if await already_running(session):
            log(f"已有实例在 {STATUS_HOST}:{STATUS_PORT} 运行，本次退出。", "ERROR")
            return

        try:
            await comfy_health(session)
            log("ComfyUI 连接正常")
        except Exception as e:
            log(f"ComfyUI 当前不可用（{e}），生成时会再次尝试", "WARN")

        bot = QQBot(session, cfg)
        try:
            runner = await serve_status(bot)
        except OSError as e:
            log(f"状态端口 {STATUS_PORT} 不可用（{e}），继续运行但无法提供状态", "WARN")
            runner = None

        try:
            await bot.run()
        except asyncio.CancelledError:
            pass
        finally:
            if runner:
                await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("已停止")
