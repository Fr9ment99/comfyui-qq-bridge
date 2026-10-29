# -*- coding: utf-8 -*-
"""把用户输入注入工作流 JSON。

关键规则：
1. targets 里每个角色都是数组，允许一个角色写多个节点（例如两处 seed）。
2. 目标输入如果当前是 ["节点", 0] 形式，说明被连线占用，直接写入会切断上游链路；
   这种配置在导入时就应该被拦下，运行期再兜底报错。
3. 图片类输入不能直接塞二进制，必须先上传到 ComfyUI 的 input 目录，再写文件名。
4. join 支持 replace / prefix / append，用于把用户输入拼到原有文字的前面或后面。
"""

import copy
import random

from .errors import ProfileError

IMAGE_ROLES = {"image", "mask"}


def _apply_join(current, value, target):
    join = str(target.get("join") or "replace")
    if join == "replace":
        return value
    separator = target.get("separator", "\n")
    if join == "prefix":
        return f"{value}{separator}" if value else current
    if join == "append":
        base = current if isinstance(current, str) else ""
        if not base:
            return value
        return f"{base}{separator}{value}"
    raise ProfileError(f"未知的 join 类型：{join}")


def _pick_output_node(profile):
    return str((profile.get("output") or {}).get("node") or "")


async def build_prompt(profile, inputs, comfy, openid, seed=None):
    """返回 (填充好的工作流, 实际使用的种子)。inputs 里图片是 bytes。"""
    workflow = copy.deepcopy(profile["workflow"])
    targets = profile.get("targets") or {}
    inputs = dict(inputs)
    seed = int(seed if seed is not None else random.randint(1, 2 ** 32 - 1))
    if "seed" in targets:
        inputs["seed"] = seed

    for role, items in targets.items():
        value = inputs.get(role)
        if value is None or value == "":
            continue

        if role in IMAGE_ROLES:
            if not isinstance(value, (bytes, bytearray)):
                raise ProfileError(f"{role} 需要图片字节，实际收到 {type(value).__name__}")
            value = await comfy.upload_image(bytes(value), openid)
        elif role in ("seed", "width", "height", "batch", "strength"):
            try:
                value = int(value) if role in ("seed", "width", "height", "batch") else float(value)
            except (TypeError, ValueError):
                raise ProfileError(f"{role} 的取值不是数字：{value!r}")

        for target in items:
            node_id = str(target.get("node") or "")
            node = workflow.get(node_id)
            if node is None:
                raise ProfileError(f"档案指向的节点 {node_id} 不在工作流里")

            if "link" in target:
                link = target["link"]
                if not (isinstance(link, list) and len(link) == 2):
                    raise ProfileError(f"节点 {node_id} 的 link 配置不合法")
                node.setdefault("inputs", {})[target["input"]] = list(link)
                continue

            input_name = str(target.get("input") or "")
            node_inputs = node.setdefault("inputs", {})
            if input_name not in node_inputs:
                raise ProfileError(f"节点 {node_id} 没有输入 {input_name}")
            current = node_inputs[input_name]
            if isinstance(current, list):
                raise ProfileError(
                    f"节点 {node_id}.{input_name} 被上游连线占用，无法写入；请改选字面量节点"
                )
            node_inputs[input_name] = _apply_join(current, value, target)

    # 文生图没有空 latent 节点时，尺寸角色可能不存在，这里不额外处理
    return workflow, seed


def output_node_of(profile):
    return _pick_output_node(profile)


def pick_images(history_entry, output_node):
    """从 /history 的 outputs 里取图片描述；优先使用档案指定的节点。"""
    outputs = history_entry.get("outputs") or {}
    order = []
    if output_node:
        order.append(str(output_node))
    order.extend([nid for nid in outputs if str(nid) not in order])
    for node_id in order:
        images = (outputs.get(node_id) or {}).get("images") or []
        if images:
            return images
    return []
