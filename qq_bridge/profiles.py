# -*- coding: utf-8 -*-
"""工作流档案：一份 API 格式工作流 + 一张"角色 -> 节点输入"的映射表。

档案就是单个 JSON 文件，放在 workflows/ 下：

{
  "id": "demo-inpaint",
  "label": "局部重绘",
  "mode": "inpaint",
  "workflow": { ...ComfyUI 导出的 API 格式... },
  "targets": {
    "image":         [{"node": "125", "input": "image"}],
    "region_prompt": [{"node": "86",  "input": "prompt"}],
    "draw_prompt":   [{"node": "77",  "input": "text"}],
    "seed":          [{"node": "79",  "input": "seed"},
                      {"node": "105", "input": "seed"}]
  },
  "output": {"node": "82"},
  "timeout": 300,
  "defaults": {"strength": 0.6}
}

targets 的值一律是数组：同一份工作流里种子可能同时喂给基础采样和精修采样，
只写一个会让两次采样用的种子不一致。
"""

import json
import os
import re

from .config import PROFILES_DIR
from .errors import ProfileError

MODES = ("txt2img", "img2img", "inpaint")
MODE_LABELS = {"txt2img": "文生图", "img2img": "图生图", "inpaint": "局部重绘"}
ROLE_NAMES = (
    "prompt", "negative", "region_prompt", "draw_prompt",
    "image", "mask", "seed", "width", "height", "strength", "batch",
)
REQUIRED_ROLES = {
    "txt2img": ("prompt", "seed"),
    "img2img": ("image", "seed"),
    "inpaint": ("image", "region_prompt", "draw_prompt", "seed"),
}

_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

# 自动识别时，这些输入名不会被当成"可写入的文字槽"
_TEXT_BLACKLIST = {
    "action", "task", "tidy_tags", "clip_name", "unet_name", "vae_name",
    "lora_name", "sampler_name", "scheduler", "model_name", "type", "device",
    "output_mode", "background", "background_color", "prompt_style", "style",
    "precision", "convert_to_safetensors", "mode_type",
}


def ensure_dir():
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)


def profile_path(profile_id):
    return PROFILES_DIR / f"{profile_id}.json"


def load_profile(profile_id):
    path = profile_path(profile_id)
    if not path.is_file():
        raise ProfileError(f"找不到工作流档案：{profile_id}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ProfileError(f"档案 {profile_id} 不是合法 JSON：{exc}")
    errors = validate_profile(data)
    if errors:
        raise ProfileError(f"档案 {profile_id} 校验失败：{errors[0]}")
    return data


def save_profile(data):
    errors = validate_profile(data)
    if errors:
        raise ProfileError("；".join(errors))
    ensure_dir()
    path = profile_path(data["id"])
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return data["id"]


def delete_profile(profile_id):
    path = profile_path(profile_id)
    if path.is_file():
        path.unlink()
        return True
    return False


def list_profiles():
    ensure_dir()
    result = []
    for path in sorted(PROFILES_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            result.append({
                "id": path.stem, "label": path.stem, "mode": "?",
                "valid": False, "error": f"JSON 解析失败：{exc}",
            })
            continue
        errors = validate_profile(data)
        result.append({
            "id": str(data.get("id") or path.stem),
            "label": str(data.get("label") or path.stem),
            "mode": str(data.get("mode") or "?"),
            "timeout": data.get("timeout"),
            "roles": sorted((data.get("targets") or {}).keys()),
            "valid": not errors,
            "error": errors[0] if errors else "",
        })
    return result


def resolve_active(mode, cfg):
    """返回该模式当前生效的档案；只有一个候选时自动选中。"""
    active = str((cfg.get("active_profiles") or {}).get(mode) or "")
    if active:
        profile = load_profile(active)
        if profile.get("mode") != mode:
            raise ProfileError(
                f"档案 {active} 属于 {MODE_LABELS.get(profile.get('mode'), profile.get('mode'))}，"
                f"不能用在 {MODE_LABELS.get(mode, mode)}"
            )
        return profile

    candidates = [p for p in list_profiles() if p["mode"] == mode and p["valid"]]
    if len(candidates) == 1:
        return load_profile(candidates[0]["id"])
    if not candidates:
        raise ProfileError(
            f"还没有为「{MODE_LABELS.get(mode, mode)}」配置工作流档案。"
            f"请在 ComfyUI 右下角徽标里导入对应模式的 API 工作流文件，或把档案放到 workflows/ 目录。"
        )
    ids = "、".join(p["id"] for p in candidates)
    raise ProfileError(
        f"「{MODE_LABELS.get(mode, mode)}」有多个档案（{ids}），请先发送 /use 档案id 选择。"
    )


def validate_profile(data):
    errors = []
    if not isinstance(data, dict):
        return ["档案必须是 JSON 对象"]

    profile_id = str(data.get("id") or "")
    if not _ID_RE.match(profile_id):
        errors.append("id 只能包含字母、数字、下划线、点和短横线")
    if not str(data.get("label") or "").strip():
        errors.append("缺少 label")
    mode = data.get("mode")
    if mode not in MODES:
        errors.append(f"mode 必须是 {'/'.join(MODES)} 之一")

    workflow = data.get("workflow")
    if not isinstance(workflow, dict) or not workflow:
        errors.append("缺少 workflow（API 格式的节点字典）")
        return errors

    targets = data.get("targets")
    if not isinstance(targets, dict) or not targets:
        errors.append("缺少 targets")
        return errors

    for role, items in targets.items():
        if role not in ROLE_NAMES:
            errors.append(f"未知角色：{role}")
            continue
        if not isinstance(items, list) or not items:
            errors.append(f"targets.{role} 必须是非空数组")
            continue
        for item in items:
            if not isinstance(item, dict):
                errors.append(f"targets.{role} 里有非法条目")
                continue
            node_id = str(item.get("node") or "")
            if node_id not in workflow:
                errors.append(f"targets.{role} 指向的节点 {node_id or '?'} 不在工作流里")
                continue
            if "link" in item:
                link = item["link"]
                if not (isinstance(link, list) and len(link) == 2 and str(link[0]) in workflow):
                    errors.append(f"targets.{role} 的 link 必须形如 [\"节点\", 0]")
                continue
            input_name = str(item.get("input") or "")
            inputs = workflow[node_id].get("inputs") or {}
            if input_name not in inputs:
                errors.append(f"节点 {node_id} 没有输入 {input_name or '?'}")
            elif isinstance(inputs[input_name], list):
                errors.append(
                    f"节点 {node_id} 的输入 {input_name} 已被连线占用，"
                    f"请改选上游的字面量节点，或用 link 改写"
                )

    output = data.get("output") or {}
    if not isinstance(output, dict) or str(output.get("node") or "") not in workflow:
        errors.append("output.node 必须指向工作流里的输出节点")

    if mode in REQUIRED_ROLES:
        for role in REQUIRED_ROLES[mode]:
            if not (targets.get(role) or []):
                errors.append(f"{MODE_LABELS[mode]}模式缺少必需角色：{role}")

    return errors


def validate_against_object_info(profile, object_info):
    """用 ComfyUI 的 /object_info 检查 class_type 和输入名是否真实存在。"""
    issues = []
    workflow = profile.get("workflow") or {}
    for role, items in (profile.get("targets") or {}).items():
        for item in items:
            node_id = str(item.get("node") or "")
            node = workflow.get(node_id) or {}
            cls = node.get("class_type")
            if cls not in object_info:
                issues.append(f"节点 {node_id} 的 class_type「{cls}」在当前 ComfyUI 里不存在（缺少自定义节点？）")
                continue
            if "link" in item:
                continue
            spec = (object_info.get(cls) or {}).get("input") or {}
            names = set((spec.get("required") or {}).keys()) | set((spec.get("optional") or {}).keys())
            if names and item.get("input") not in names:
                issues.append(f"节点 {node_id}（{cls}）没有输入「{item.get('input')}」")
    return issues


# --------------------------------------------------------------------------
# 自动识别：从一份 API 工作流里猜出角色映射
# --------------------------------------------------------------------------

def _is_sampler(cls):
    low = cls.lower()
    return "ksampler" in low or "sampler" in low or "upscale" in low


def _is_upscaler(cls):
    return "upscale" in cls.lower()


def _literal_text_input(node, prefer=("text_a", "text", "prompt", "string")):
    inputs = node.get("inputs") or {}
    for key in prefer:
        if isinstance(inputs.get(key), str):
            return key
    for key, value in inputs.items():
        if isinstance(value, str) and key not in _TEXT_BLACKLIST:
            return key
    return None


def autodetect(workflow):
    """返回 {mode, targets, output, warnings}。全部基于 class_type 和连线关系猜测。"""
    warnings = []
    targets = {}

    samplers = []
    for node_id, node in workflow.items():
        cls = str(node.get("class_type") or "")
        if _is_sampler(cls):
            samplers.append((node_id, node, cls))

    def link_of(node, key):
        value = (node.get("inputs") or {}).get(key)
        if isinstance(value, list) and len(value) == 2 and str(value[0]) in workflow:
            return str(value[0])
        return None

    # 正/负提示词：优先顺着 KSampler 的 positive/negative 连线找
    prompt_found = negative_found = False
    for _, node, cls in samplers:
        if _is_upscaler(cls):
            continue
        pos_id = link_of(node, "positive")
        neg_id = link_of(node, "negative")
        if pos_id and not prompt_found:
            cand = workflow[pos_id]
            text_key = _literal_text_input(cand, ("text",))
            if text_key:
                targets["prompt"] = [{"node": pos_id, "input": text_key}]
                prompt_found = True
            else:
                linked = (cand.get("inputs") or {}).get("text")
                if isinstance(linked, list) and len(linked) == 2 and str(linked[0]) in workflow:
                    upstream_id = str(linked[0])
                    upstream = workflow[upstream_id]
                    upstream_key = _literal_text_input(upstream)
                    if upstream_key:
                        targets["prompt"] = [{
                            "node": upstream_id, "input": upstream_key,
                            "join": "prefix", "separator": "\n",
                        }]
                        prompt_found = True
                        warnings.append(
                            f"节点 {pos_id}.text 被节点 {upstream_id} 的输出占用，"
                            f"已改为写入 {upstream_id}.{upstream_key}（前缀拼接）"
                        )
        if neg_id and not negative_found:
            cand = workflow[neg_id]
            text_key = _literal_text_input(cand, ("text",))
            if text_key:
                targets["negative"] = [{"node": neg_id, "input": text_key}]
                negative_found = True
        if prompt_found and negative_found:
            break

    # 图片输入
    for node_id, node in workflow.items():
        cls = str(node.get("class_type") or "")
        if cls == "LoadImage" and "image" not in targets:
            targets["image"] = [{"node": node_id, "input": "image"}]
        elif cls == "LoadImageMask" and "mask" not in targets:
            targets["mask"] = [{"node": node_id, "input": "image"}]

    # 文字分割（SAM / GroundingDINO 之类）：带 prompt 文本输入的分割节点
    for node_id, node in workflow.items():
        cls = str(node.get("class_type") or "")
        inputs = node.get("inputs") or {}
        if "region_prompt" in targets:
            break
        if "sam" in cls.lower() or "segment" in cls.lower() or "grounding" in cls.lower():
            for key in ("prompt", "text", "text_prompt", "positive"):
                if isinstance(inputs.get(key), str):
                    targets["region_prompt"] = [{"node": node_id, "input": key}]
                    break

    # 重绘描述：分割节点之外、有字面量 text 的 CLIPTextEncode
    if "region_prompt" in targets and "prompt" in targets:
        for role in ("prompt",):
            for item in targets.get(role, []):
                node = workflow.get(item["node"]) or {}
                if str(node.get("class_type")) == "CLIPTextEncode":
                    targets["draw_prompt"] = [dict(item)]
                    break

    # 种子：所有采样器 / 放大精修节点
    seeds = []
    for node_id, node, cls in samplers:
        inputs = node.get("inputs") or {}
        for key in ("seed", "noise_seed"):
            if isinstance(inputs.get(key), (int, float)):
                seeds.append({"node": node_id, "input": key})
                break
    if seeds:
        targets["seed"] = seeds

    # 尺寸：顺便记住工作流自己的宽高，导入档案时作为默认值
    defaults = {}
    for node_id, node in workflow.items():
        cls = str(node.get("class_type") or "")
        if cls.startswith("Empty") and "Latent" in cls:
            inputs = node.get("inputs") or {}
            if isinstance(inputs.get("width"), (int, float)) and isinstance(inputs.get("height"), (int, float)):
                targets["width"] = [{"node": node_id, "input": "width"}]
                targets["height"] = [{"node": node_id, "input": "height"}]
                defaults = {"width": int(inputs["width"]), "height": int(inputs["height"])}
                break

    # 强度（采样器的 denoise）
    for node_id, node, cls in samplers:
        if _is_upscaler(cls):
            continue
        if isinstance((node.get("inputs") or {}).get("denoise"), (int, float)):
            targets["strength"] = [{"node": node_id, "input": "denoise"}]
            break

    # 输出
    saves = [nid for nid, node in workflow.items() if str(node.get("class_type")) == "SaveImage"]
    output = {"node": saves[0]} if saves else {}
    if len(saves) > 1:
        warnings.append(f"工作流里有 {len(saves)} 个 SaveImage，默认使用节点 {saves[0]}")

    if "region_prompt" in targets and "image" in targets:
        mode = "inpaint"
    elif "image" in targets:
        mode = "img2img"
    else:
        mode = "txt2img"

    missing = [r for r in REQUIRED_ROLES[mode] if r not in targets]
    if missing:
        warnings.append(f"自动识别缺少必需角色：{'、'.join(missing)}，请手动补充")

    if not output:
        warnings.append("没有找到 SaveImage 节点，无法取图")
    if "width" not in targets and mode == "txt2img":
        warnings.append("没有找到空 latent 节点，文生图无法自动改尺寸")

    return {
        "mode": mode, "targets": targets, "output": output,
        "defaults": defaults, "warnings": warnings,
    }


def check_workflow(mode, workflow):
    """导入前的节点体检，返回 (问题列表, 自动识别结果)。问题为空表示可以存成档案。"""
    detected = autodetect(workflow)
    targets = detected["targets"]
    issues = []

    if not targets.get("seed"):
        issues.append("没有找到采样器（KSampler）的 seed")

    if mode == "txt2img":
        if not targets.get("prompt"):
            issues.append("没有找到文本输入节点：需要一个能直接写字的 CLIPTextEncode")
        if not detected["output"]:
            issues.append("没有找到图片输出节点：需要一个 SaveImage")
    elif mode == "img2img":
        if not targets.get("image"):
            issues.append("没有找到图片输入节点：需要一个 LoadImage")
        if not detected["output"]:
            issues.append("没有找到图片输出节点：需要一个 SaveImage")
    elif mode == "inpaint":
        if not targets.get("image"):
            issues.append("没有找到图片输入节点：需要一个 LoadImage")
        if not targets.get("region_prompt"):
            issues.append("没有找到遮罩文本节点：需要一个带文字输入的 SAM3 分割节点")
        if not targets.get("draw_prompt"):
            issues.append("没有找到重绘描述节点：需要一个新的 CLIPTextEncode")
        if not detected["output"]:
            issues.append("没有找到图片输出节点：需要一个 SaveImage")

    if not issues and detected["mode"] != mode:
        issues.append(
            f"这个文件看起来是「{MODE_LABELS.get(detected['mode'], detected['mode'])}」工作流，"
            f"请用对应的导入按钮"
        )
    return issues, detected
