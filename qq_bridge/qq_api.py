# -*- coding: utf-8 -*-
"""QQ 开放平台 HTTP 客户端（v2）。

只覆盖本项目用到的接口：token、网关、单聊消息、富媒体上传、互动事件应答。
"""

import asyncio
import base64
import json
import time

import aiohttp

from .errors import QQAPIError

API_BASE = "https://api.bot.qq.com"
TOKEN_URL = "https://api.bot.qq.com/app/getAppAccessToken"
MAX_REPLY_PER_MSG = 4


class QQClient:
    def __init__(self, session, appid, secret):
        self.session = session
        self.appid = appid
        self.secret = secret
        self.token = None
        self.expire_at = 0
        self.lock = asyncio.Lock()
        self.seq_map = {}

    async def access_token(self):
        async with self.lock:
            if self.token and time.time() < self.expire_at - 60:
                return self.token
            async with self.session.post(
                TOKEN_URL,
                json={"appId": self.appid, "clientSecret": self.secret},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as response:
                text = await response.text()
            try:
                data = json.loads(text)
            except Exception:
                raise QQAPIError(f"获取 access_token 返回非 JSON：HTTP {response.status} {text[:200]}")
            if "access_token" not in data:
                raise QQAPIError(f"获取 access_token 失败：{text[:300]}")
            self.token = data["access_token"]
            self.expire_at = time.time() + int(data.get("expires_in", 7200))
            print("[INFO] access_token 已刷新", flush=True)
            return self.token

    async def api(self, method, path, payload=None, timeout=60):
        token = await self.access_token()
        headers = {"Authorization": f"QQBot {token}", "Content-Type": "application/json"}
        async with self.session.request(
            method,
            API_BASE + path,
            headers=headers,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as response:
            text = await response.text()
        data = {}
        if text:
            try:
                data = json.loads(text)
            except Exception:
                data = {}
        code = data.get("code") if isinstance(data, dict) else None
        if response.status >= 300 or code not in (None, 0):
            message = ""
            if isinstance(data, dict):
                message = data.get("message") or data.get("msg") or ""
            raise QQAPIError(
                f"{method} {path} -> HTTP {response.status} code={code} {message or text[:200]}",
                code=code,
            )
        return data

    async def gateway(self):
        data = await self.api("GET", "/gateway")
        url = data.get("url")
        if not url:
            raise QQAPIError(f"获取网关失败：{str(data)[:200]}")
        return url

    def next_seq(self, msg_id):
        count = self.seq_map.get(msg_id, 0) + 1
        self.seq_map[msg_id] = count
        if len(self.seq_map) > 500:
            self.seq_map.pop(next(iter(self.seq_map)))
        return min(count, MAX_REPLY_PER_MSG)

    async def send(self, openid, content=None, markdown=None, keyboard=None,
                   msg_id=None, event_id=None, media=None):
        payload = {}
        if media:
            payload["msg_type"] = 7
            payload["media"] = {"file_info": media}
            if content:
                payload["content"] = content
        elif markdown is not None:
            payload["msg_type"] = 2
            payload["markdown"] = {"content": markdown}
        else:
            payload["msg_type"] = 0
            payload["content"] = content or ""

        if keyboard:
            payload["keyboard"] = keyboard

        if event_id:
            payload["event_id"] = event_id
        elif msg_id:
            payload["msg_id"] = msg_id
            payload["msg_seq"] = self.next_seq(msg_id)

        await self.api("POST", f"/v2/users/{openid}/messages", payload)

    async def send_text(self, openid, content, msg_id=None, event_id=None):
        await self.send(openid, content=content, msg_id=msg_id, event_id=event_id)

    async def send_menu(self, openid, markdown, keyboard, msg_id=None, event_id=None):
        await self.send(openid, markdown=markdown, keyboard=keyboard,
                        msg_id=msg_id, event_id=event_id)

    async def upload_image(self, openid, data):
        file_data = base64.b64encode(data).decode()
        payload = {
            "file_type": 1,
            "url": "",
            "srv_send_msg": False,
            "file_data": file_data,
        }
        result = await self.api("POST", f"/v2/users/{openid}/files", payload, timeout=120)
        info = result.get("file_info")
        if not info:
            raise QQAPIError(f"上传图片未返回 file_info：{str(result)[:200]}")
        return info

    async def send_image(self, openid, file_info, content="", msg_id=None, event_id=None):
        await self.send(openid, content=content, media=file_info,
                        msg_id=msg_id, event_id=event_id)

    async def respond_interaction(self, interaction_id, code=0):
        if not interaction_id:
            return
        await self.api("PUT", f"/interactions/{interaction_id}", {"code": code}, timeout=10)
