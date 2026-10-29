# -*- coding: utf-8 -*-
"""配置读写。

设计要点：
1. 所有可调项都有默认值，旧版配置（只有 appid/appsecret/openids/workflow）可以直接读。
2. 密钥支持环境变量覆盖，方便容器或 CI 使用。
3. 写入采用"临时文件 + 原子替换"，避免进程被杀时留下半个 JSON。
"""

import copy
import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_FILE = BASE_DIR / "qq_bot_config.json"
PROMPTS_FILE = BASE_DIR / "prompts.json"
PROFILES_DIR = BASE_DIR / "workflows"

DEFAULTS = {
    "appid": "",
    "appsecret": "",
    "openids": [],
    "autostart": True,
    # 单用户自用场景：空白名单时把第一个说话的账号绑定为安装者。
    # 如果把机器人公开出去，请先手动填好 openids，再关掉这个开关。
    "auto_bind_first_user": True,
    "comfyui_url": "http://127.0.0.1:8188",
    "status_port": 8189,
    "queue_limit": 3,
    # 0 表示使用档案里的 timeout
    "gen_timeout": 0,
    "default_width": 1024,
    "default_height": 1024,
    # 各模式当前启用的档案 id；为空时自动使用该模式下唯一的档案
    "active_profiles": {"txt2img": "", "img2img": "", "inpaint": ""},
    # 发送按钮失败后置为 False，避免每次都撞一次错误
    "keyboard_ok": True,
    "max_image_bytes": 3500000,
    "language": "zh",
}

CONFIG_KEYS = tuple(DEFAULTS.keys())


def _merge_defaults(cfg):
    merged = copy.deepcopy(DEFAULTS)
    for key, value in (cfg or {}).items():
        if key in ("active_profiles",) and isinstance(value, dict):
            merged["active_profiles"].update(
                {k: str(v or "") for k, v in value.items() if k in merged["active_profiles"]}
            )
        elif key in DEFAULTS:
            merged[key] = value
    return merged


def load_config():
    cfg = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                cfg = data
        except Exception as exc:  # 配置坏了也要能启动，用默认值兜底
            print(f"[WARN] 读取配置失败，使用默认值：{exc}", flush=True)

    cfg = _merge_defaults(cfg)

    if os.environ.get("QQ_APPID"):
        cfg["appid"] = os.environ["QQ_APPID"]
    if os.environ.get("QQ_APPSECRET"):
        cfg["appsecret"] = os.environ["QQ_APPSECRET"]

    cfg["appid"] = str(cfg.get("appid") or "").strip()
    cfg["appsecret"] = str(cfg.get("appsecret") or "").strip()
    cfg["openids"] = [str(x).strip() for x in (cfg.get("openids") or []) if str(x).strip()]
    cfg["comfyui_url"] = str(cfg.get("comfyui_url") or DEFAULTS["comfyui_url"]).rstrip("/")
    cfg["queue_limit"] = max(1, int(cfg.get("queue_limit") or 3))
    cfg["status_port"] = int(cfg.get("status_port") or 8189)
    cfg["gen_timeout"] = max(0, int(cfg.get("gen_timeout") or 0))
    cfg["default_width"] = max(64, int(cfg.get("default_width") or 1024))
    cfg["default_height"] = max(64, int(cfg.get("default_height") or 1024))
    cfg["max_image_bytes"] = max(100_000, int(cfg.get("max_image_bytes") or 3_500_000))
    cfg["auto_bind_first_user"] = bool(cfg.get("auto_bind_first_user", True))
    cfg["keyboard_ok"] = bool(cfg.get("keyboard_ok", True))
    return cfg


def save_config(cfg):
    """只保存已知字段，并保持 active_profiles 的结构完整。"""
    data = {}
    for key in CONFIG_KEYS:
        if key in cfg:
            data[key] = cfg[key]
    data["active_profiles"] = {
        "txt2img": str((cfg.get("active_profiles") or {}).get("txt2img") or ""),
        "img2img": str((cfg.get("active_profiles") or {}).get("img2img") or ""),
        "inpaint": str((cfg.get("active_profiles") or {}).get("inpaint") or ""),
    }
    tmp = CONFIG_FILE.with_name(CONFIG_FILE.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, CONFIG_FILE)


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
