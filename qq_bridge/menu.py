# -*- coding: utf-8 -*-
"""按钮菜单与文字菜单。

按钮使用 keyboard.content（自定义键盘），不用 keyboard.id 模板：
模板 ID 按机器人申请，无法随插件分发给其他安装者。
发送失败时上层会降级为文字菜单。
"""

MODE_ALIASES = {
    "1": "txt2img", "2": "img2img", "3": "inpaint", "4": "again",
    "txt2img": "txt2img", "t2i": "txt2img", "text2img": "txt2img", "文生图": "txt2img",
    "img2img": "img2img", "i2i": "img2img", "图生图": "img2img",
    "inpaint": "inpaint", "重绘": "inpaint", "局部重绘": "inpaint",
    "again": "again", "再来一张": "again",
}

BUTTONS = [
    ("m_t2i", "文生图", "/mode txt2img", 1),
    ("m_i2i", "图生图", "/mode img2img", 1),
    ("m_paint", "局部重绘", "/mode inpaint", 1),
    ("m_again", "再来一张", "/again", 0),
]


def normalize_mode(text):
    return MODE_ALIASES.get(str(text or "").strip().lower())


def keyboard_payload():
    buttons = []
    for button_id, label, data, style in BUTTONS:
        buttons.append({
            "id": button_id,
            "render_data": {"label": label, "visited_label": label, "style": style},
            "action": {
                "type": 2,
                "permission": {"type": 2},
                "data": data,
                "enter": True,
                "reply": False,
                "unsupport_tips": "请升级到最新版手机 QQ",
            },
        })
    return {"content": {"rows": [{"buttons": buttons}]}}


def markdown_menu(hint="", active_line=""):
    lines = ["**请选择模式**"]
    if hint:
        lines.append(hint)
    lines.append("")
    lines.append("文生图：发送文字")
    lines.append("图生图：发送图片，可附一句描述")
    lines.append("局部重绘：发送图片，并附「区域 | 重绘内容」")
    lines.append("再来一张：复用上一次的参数")
    if active_line:
        lines.append("")
        lines.append(active_line)
    lines.append("")
    lines.append("切换档案：/profiles")
    return "\n".join(lines)


def text_menu(hint="", active_line=""):
    lines = ["请选择模式（回复序号）："]
    if hint:
        lines.append(hint)
    lines.append("1. 文生图")
    lines.append("2. 图生图")
    lines.append("3. 局部重绘")
    lines.append("4. 再来一张")
    if active_line:
        lines.append("")
        lines.append(active_line)
    lines.append("")
    lines.append("切换档案：/profiles")
    return "\n".join(lines)


def mode_prompt(mode, profile_label=""):
    suffix = f"\n档案：{profile_label}" if profile_label else ""
    if mode == "txt2img":
        return "已选择「文生图」。请发送提示词。" + suffix
    if mode == "img2img":
        return "已选择「图生图」。请发送图片，可附一句描述。" + suffix
    if mode == "inpaint":
        return (
            "已选择「局部重绘」。请发送图片，并附：区域 | 重绘内容\n"
            "例如：猫 | 一只穿西装的橘猫"
            + suffix
        )
    return "未知模式"


def split_region(text):
    """把「猫 | 一只狗」拆成 (分割目标, 重绘内容, 是否显式指定)。"""
    text = (text or "").strip()
    if "|" in text:
        region, draw = text.split("|", 1)
        return region.strip(), draw.strip(), True
    return text, text, False
