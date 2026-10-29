# -*- coding: utf-8 -*-
"""QQ 机器人 <-> 本地 ComfyUI 桥接核心包。

模块划分：
- config    配置读写与默认值
- profiles  工作流档案（API 工作流 + 角色映射）的加载、校验、自动识别
- workflow  把用户输入注入工作流 JSON
- comfy     ComfyUI HTTP 客户端
- qq_api    QQ 开放平台 HTTP 客户端
- session   每用户会话与最近结果缓存
- menu      按钮菜单与文字菜单
- bot       消息路由、命令处理、生成队列
- app       进程入口
"""

__all__ = ["config", "profiles", "workflow", "comfy", "qq_api", "session", "menu", "bot"]
