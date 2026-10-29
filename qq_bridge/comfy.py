# -*- coding: utf-8 -*-
"""ComfyUI HTTP 客户端：上传图片、提交任务、等待结果、取图。"""

import asyncio
import json
import re
import time
import uuid

import aiohttp

from .errors import ComfyError
from .workflow import pick_images


def _safe_segment(text, limit=16):
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "", str(text or ""))
    return cleaned[:limit] or "user"


class ComfyClient:
    def __init__(self, session, base_url, request_timeout=30):
        self.session = session
        self.base_url = str(base_url or "http://127.0.0.1:8188").rstrip("/")
        self.request_timeout = request_timeout

    async def health(self):
        async with self.session.get(
            f"{self.base_url}/system_stats",
            timeout=aiohttp.ClientTimeout(total=3),
        ) as response:
            if response.status != 200:
                raise ComfyError(f"ComfyUI HTTP {response.status}")
            return await response.json()

    async def object_info(self):
        async with self.session.get(
            f"{self.base_url}/object_info",
            timeout=aiohttp.ClientTimeout(total=30),
        ) as response:
            if response.status != 200:
                raise ComfyError(f"获取 object_info 失败：HTTP {response.status}")
            return await response.json()

    async def queue_status(self):
        async with self.session.get(
            f"{self.base_url}/queue",
            timeout=aiohttp.ClientTimeout(total=5),
        ) as response:
            return await response.json()

    async def upload_image(self, data, openid, filename=None):
        """上传到 input/qq_bridge/<openid>/，返回 LoadImage 可用的路径。"""
        filename = filename or f"qq_{uuid.uuid4().hex[:12]}.png"
        subfolder = f"qq_bridge/{_safe_segment(openid)}"
        form = aiohttp.FormData()
        form.add_field("image", data, filename=filename, content_type="image/png")
        form.add_field("overwrite", "true")
        form.add_field("subfolder", subfolder)
        form.add_field("type", "input")
        try:
            async with self.session.post(
                f"{self.base_url}/upload/image",
                data=form,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as response:
                text = await response.text()
                if response.status != 200:
                    raise ComfyError(f"上传图片失败 HTTP {response.status}：{text[:200]}")
                payload = json.loads(text)
        except aiohttp.ClientError as exc:
            raise ComfyError(f"无法连接 ComfyUI（{exc}），请确认它已启动")

        name = payload.get("name")
        returned_subfolder = payload.get("subfolder") or subfolder
        if not name:
            raise ComfyError(f"上传图片未返回文件名：{text[:200]}")
        return f"{returned_subfolder}/{name}" if returned_subfolder else name

    async def submit(self, workflow, client_id=None):
        payload = {"prompt": workflow}
        if client_id:
            payload["client_id"] = client_id
        try:
            async with self.session.post(
                f"{self.base_url}/prompt",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                text = await response.text()
                if response.status != 200:
                    raise ComfyError(self._format_submit_error(text, response.status))
                data = json.loads(text)
        except aiohttp.ClientError as exc:
            raise ComfyError(f"无法连接 ComfyUI（{exc}），请确认它已启动")

        prompt_id = data.get("prompt_id")
        if not prompt_id:
            raise ComfyError(f"ComfyUI 未返回 prompt_id：{text[:200]}")
        return prompt_id

    @staticmethod
    def _format_submit_error(text, status):
        try:
            data = json.loads(text)
        except Exception:
            return f"提交任务失败 HTTP {status}：{text[:300]}"

        parts = []
        error = data.get("error") or {}
        if isinstance(error, dict) and error.get("message"):
            parts.append(str(error["message"]))
        for node_id, detail in (data.get("node_errors") or {}).items():
            for item in (detail or {}).get("errors") or []:
                message = item.get("message") or item.get("type")
                if message:
                    parts.append(f"节点 {node_id}：{message}")
        if not parts:
            parts.append(text[:300])
        return "提交任务失败：" + "；".join(parts[:3])

    async def wait(self, prompt_id, output_node, timeout):
        deadline = time.time() + max(10, int(timeout))
        while time.time() < deadline:
            await asyncio.sleep(1)
            try:
                async with self.session.get(
                    f"{self.base_url}/history/{prompt_id}",
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as response:
                    history = await response.json()
            except aiohttp.ClientError as exc:
                raise ComfyError(f"查询生成结果失败（{exc}）")

            if prompt_id not in history:
                continue

            entry = history[prompt_id]
            status = entry.get("status") or {}
            if status.get("status_str") == "error":
                detail = json.dumps(status.get("messages", ""), ensure_ascii=False)[:300]
                raise ComfyError(f"ComfyUI 执行出错：{detail}")

            images = pick_images(entry, output_node)
            if not images:
                continue

            info = images[0]
            params = {
                "filename": info.get("filename", ""),
                "subfolder": info.get("subfolder", ""),
                "type": info.get("type", "output"),
            }
            async with self.session.get(
                f"{self.base_url}/view",
                params=params,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as response:
                data = await response.read()
            if not data:
                raise ComfyError("取回的图片为空")
            return data

        raise ComfyError(f"生成超时（{int(timeout)} 秒）")

    async def run(self, workflow, output_node, timeout):
        prompt_id = await self.submit(workflow)
        return await self.wait(prompt_id, output_node, timeout)

    async def interrupt(self):
        async with self.session.post(
            f"{self.base_url}/interrupt",
            timeout=aiohttp.ClientTimeout(total=5),
        ) as response:
            await response.read()
