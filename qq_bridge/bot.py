# -*- coding: utf-8 -*-
"""消息路由与生成队列。

交互模型（已与作者确认）：
- 按钮菜单固定四项：文生图 / 图生图 / 局部重绘 / 再来一张
- 未选模式时，用户发什么都只回菜单
- 模式一次性有效，出图后清空并再次弹出菜单
- 生成期间不发 ack，结果图 + 菜单共消耗 2 次被动回复额度
"""

import asyncio
import collections
import io
import json
import os
import random
import time
import traceback
from typing import Optional

import aiohttp

from . import menu
from .comfy import ComfyClient
from .config import load_config, load_prompts, save_config, save_prompts
from .errors import ComfyError, ProfileError, QQAPIError
from .profiles import MODE_LABELS, list_profiles, load_profile, resolve_active
from .qq_api import QQClient
from .session import JobRecord, SessionStore
from .workflow import build_prompt, output_node_of

INTENTS = (1 << 25) | (1 << 26)  # 单聊消息 + 互动事件
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
STATUS_APP = "qq-comfy-bridge"


def log(message, level="INFO"):
    print(f"[{time.strftime('%H:%M:%S')}] [{level}] {message}", flush=True)


def prepare_image(data, max_bytes):
    """保证图片体积满足 QQ 富媒体上传限制，必要时转 JPEG 或缩小。"""
    if len(data) <= max_bytes:
        return data
    try:
        from PIL import Image
    except Exception:
        return data

    image = Image.open(io.BytesIO(data)).convert("RGB")
    for quality in (90, 80, 70, 60):
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", quality=quality, optimize=True)
        if buffer.tell() <= max_bytes:
            log(f"图片过大，已压缩为 JPEG(q={quality})，{buffer.tell()} 字节")
            return buffer.getvalue()

    while True:
        width, height = image.size
        if width <= 512 or height <= 512:
            return data
        image = image.resize((int(width * 0.8), int(height * 0.8)), Image.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", quality=85, optimize=True)
        if buffer.tell() <= max_bytes:
            log(f"图片过大，已缩放为 {image.size}，{buffer.tell()} 字节")
            return buffer.getvalue()


class Reply:
    """被动回复的锚点：要么挂消息 msg_id，要么挂互动事件 event_id。"""

    def __init__(self, msg_id=None, event_id=None):
        self.msg_id = msg_id
        self.event_id = event_id


class QQBot:
    def __init__(self, http_session, cfg):
        self.http = http_session
        self.cfg = cfg
        self.qq = QQClient(http_session, cfg.get("appid", ""), cfg.get("appsecret", ""))
        self.comfy = ComfyClient(http_session, cfg.get("comfyui_url"))
        self.store = SessionStore()
        self.queue = asyncio.Queue(maxsize=int(cfg.get("queue_limit") or 3))
        self.seen = collections.OrderedDict()
        self.seen_interactions = collections.OrderedDict()
        self.session_id = None
        self.last_seq = None
        self.connected = False
        self.current_job = None
        self.started_at = time.time()
        self.last_error = ""
        self.last_image_at = 0

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------

    def stats(self):
        try:
            profiles = list_profiles()
        except Exception:
            profiles = []
        return {
            "app": STATUS_APP,
            "pid": os.getpid(),
            "connected": self.connected,
            "uptime": int(time.time() - self.started_at),
            "queue": self.queue.qsize(),
            "queue_limit": self.cfg.get("queue_limit", 3),
            "generating": bool(self.current_job),
            "prompt": (self.current_job or {}).get("display", "")[:120],
            "last_error": self.last_error,
            "last_image_at": int(self.last_image_at) or None,
            "active_profiles": dict(self.cfg.get("active_profiles") or {}),
            "profiles": profiles,
            "keyboard_ok": bool(self.cfg.get("keyboard_ok", True)),
            "comfyui_url": self.cfg.get("comfyui_url"),
        }

    def active_summary(self):
        active = self.cfg.get("active_profiles") or {}
        parts = []
        for mode in ("txt2img", "img2img", "inpaint"):
            profile_id = active.get(mode) or ""
            if profile_id:
                parts.append(f"{MODE_LABELS[mode]}={profile_id}")
        return ("当前档案：" + "，".join(parts)) if parts else ""

    # ------------------------------------------------------------------
    # 收发
    # ------------------------------------------------------------------

    def authorized(self, openid):
        allowed = self.cfg.setdefault("openids", [])
        if allowed:
            return openid in allowed
        if self.cfg.get("auto_bind_first_user", True):
            allowed.append(openid)
            try:
                save_config(self.cfg)
            except Exception as exc:
                log(f"保存绑定结果失败：{exc}", "WARN")
            log(f"首次使用，已绑定 OpenID：{openid[:8]}…")
            return True
        log(f"未绑定用户尝试访问：{openid[:8]}…，已忽略", "WARN")
        return False

    async def reply_text(self, openid, reply, text):
        await self.qq.send_text(openid, text, msg_id=reply.msg_id, event_id=reply.event_id)

    async def send_menu(self, openid, reply, hint=""):
        active_line = self.active_summary()
        if self.cfg.get("keyboard_ok", True):
            try:
                await self.qq.send_menu(
                    openid,
                    menu.markdown_menu(hint, active_line),
                    menu.keyboard_payload(),
                    msg_id=reply.msg_id,
                    event_id=reply.event_id,
                )
                return
            except QQAPIError as exc:
                log(f"按钮菜单发送失败，降级为文字菜单：{exc}", "WARN")
                self.cfg["keyboard_ok"] = False
                try:
                    save_config(self.cfg)
                except Exception:
                    pass
        await self.reply_text(openid, reply, menu.text_menu(hint, active_line))

    async def on_message(self, data):
        msg_id = data.get("id")
        author = data.get("author") or {}
        openid = author.get("user_openid") or data.get("user_openid") or ""
        content = (data.get("content") or "").strip()
        attachments = data.get("attachments") or []
        if not msg_id or not openid:
            return

        if msg_id in self.seen:
            return
        self.seen[msg_id] = time.time()
        if len(self.seen) > 500:
            self.seen.popitem(last=False)

        log(f"收到消息 openid={openid[:8]}… 内容={content[:60]!r} 附件={len(attachments)}")
        if not self.authorized(openid):
            return

        images = await self.download_images(attachments)
        try:
            await self.route(openid, Reply(msg_id=msg_id), content, images)
        except QQAPIError as exc:
            log(f"回复失败：{exc}", "ERROR")
        except Exception:
            log("处理消息异常：\n" + traceback.format_exc(), "ERROR")

    async def on_interaction(self, data):
        interaction_id = data.get("id")
        try:
            await self.qq.respond_interaction(interaction_id)
        except Exception as exc:
            log(f"互动事件应答失败：{exc}", "WARN")

        if int(data.get("type") or 0) != 11:
            return
        openid = data.get("user_openid") or ""
        resolved = (data.get("data") or {}).get("resolved") or {}
        button_data = str(resolved.get("button_data") or "").strip()
        if not interaction_id or not openid or not button_data:
            return

        if interaction_id in self.seen_interactions:
            return
        self.seen_interactions[interaction_id] = time.time()
        if len(self.seen_interactions) > 500:
            self.seen_interactions.popitem(last=False)

        log(f"按钮回调 openid={openid[:8]}… data={button_data!r}")
        if not self.authorized(openid):
            return

        try:
            await self.route(openid, Reply(event_id=interaction_id), button_data, [])
        except QQAPIError as exc:
            log(f"按钮回复失败：{exc}", "ERROR")
        except Exception:
            log("处理按钮回调异常：\n" + traceback.format_exc(), "ERROR")

    async def download_images(self, attachments):
        images = []
        for item in attachments or []:
            if not isinstance(item, dict):
                continue
            content_type = str(item.get("content_type") or "")
            url = item.get("url")
            if not url or (content_type and not content_type.startswith("image/")):
                continue
            try:
                async with self.http.get(url, timeout=aiohttp.ClientTimeout(total=60)) as response:
                    if response.status != 200:
                        log(f"下载附件失败 HTTP {response.status}", "WARN")
                        continue
                    data = await response.read()
            except aiohttp.ClientError as exc:
                log(f"下载附件异常：{exc}", "WARN")
                continue
            if not data:
                continue
            if len(data) > MAX_ATTACHMENT_BYTES:
                log(f"附件过大，已忽略：{len(data)} 字节", "WARN")
                continue
            images.append(data)
        return images

    # ------------------------------------------------------------------
    # 路由
    # ------------------------------------------------------------------

    async def route(self, openid, reply, content, images):
        text = (content or "").strip()
        if text.startswith("/"):
            await self.handle_command(openid, reply, text, images)
            return

        session = self.store.get(openid)
        if not session.mode:
            mode = menu.normalize_mode(text) if text else None
            if mode in ("txt2img", "img2img", "inpaint", "again"):
                await self.select_mode(openid, reply, mode)
                return
            hint = "请先选择模式，再发送图片" if images else "请先选择模式"
            await self.send_menu(openid, reply, hint=hint)
            return

        mode = session.mode
        if mode == "txt2img":
            if images:
                await self.reply_text(openid, reply, "文生图只接收文字提示词，请直接发送文字。")
                return
            if not text:
                await self.reply_text(openid, reply, "请发送提示词。")
                return
            await self.enqueue(openid, reply, "txt2img", {"prompt": text})

        elif mode == "img2img":
            if not images:
                await self.reply_text(openid, reply, "请发送一张图片，可以附一句描述。")
                return
            await self.enqueue(
                openid, reply, "img2img",
                {"image": images[0], "prompt": text or None},
            )

        elif mode == "inpaint":
            if not images:
                await self.reply_text(
                    openid, reply, "请发送原图，并附：区域 | 重绘内容。"
                )
                return
            region, draw, explicit = menu.split_region(text)
            if not region:
                await self.reply_text(
                    openid, reply,
                    "请补一句描述：区域 | 重绘内容，例如「猫 | 一只穿西装的橘猫」。",
                )
                return
            hint = "" if explicit else "提示：用「区域 | 内容」可以分别指定分割目标和重绘内容"
            await self.enqueue(
                openid, reply, "inpaint",
                {"image": images[0], "region_prompt": region, "draw_prompt": draw},
                hint=hint,
            )

    async def handle_command(self, openid, reply, text, images):
        command, _, rest = text.partition(" ")
        command = command.lower()
        rest = rest.strip()

        if command in ("/menu", "/start", "/help", "/菜单", "/帮助"):
            await self.send_menu(openid, reply)

        elif command in ("/mode", "/模式"):
            mode = menu.normalize_mode(rest)
            if not mode:
                await self.reply_text(
                    openid, reply,
                    "用法：/mode txt2img | img2img | inpaint\n也可以直接点菜单按钮。",
                )
                return
            await self.select_mode(openid, reply, mode)

        elif command in ("/again", "/再来一张"):
            await self.again(openid, reply)

        elif command in ("/redraw", "/重绘"):
            await self.redraw(openid, reply, rest, images)

        elif command in ("/profiles", "/档案"):
            await self.list_profiles(openid, reply)

        elif command in ("/use", "/启用"):
            await self.use_profile(openid, reply, rest)

        elif command in ("/status", "/状态"):
            await self.reply_text(openid, reply, await self.build_status())

        elif command in ("/cancel", "/取消"):
            try:
                await self.comfy.interrupt()
                await self.reply_text(openid, reply, "已请求中断当前生成。")
            except Exception as exc:
                await self.reply_text(openid, reply, f"中断失败：{exc}")

        elif command == "/save":
            name, _, prompt = rest.partition(" ")
            if not name or not prompt.strip():
                await self.reply_text(openid, reply, "用法：/save 名字 提示词")
                return
            prompts = load_prompts()
            prompts[name] = prompt.strip()
            save_prompts(prompts)
            await self.reply_text(openid, reply, f"已收藏：{name}")

        elif command == "/list":
            prompts = load_prompts()
            if not prompts:
                await self.reply_text(openid, reply, "收藏夹是空的，用 /save 名字 提示词 添加。")
            else:
                lines = [f"{index + 1}. {key}" for index, key in enumerate(prompts)]
                await self.reply_text(openid, reply, "收藏列表：\n" + "\n".join(lines))

        elif command == "/get":
            prompts = load_prompts()
            if rest not in prompts:
                await self.reply_text(openid, reply, f"找不到收藏：{rest}")
                return
            await self.enqueue(openid, reply, "txt2img", {"prompt": prompts[rest]})

        elif command == "/del":
            prompts = load_prompts()
            if rest in prompts:
                prompts.pop(rest)
                save_prompts(prompts)
                await self.reply_text(openid, reply, f"已删除：{rest}")
            else:
                await self.reply_text(openid, reply, f"找不到收藏：{rest}")

        else:
            await self.reply_text(openid, reply, "未知指令，发送 /help 查看用法。")

    async def select_mode(self, openid, reply, mode):
        if mode == "again":
            await self.again(openid, reply)
            return
        try:
            profile = resolve_active(mode, self.cfg)
        except ProfileError as exc:
            await self.reply_text(openid, reply, str(exc))
            return
        self.store.set_mode(openid, mode)
        await self.reply_text(openid, reply, menu.mode_prompt(mode, profile.get("label", "")))

    async def again(self, openid, reply):
        last = self.store.last(openid)
        if not last:
            await self.reply_text(openid, reply, "还没有生成记录，先出一次图吧。")
            return

        inputs = {"prompt": last.prompt}
        if last.mode in ("img2img", "inpaint"):
            if not last.images:
                await self.reply_text(openid, reply, "上一次的结果没有可复用的原图，请重新发送图片。")
                return
            inputs["image"] = last.images[0]
        if last.mode == "inpaint":
            inputs["region_prompt"] = last.region_prompt
            inputs["draw_prompt"] = last.draw_prompt

        await self.enqueue(openid, reply, last.mode, inputs, profile_id=last.profile_id)

    async def redraw(self, openid, reply, rest, images):
        region, draw, explicit = menu.split_region(rest)
        if not region:
            await self.reply_text(
                openid, reply,
                "用法：/redraw 区域 | 重绘内容\n也可以先发送图片，再发这句。",
            )
            return

        image = images[0] if images else None
        if image is None:
            last = self.store.last(openid)
            if last and last.images:
                image = last.images[0]
        if image is None:
            await self.reply_text(openid, reply, "请先发送原图，或者先出过一张图。")
            return

        hint = "" if explicit else "提示：用「区域 | 内容」可以分别指定分割目标和重绘内容"
        await self.enqueue(
            openid, reply, "inpaint",
            {"image": image, "region_prompt": region, "draw_prompt": draw},
            hint=hint,
        )

    async def list_profiles(self, openid, reply):
        profiles = list_profiles()
        if not profiles:
            await self.reply_text(
                openid, reply,
                "还没有工作流档案。请在 ComfyUI 右下角徽标里点「导入当前画布」。",
            )
            return
        active = self.cfg.get("active_profiles") or {}
        lines = ["工作流档案："]
        for item in profiles:
            flag = "✓" if item["valid"] else "✗"
            mode = MODE_LABELS.get(item["mode"], item["mode"])
            mark = "（当前）" if active.get(item["mode"]) == item["id"] else ""
            line = f"{flag} {item['id']} · {mode} · {item['label']}{mark}"
            if not item["valid"]:
                line += f"\n   {item['error']}"
            lines.append(line)
        lines.append("\n用 /use 档案id 启用")
        await self.reply_text(openid, reply, "\n".join(lines))

    async def use_profile(self, openid, reply, profile_id):
        if not profile_id:
            await self.reply_text(openid, reply, "用法：/use 档案id")
            return
        try:
            profile = load_profile(profile_id)
        except ProfileError as exc:
            await self.reply_text(openid, reply, str(exc))
            return
        mode = profile.get("mode")
        self.cfg.setdefault("active_profiles", {})[mode] = profile_id
        save_config(self.cfg)
        await self.reply_text(
            openid, reply,
            f"已启用：{profile.get('label')}（{MODE_LABELS.get(mode, mode)}）",
        )

    async def build_status(self):
        lines = ["运行中"]
        try:
            stats = await self.comfy.health()
            devices = stats.get("devices") or []
            name = devices[0].get("name", "未知设备") if devices else "未知设备"
            lines.append(f"ComfyUI：在线（{name}）")
            queue = await self.comfy.queue_status()
            lines.append(
                f"ComfyUI 队列：运行 {len(queue.get('queue_running') or [])} / "
                f"等待 {len(queue.get('queue_pending') or [])}"
            )
        except Exception:
            lines.append("ComfyUI：离线")
        lines.append(f"机器人队列：{self.queue.qsize()} 个等待，上限 {self.cfg.get('queue_limit')}")
        if self.current_job:
            lines.append(f"当前任务：{self.current_job.get('display', '')[:40]}")
        summary = self.active_summary()
        if summary:
            lines.append(summary)
        lines.append(f"已运行：{int(time.time() - self.started_at)} 秒")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 队列
    # ------------------------------------------------------------------

    async def enqueue(self, openid, reply, mode, inputs, profile_id=None, hint=""):
        if self.queue.full():
            await self.reply_text(
                openid, reply,
                f"队列已满（{self.cfg.get('queue_limit')} 个任务），请稍后再试。",
            )
            return
        try:
            profile = load_profile(profile_id) if profile_id else resolve_active(mode, self.cfg)
        except ProfileError as exc:
            await self.reply_text(openid, reply, str(exc))
            return
        if profile.get("mode") != mode:
            await self.reply_text(
                openid, reply,
                f"档案 {profile.get('id')} 不是{MODE_LABELS.get(mode, mode)}模式，请用 /use 选择正确的档案。",
            )
            return

        seed = random.randint(1, 2 ** 32 - 1)
        display = str(inputs.get("prompt") or inputs.get("draw_prompt") or "")[:60]
        job = {
            "openid": openid,
            "msg_id": reply.msg_id,
            "event_id": reply.event_id,
            "mode": mode,
            "profile_id": profile.get("id"),
            "inputs": inputs,
            "seed": seed,
            "hint": hint,
            "display": display,
            "created": time.time(),
        }
        self.store.clear_mode(openid)
        await self.queue.put(job)
        log(f"任务入队：mode={mode} profile={profile.get('id')} seed={seed} 队列={self.queue.qsize()}")

    async def worker(self):
        while True:
            job = await self.queue.get()
            try:
                await self.process(job)
            except asyncio.CancelledError:
                raise
            except Exception:
                log("生成任务异常：\n" + traceback.format_exc(), "ERROR")
            finally:
                self.current_job = None
                self.queue.task_done()

    async def process(self, job):
        self.current_job = job
        openid = job["openid"]
        reply = Reply(msg_id=job["msg_id"], event_id=job["event_id"])
        started = time.time()

        try:
            profile = load_profile(job["profile_id"])
            inputs = dict(job["inputs"])
            targets = profile.get("targets") or {}
            defaults = profile.get("defaults") or {}
            if "width" in targets and "width" not in inputs:
                inputs["width"] = defaults.get("width", self.cfg.get("default_width", 1024))
            if "height" in targets and "height" not in inputs:
                inputs["height"] = defaults.get("height", self.cfg.get("default_height", 1024))
            if "batch" in targets:
                inputs.setdefault("batch", defaults.get("batch", 1))
            if "strength" in targets and "strength" not in inputs and "strength" in defaults:
                inputs["strength"] = defaults["strength"]

            timeout = int(self.cfg.get("gen_timeout") or 0) or int(profile.get("timeout") or 300)
            log(f"开始生成 profile={job['profile_id']} seed={job['seed']} timeout={timeout}s")

            workflow, seed = await build_prompt(profile, inputs, self.comfy, openid, job["seed"])
            data = await self.comfy.run(workflow, output_node_of(profile), timeout)
        except (ComfyError, ProfileError, QQAPIError) as exc:
            log(f"生成失败：{exc}", "ERROR")
            self.last_error = str(exc)
            try:
                await self.reply_text(openid, reply, f"生成失败：{exc}")
                await self.send_menu(openid, reply)
            except Exception:
                log("错误提示发送失败：\n" + traceback.format_exc(), "ERROR")
            return
        except Exception as exc:
            log("生成异常：\n" + traceback.format_exc(), "ERROR")
            self.last_error = str(exc)
            try:
                await self.reply_text(openid, reply, f"生成异常：{exc}")
            except Exception:
                pass
            return

        elapsed = time.time() - started
        data = prepare_image(data, int(self.cfg.get("max_image_bytes") or 3_500_000))
        log(f"生成完成，用时 {elapsed:.1f}s，图片 {len(data)} 字节")

        images = []
        if job["mode"] in ("img2img", "inpaint") and isinstance(inputs.get("image"), (bytes, bytearray)):
            images.append(bytes(inputs["image"]))
        self.store.remember(openid, JobRecord(
            mode=job["mode"],
            profile_id=job["profile_id"],
            prompt=str(inputs.get("prompt") or ""),
            region_prompt=str(inputs.get("region_prompt") or ""),
            draw_prompt=str(inputs.get("draw_prompt") or ""),
            seed=seed,
            images=images,
            output=data,
        ))

        try:
            file_info = await self.qq.upload_image(openid, data)
        except QQAPIError as exc:
            log(f"上传图片失败：{exc}", "ERROR")
            try:
                await self.reply_text(openid, reply, f"图片上传失败：{exc}")
            except Exception:
                pass
            return

        caption = f"{profile.get('label') or profile.get('id')} · {elapsed:.0f}s · seed {seed}"
        if job.get("hint"):
            caption += "\n" + job["hint"]

        try:
            await self.qq.send_image(
                openid, file_info, caption,
                msg_id=job["msg_id"], event_id=job["event_id"],
            )
            self.last_image_at = time.time()
            log("图片已发送")
        except QQAPIError as exc:
            log(f"发送图片失败：{exc}", "ERROR")
            try:
                await self.reply_text(openid, reply, f"图片发送失败：{exc}\n可能已经超出被动回复窗口。")
            except Exception:
                pass
            return

        try:
            await self.send_menu(openid, reply)
        except Exception as exc:
            log(f"菜单发送失败：{exc}", "WARN")

    # ------------------------------------------------------------------
    # WebSocket
    # ------------------------------------------------------------------

    async def run(self):
        asyncio.create_task(self.worker())
        failures = 0
        while True:
            try:
                await self.connect_once()
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                self.last_error = str(exc)
                log(f"连接断开：{exc!r}", "ERROR")
            delay = min(30, 3 * max(failures, 1))
            log(f"{delay} 秒后重连…")
            await asyncio.sleep(delay)

    async def connect_once(self):
        url = await self.qq.gateway()
        log(f"连接网关 {url}")
        async with self.http.ws_connect(
            url, heartbeat=None, max_msg_size=0, timeout=20, autoping=True
        ) as ws:
            hello = await ws.receive_json(timeout=20)
            interval = (hello.get("d") or {}).get("heartbeat_interval", 40000) / 1000
            log(f"网关握手完成，心跳间隔 {interval:.0f}s")

            token = await self.qq.access_token()
            await ws.send_json({
                "op": 2,
                "d": {
                    "token": f"QQBot {token}",
                    "intents": INTENTS,
                    "shard": [0, 1],
                    "properties": {},
                },
            })

            heartbeat = asyncio.create_task(self.heartbeat(ws, interval))
            try:
                async for message in ws:
                    if message.type == aiohttp.WSMsgType.TEXT:
                        payload = json.loads(message.data)
                        if await self.on_payload(payload):
                            log("服务端要求重新连接", "WARN")
                            break
                    elif message.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        log(f"连接关闭：{ws.close_code}", "WARN")
                        break
            finally:
                self.connected = False
                heartbeat.cancel()

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
            event = payload.get("t")
            data = payload.get("d") or {}
            if event == "READY":
                self.session_id = data.get("session_id")
                self.connected = True
                self.last_error = ""
                user = data.get("user") or {}
                log(f"机器人已上线：{user.get('username', '未知')} session={self.session_id}")
            elif event == "C2C_MESSAGE_CREATE":
                asyncio.create_task(self.on_message(data))
            elif event == "INTERACTION_CREATE":
                asyncio.create_task(self.on_interaction(data))
        elif op in (7, 9):
            return True
        return False
