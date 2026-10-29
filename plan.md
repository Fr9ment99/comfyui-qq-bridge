# comfyui-qq-bridge 开源与通用化改造计划

> 目标：把一个"只在我这台机器上能跑"的自用插件，变成"别人 clone 下来、填两个密钥、导入一个工作流就能跑"的开源项目。
> 本文只做规划，不含代码改动。

---

## 0. 一句话结论

功能本身是成立的（QQ 官方机器人 C2C 消息 → 本地 ComfyUI 出图 → 富媒体回传），**真正阻碍开源的不是功能，而是三样东西：写死的工作流/节点 ID、写死的本机环境、缺失的工程化外壳（README / LICENSE / 依赖声明 / 发布元数据）**。
先做 P0「去私有化 + 补壳」，再谈"支持群聊、多工作流、多平台"这类通用性扩展。**顺序颠倒会白干。**

---

## 1. 现状盘点

### 1.1 组件与职责

| 文件 | 职责 | 评价 |
| --- | --- | --- |
| `__init__.py` | ComfyUI 宿主插件：拉起/重启/停止子进程，注册 `/qq_bridge/*` HTTP 路由，挂载 `./web` | 生命周期管理写得干净，可保留 |
| `qq_comfy_bridge.py` | 独立 asyncio 进程：QQ WebSocket 网关、消息分发、任务队列、调用 ComfyUI、回传图片 | 核心资产，但耦合最重 |
| `qq_bot_panel.py` | Tkinter 桌面控制面板（填密钥、启停、看日志） | 与 Web 徽标功能重叠，且引入 `requests`、Windows 向导 |
| `web/qq_bridge.js` | ComfyUI 前端扩展：右下角状态徽标 + 设置项 | 体验好，是开源后的主入口 |
| `qq_bot_config.example.json` | 配置样例 | 字段太少，需要扩 |
| `启动QQ机器人面板.bat` | Windows 启动脚本 | 平台绑定 |

### 1.2 已经做对、应当保留的设计

- **WebSocket 主动外连**，不需要公网 IP / 端口映射，这是能在家庭宽带、校园网跑起来的关键。
- **被动回复遵守 QQ 频次约束**（`msg_seq` 递增、单条消息最多 4 次回复），符合平台规范。
- **图片 base64 直传 + 超限自动压缩**（`prepare_image`），不依赖公网图床。
- **单实例锁**（用 8189 状态端口占用判断）+ 孤儿进程清理，工程细节到位。
- **子进程与 ComfyUI 主进程隔离**，机器人崩了不影响 ComfyUI。
- **队列串行 + 上限**，符合单 GPU 现实。
- Git 历史干净：6 个提交，**从未提交过 `qq_bot_config.json` / `prompts.json`**，无密钥泄漏。

### 1.3 能力边界（必须在 README 里写清楚，别让用户误解）

> 本节描述的是**当前实现**。已与作者确认的目标形态见 §10（文生图 / 图生图 / 文字分割局部重绘 / 按钮菜单）。

- 只接 **C2C 单聊**（`intents = 1 << 25`，只处理 `C2C_MESSAGE_CREATE`），**不支持群聊 / 频道 / 频道私信**。
- 只做**文生图**，单一工作流，**不支持负面提示词、尺寸、模型、LoRA、图生图、视频**。
- 回复窗口约 5 分钟，`GEN_TIMEOUT = 240s` 是被动回复约束倒推出来的，不是随便写的。
- 白名单为空时**自动绑定第一个发消息的人**——自用很爽，开源后是安全隐患（见 §2 P0-9）。

---

## 2. 阻碍开源的耦合点清单

按优先级排序。P0 = 不改别人就跑不起来；P1 = 影响体验与口碑；P2 = 长期演进。

### P0：私有环境耦合（不改必然"别人装完报错"）

| # | 问题 | 位置 | 影响 | 建议 |
| --- | --- | --- | --- | --- |
| 1 | 默认工作流名写死为 `Z-iMage-turbo基础工作流（文生图）.json` | `qq_comfy_bridge.py:33` | 别人机器上不存在 | 改为 `workflows/` 目录约定 + 配置项；仓库内提供**一个最小示例工作流**（API 格式） |
| 2 | 找不到时**向上翻 4 层父目录**搜索工作流 | `qq_comfy_bridge.py:95-99` | 在自己机器上"能用"的元凶；开源后行为不可预期，还可能误读到别人的文件 | 删除父目录回溯，只认配置路径 + 插件自身 `workflows/` |
| 3 | 节点 ID 写死 `6 / 3 / 13 / 9` | `qq_comfy_bridge.py:38-41` | **任何人换工作流即崩**，这是最大的通用性硬伤 | 配置化"节点映射"（见 §3.1）；缺失时给出可读报错并指向文档 |
| 4 | 分辨率 `768×1280` 写死，帮助文案里也写死 | `qq_comfy_bridge.py:42, 251` | 用户无法改尺寸 | 配置项 `default_width/height`，帮助文案动态生成 |
| 5 | ComfyUI 地址写死 `127.0.0.1:8188` | `qq_comfy_bridge.py:37`，`qq_bot_panel.py:29` | 远程 ComfyUI / 改端口场景直接不可用 | 配置项 `comfyui_url`，默认本机 |
| 6 | 状态端口 `8189` 写死，且插件侧靠固定端口探测 | `qq_comfy_bridge.py:54-56`，`__init__.py:28` | 端口冲突时插件永远显示"未响应" | 端口可配；插件侧改成"先读配置再探测"，端口占用时自动 +1 并回写 |
| 7 | 没有任何依赖声明 | 仓库根 | 手动装 `aiohttp` 才能跑，新手第一步就卡住 | `requirements.txt`（+ 可选 `pyproject.toml`） |
| 8 | 配置项只有 5 个字段 | `qq_bot_config.example.json` | 上面所有可配置项无处安放 | 重设计配置结构（见 §3.1），保留向后兼容的读取逻辑 |
| 9 | 白名单为空 → 自动把第一个陌生人绑定为唯一用户 | `qq_comfy_bridge.py:349-356` | 机器人一旦被公开发现，**第一个发消息的人即获得生图权限**（消耗你的 GPU） | 改为显式开关 `auto_bind_first_user`（默认关闭），或提供"配对码"机制；README 醒目说明 |
| 10 | 无 `README` / `LICENSE` / `CHANGELOG` | 仓库根 | 无法发布、无法被 ComfyUI Manager 收录、别人不知道这是什么 | 见 §4 M0/M1 |

### P1：普适性与体验

| # | 问题 | 位置 | 建议 |
| --- | --- | --- | --- |
| 11 | 两套控制面板重复（Tkinter vs Web 徽标） | `qq_bot_panel.py` | **主线只保留 Web 徽标**（零额外依赖、跨平台）；Tkinter 面板降级为 `tools/` 下的可选调试工具，或直接删除。删除可顺带去掉 `requests` 依赖 |
| 12 | Windows 绑定：`CREATE_NO_WINDOW`、`taskkill`、`.bat` | `__init__.py:30,122-124`，`.bat` | 代码已有 POSIX 分支，但文档与启动方式仍是 Windows 视角；补 `run.sh` 与"直接命令启动"说明，README 标注平台支持情况 |
| 13 | 中文写死在 UI / 日志 / 帮助文本 | `web/qq_bridge.js` 设置项名、`qq_comfy_bridge.py:250-260` | 至少 README 中英双语；用户可见文案抽成常量表（轻量 i18n，不引第三方库） |
| 14 | 队列上限 3、超时 240s、回复上限 4 全部写死 | `qq_comfy_bridge.py:43,50,51` | 提为配置项 |
| 15 | 提示词收藏是全局单文件 | `qq_comfy_bridge.py:32` | 多用户会互相看到/覆盖。按 `openid` 隔离，或明确声明"单用户模式" |
| 16 | 生成进度靠 1 秒轮询 `/history` | `qq_comfy_bridge.py:184-216` | 改成订阅 ComfyUI `/ws` 的 `progress` / `executed` 事件，省资源，还能回"已完成 40%" |
| 17 | 失败提示直接把 HTTP 响应体抛给用户 | `qq_comfy_bridge.py:177,196` | 对普通用户不可读；区分"用户可读提示"与"日志详情" |
| 18 | 日志仅打印到 stdout，重启即丢 | `__init__.py:56-59` | 可选写入轮转日志文件，方便 issue 反馈 |
| 19 | 无 ComfyUI Manager / Registry 发布元数据 | 仓库根 | 见 §4 M0-4 |

### P2：架构演进（不要现在做）

| # | 议题 | 说明 |
| --- | --- | --- |
| 20 | IM 适配器抽象 | 现在 `QQBot` 类同时承担"IM 连接 + 队列 + ComfyUI 调用 + 指令解析"。真要支持 Telegram/Discord 时再拆，别提前抽象 |
| 21 | 多工作流 / 参数化指令 | 如 `/draw 尺寸=1024x1024 负面=...`，需要先有配置化的节点映射才谈得上 |
| 22 | 群聊 / 频道 | 需要额外 intents、@ 消息处理、群白名单、频次策略，属于独立特性 |
| 23 | 常驻服务化 | 用 systemd / Windows 服务替掉"子进程随 ComfyUI 起落" |

---

## 3. 目标形态

### 3.1 配置结构（建议）

保留 `qq_bot_config.json`（已 gitignore），新增字段全部有默认值，**旧配置可直接读**：

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `appid` / `appsecret` | `""` | 保留；额外支持环境变量 `QQ_APPID` / `QQ_APPSECRET` 覆盖（便于容器/CI） |
| `openids` | `[]` | 保留 |
| `auto_bind_first_user` | `false` | 新增，见 P0-9 |
| `comfyui_url` | `http://127.0.0.1:8188` | 新增 |
| `workflow` | `workflows/default.json` | 改为相对插件目录的路径 |
| `node_map` | 见下 | **新增，核心** |
| `default_width` / `default_height` | `768` / `1280` | 新增 |
| `queue_limit` / `gen_timeout` | `3` / `240` | 新增 |
| `status_port` | `8189` | 新增，占用时自动顺延 |
| `language` | `zh` | 新增，`zh` / `en` |

`node_map` 采用"角色 → 节点 ID + 输入名"的映射，避免假设具体节点：

```json
{
  "prompt": { "node": "6",  "input": "text" },
  "seed":   { "node": "3",  "input": "seed" },
  "size":   { "node": "13", "input": ["width", "height"] },
  "save":   { "node": "9",  "input": "images" }
}
```

配套做两件事：
1. **启动自检**：读工作流后校验 `node_map` 里每个节点与输入是否存在，缺失时输出"你的工作流节点 ID 是 X，请在配置里改 node_map"这一级别的可读报错，而不是 KeyError。
2. **文档给出"如何拿到节点 ID"的 3 步图示**（ComfyUI 里导出 API 格式 → 打开 JSON → 找 class_type）。

### 3.2 目录结构（建议）

```
comfyui-qq-bridge/
├─ __init__.py            # 只做注册与生命周期，保持轻
├─ qq_bridge/             # 核心逻辑拆包
│  ├─ __init__.py
│  ├─ bot.py              # QQ 网关连接 / 消息分发
│  ├─ comfy.py            # ComfyUI 提交 / 进度 / 取图
│  ├─ workflow.py         # 模板加载 + node_map 注入 + 自检
│  ├─ queue.py            # 串行队列与限流
│  └─ config.py           # 配置读写 + 默认值 + 环境变量覆盖
├─ web/qq_bridge.js
├─ workflows/default.json # 最小可用示例（API 格式）
├─ docs/                  # 截图 / GIF / 节点 ID 图解
├─ tools/panel.py         # 可选，原 Tkinter 面板（或用 README 说明删除）
├─ requirements.txt
├─ pyproject.toml         # 含 [tool.comfy] 发布元数据
├─ LICENSE
├─ README.md / README.en.md
├─ CHANGELOG.md
├─ CONTRIBUTING.md
└─ plan.md                # 本文件，发布前可移到 docs/ 或删除
```

拆包这一步**建议放在 M1 末尾、M2 开始时**做，不要一上来大改，先让配置化在单文件里跑通。

### 3.3 开源后的对外承诺（README 必写）

- 支持：Windows / Linux / macOS；ComfyUI 手动安装、秋叶整合包、ComfyUI Manager 安装三种路径。
- 需要：已认证的 QQ 开放平台机器人 + **已开通 C2C 单聊权限**（这是最容易卡住的一步，必须写）。
- 不提供：任何形式的公共托管服务；不收集任何数据；所有请求仅在本机与腾讯官方 API 之间。
- 免责：与腾讯无关联；使用者需自行遵守 QQ 开放平台条款。

---

## 4. 分阶段路线图

### M0 · 止血与仓库整理（先做，1 个 PR 内完成）

- [ ] 移除父目录工作流回溯（P0-2），只认显式配置。
- [ ] 补 `LICENSE`（**建议 MIT**；若要和 ComfyUI 生态强一致可选 GPL-3.0，二选一并在 README 说明）。
- [ ] 补 `requirements.txt`（`aiohttp`；若保留面板再加 `requests`）。
- [ ] 补最小 `README.md`：一句话介绍 + 一张截图 + 三步启动。**先有，再完善。**
- [ ] `.gitignore` 补充 `.venv/`、`.idea/`、`.DS_Store`、`workflows/private/`、`*.png.tmp`。
- [ ] 跑一次密钥扫描（`gitleaks detect` 或手工 `git log -p` 搜索 `appsecret`），确认历史干净（本次已初步确认）。
- [ ] 统一 commit message 为英文祈使句（现有历史已基本符合 `Add ...` 风格，保持）。

**验收**：一个陌生人看到仓库，能在不问你任何问题的情况下判断"这是什么、要不要用"。

### M1 · 配置化（做出"最小可用开源版"）

- [ ] 按 §3.1 重做配置结构与默认值，兼容旧配置读取。
- [ ] 工作流搬到 `workflows/default.json`，仓库内置一份**通用 SD/SDXL 文生图 API 工作流**。
- [ ] 实现 `node_map` 注入 + 启动自检 + 可读报错（P0-3）。
- [ ] `comfyui_url`、分辨率、队列上限、超时、状态端口全部可配（P0-4~6、P1-14）。
- [ ] 白名单默认改为不自动绑定（P0-9），双入口都要能控制。
- [ ] 状态端口被占用时自动顺延，并把实际端口回写给插件（否则 Web 徽标会瞎）。
- [ ] 删除 Tkinter 面板或移动到 `tools/`（P1-11）。
- [ ] README 补齐：安装三种路径、QQ 平台开通步骤、拿到节点 ID 的图解、配置项表、FAQ、隐私与免责。
- [ ] `README.en.md` 英文版。

**验收**：在一台**干净的机器**（或新用户群友）上，仅按 README 操作，20 分钟内能收到第一张图。这是本项目开源成败的唯一硬指标。

### M2 · 普适性增强（有第一批用户之后再做）

- [ ] 进度反馈：改订阅 ComfyUI `/ws`，回"排队中 / 已完成 x%"（P1-16）。
- [ ] 用户可读的错误文案分层（P1-17）。
- [ ] 提示词收藏按用户隔离（P1-15）。
- [ ] 支持负面提示词与常用尺寸预设（不必上完整指令系统）。
- [ ] 日志落盘 + 一键导出（方便收 issue）。
- [ ] 轻量 i18n（中/英），`language` 配置生效（P1-13）。
- [ ] 发布元数据：`pyproject.toml` + `[tool.comfy]`，向 ComfyUI Manager 的 custom-node-list 提 PR（P0-19）。
- [ ] GitHub Actions：`ruff` + `python -m compileall` + 打包检查。

**验收**：被 ComfyUI Manager 搜到并能一键安装；issue 里不再出现"节点 6 不存在"这类本可避免的问题。

### M3 · 发布与社区

- [ ] `CHANGELOG.md` + 语义化版本 + 首个 Git tag / Release（附 GIF 演示）。
- [ ] Issue / PR 模板、`CONTRIBUTING.md`、行为准则。
- [ ] 发布渠道：ComfyUI Manager 列表、Reddit r/comfyui、B 站/即刻/少数派类教程、QQ 机器人开发者群。
- [ ] 明确"支持的平台与 Python 版本"矩阵，避免无止境的兼容性兜底。

### M4 · 可选架构演进（仅在有明确需求时）

- [ ] 按 §3.2 拆包（`qq_bridge/`）。
- [ ] 抽 IM 适配器接口（为 Telegram/Discord 留位，但**没有第二个实现就不要抽**）。
- [ ] 群聊 / 频道支持（独立特性，需评估平台审核与频次配额）。

---

## 5. 文件级改动清单（速查）

| 文件 | M0 | M1 | M2 |
| --- | --- | --- | --- |
| `LICENSE` | 新增 | | |
| `README.md` / `README.en.md` | 最小版 | 完整版 | 补 FAQ |
| `requirements.txt` | 新增 | 校正 | |
| `pyproject.toml` | | | 新增（发布用） |
| `.gitignore` | 补充 | | |
| `qq_bot_config.example.json` | | 扩字段 | |
| `__init__.py` | 端口探测读配置 | 配置化 | 路由无需变 |
| `qq_comfy_bridge.py` | 删父目录回溯 | 配置化 + node_map + 自检 | 进度事件 / 错误分层 / 收藏隔离 |
| `web/qq_bridge.js` | | 跟随新状态字段 | i18n |
| `qq_bot_panel.py` | | 删除或移至 `tools/` | |
| `workflows/default.json` | | 新增 | |
| `docs/` | | 截图 + 节点 ID 图解 | 演示 GIF |
| `CHANGELOG.md` | | | 新增 |

---

## 6. README 大纲（直接照抄结构）

1. 一句话介绍 + 演示 GIF
2. 效果截图（QQ 里收到图 / ComfyUI 右下角徽标）
3. 功能与**能力边界**（只支持单聊、只支持文生图）
4. 前置条件（QQ 开放平台机器人 + C2C 权限 + ComfyUI）
5. 安装（三种路径：Manager / 手动 clone / 整合包）
6. 配置（表格：字段、默认值、说明）
7. 如何准备你自己的工作流（API 格式导出 + 拿到节点 ID 的图解 + `node_map` 怎么填）
8. 常用指令（`/status` `/cancel` `/save` `/get` `/list` `/del`）
9. 常见问题（连不上网关 / 收不到消息 / 图片过大 / 端口占用 / 提示"缺少节点 X"）
10. 隐私与安全（密钥如何存、白名单、仅回环地址、不外传数据）
11. 免责声明（与腾讯无关联、遵守平台条款）
12. License / 致谢

---

## 7. 风险与注意事项

| 风险 | 说明 | 应对 |
| --- | --- | --- |
| 破坏自用环境 | 当前"能用"部分依赖父目录工作流与固定节点 ID，改动后自己先崩 | 先给现有工作流补一份 `node_map` 配置，再改代码；保留旧配置读取逻辑 |
| QQ 平台权限门槛 | 很多人卡在"没有 C2C 权限"就报"收不到消息" | README 单独一节 + FAQ 置顶；提供一键自检（能否拿到 token / 能否连网关） |
| 密钥安全 | 明文 JSON | README 醒目提示；支持环境变量；`.gitignore` 已覆盖；示例文件不含真实值 |
| 端口冲突 | 8188/8189 都可能被占 | 状态端口自动顺延；ComfyUI 地址可配；报错文案指向改配置 |
| 杀软误报 | 起子进程 + `taskkill` | 文档说明；长期可考虑同进程 asyncio 任务替代子进程 |
| 许可证选错 | 与 ComfyUI（GPL-3.0）生态混用 | 插件是独立进程/独立仓库，MIT 可用；若直接 import ComfyUI 内部模块则需评估，建议保守选 GPL-3.0 |
| 范围失控 | 一开源就被要求加群聊、视频、多平台 | §8 明确"暂不支持"，用 Milestone 管理需求 |

---

## 8. 明确不做的事（控制范围）

- 不提供公共在线服务、不做多租户账号体系。
- 不接入数据库，配置仍用 JSON。
- 不在没有第二个 IM 实现时抽"适配器框架"。
- 不为了兼容而保留"父目录找工作流"这类隐式行为。
- 不重写前端；Web 徽标够用就不做成完整页面。

---

## 9. 建议的开工顺序（第一周）

1. 建 `README.md` 骨架 + `LICENSE` + `requirements.txt`（半天，先把门面立起来）。
2. 删父目录回溯，工作流改到 `workflows/default.json`，自己机器先跑通（1 天）。
3. 加 `node_map` 与启动自检，故意填错节点 ID 验证报错是否可读（1 天）。
4. 其余写死项配置化（1 天）。
5. 用一台干净环境按 README 走一遍，把每一步卡点补进文档（1 天）。
6. 打 tag、发 Release，投 ComfyUI Manager 列表。

完成第 5 步之前，**在任何社区推广之前先自己用一周**——第一批用户的负面印象很难挽回。

---

## 10. 功能设计 v2（已与作者确认）

> 本章是讨论后确定的目标形态，覆盖 §3.1 的旧配置草案。实现时以本章为准。

### 10.1 定位与范围

- **单聊、安装者自用**：安装者在 ComfyUI 里填自己的 AppID / AppSecret，然后在手机 QQ 上使用自己 PC 上的模型。
- **插件是 bridge + 框架**，不内置任何具体模型：用户自己准备 API 格式工作流，插件只负责"消息 → 填模板 → 提交 → 回图"。
- 明确不支持：群聊、频道、频道私信、多租户、公共托管服务。
- 开源声明中写清"仅支持单聊、仅限安装者本人使用"，并提示不要把机器人提交到公开推荐位。

### 10.2 交互入口：按钮菜单

微信/QQ 里没有画布，所以"选模式"必须前移到消息层。

**菜单内容（固定四项）**：文生图 / 图生图 / 局部重绘 / 再来一张。

**弹出时机**：

| 时机 | 动作 | 回复预算 |
| --- | --- | --- |
| 用户发来消息，但当前没有已选模式 | 只回菜单，不执行 | 1 次 |
| 用户点击模式按钮 | 指令按钮 `action.type=2` + `enter=true`，点一下自动发出一条命令消息 | 0 次（由用户消息触发新一轮） |
| 任务入队成功 | 「已收到，正在生成。seed xxx」，有排队时附前面任务数 | 1 次 |
| 任务完成 | 结果图（caption 带耗时/seed）+ 菜单 | 2 次 |
| 插件启动 / WebSocket READY | **不推送**：此时没有 `openid`，且属主动消息（4 条/月） | — |

**规则**：

- 模式**一次性有效**，任务完成即失效，下次必须重新选，避免"上次选了重绘、这次误发一张照片"。
- 生成期间先回一条 ack，让用户确认任务已提交（之前只看到"..."会以为没连上）。
- **按钮为主路径**：作者账号已验证可发送按钮交互。
- **统一使用 `keyboard.content`（自定义键盘）而非 `keyboard.id` 模板**：模板 ID 是按机器人申请、无法随插件分发，若写死模板 id，其他安装者必然失败。
- 首发菜单时做一次失败探测：发送失败（无权限 / markdown 被拒）则整条降级为**同内容的文字菜单**，写 `keyboard_ok: false`，本次运行内不再重试。
- 点击回调必须在 **3 秒内** `PUT /interactions/{interaction_id}`，否则用户端转圈；真正的生成在其后异步进行，用 `event_id` 发被动消息。

### 10.3 输入路由

| 用户发来 | 模式 | 档案必需角色 |
| --- | --- | --- |
| 文字 | 文生图 | `prompt`、`seed`、`width/height`、`output` |
| 图片（可附文字） | 图生图 | `image`、`prompt`（可选）、`seed`、`output` |
| 图片 → 提示词组（两轮） | 局部重绘 | `image`、`region_prompt`、`draw_prompt`、`seed`、`output` |

### 10.4 工作流档案（profile）schema

每个档案 = 一份 API 格式工作流 + 一张"角色 → 目标节点"的映射。

```json
{
  "id": "demo-inpaint-sam3",
  "label": "局部重绘（文字分割）",
  "mode": "inpaint",
  "requires": ["image", "region_prompt", "draw_prompt"],
  "targets": {
    "image":         [{ "node": "125", "input": "image" }],
    "region_prompt": [{ "node": "86",  "input": "prompt" }],
    "draw_prompt":   [{ "node": "77",  "input": "text" }],
    "seed":          [{ "node": "79",  "input": "seed" },
                      { "node": "105", "input": "seed" }],
    "strength":      [{ "node": "79",  "input": "denoise" }]
  },
  "output": { "node": "82" },
  "timeout": 300
}
```

**硬规则**（全部由实际工作流倒推而来）：

1. **每个角色都是数组**。同一份工作流里 seed 可能同时喂给基础采样和精修采样（本项目的重绘就是 `79` + `105`），只写一个会导致两次采样种子不同。
2. **提示词可能不是字面量**。图生图档案里 `CLIPTextEncode(50).text` 连的是 `StringFunction(71)` 的输出，正确注入点是 `71.text_a`，并声明 `"join": "prefix"`。直接改写被连线的输入会切断上游链路。
3. **导入时必须检测"目标输入是否为连线数组"**，若是则提示"该输入被上游占用，请改选字面量节点"，不能静默覆盖。
4. **图片类输入先 `POST /upload/image`** 落到 `input/qq_bridge/<openid>/`，再把返回的 `name` 写进 `LoadImage.image`；工作流 JSON 里塞不进二进制。
5. **只有文生图需要注入 `width/height`**；图生图和重绘的 latent 来自原图，注入宽高反而会拉伸。
6. `output` 必填，用于从 `/history` 取图；`timeout` 按档案配，不设全局值。
7. 导入后调 `/object_info` 校验节点 class 与输入名，失败时给出"节点 6 不是 CLIPTextEncode"级别的可读错误。

### 10.5 三份工作流的实测映射

**图生图**（`Z-iMage-turbo基础工作流（图生图）.json`）：

| 角色 | 目标 | 说明 |
| --- | --- | --- |
| `image` | `51.image` (LoadImage) | 用户上传的图 |
| `prompt` | `71.text_a` (StringFunction) | 字面量前缀 |
| `seed` | `44.seed` (KSampler) | 单个采样器 |
| `output` | `46` (SaveImage) | |
| 备注 | `44.denoise = 1` | 这是整图重绘，不是低强度图生图；strength 应作为可选覆盖项 |

**局部重绘**（`Z-iMage-turbo基础工作流（局部重绘）.json`）：

| 角色 | 目标 | 说明 |
| --- | --- | --- |
| `image` | `125.image` (LoadImage) | |
| `region_prompt` | `86.prompt` (SAM3Segment) | 文字描述要分割的目标 |
| `draw_prompt` | `77.text` (CLIPTextEncode) | 重绘成什么 |
| `seed` | `79.seed` + `105.seed` | 基础采样 + 精修采样，必须同值 |
| `strength` | `79.denoise` | 默认 0.6 |
| `output` | `82` (SaveImage) | |
| 待确认 | `80.pixels` 取 `["86",0]` | 若 SAM3 的 0 号输出是抠图结果，底图会缺背景；建议做成可选 `base_image`，能在 `86/0` 与 `125/0` 之间切换 |
| 备注 | `105` 是第二遍精修（denoise 0.2） | 耗时接近两倍，`timeout` 要放宽到 300s |

**文生图**：当前仓库未附带 API JSON，按通用节点自动识别 `CLIPTextEncode → KSampler → EmptyLatentImage → SaveImage`，以导入时的识别结果为准，必须人工确认。

### 10.6 局部重绘的输入语法

SAM3 需要**英文名词性目标**，CLIPTextEncode 需要**完整描述**，一句话拆不出来，所以用两段式。

移动端无法图片和文字同时发出，因此改成**两轮**：

```
用户: [图片]              → Bot: 图片已收到，请发送「遮罩目标；重绘内容」
用户: legs；改成白色过膝袜  → Bot: 开始重绘
```

```
/redraw legs；一只穿西装的橘猫，坐在沙发上
```

- 分隔符为全角「；」，同时兼容半角 `;` 和 `|`。
- 未写分隔符时：整句同时喂给两边，并在回复里附一行用法提示（覆盖"整体风格修改"这类无需精确分割的场景）。
- 遮罩目标必须是英文，帮助文案里明确提示。
- `draw_prompt` 送进工作流前自动追加画质词 `smooth edges, seamless blending, highly detailed, natural lighting, high quality`，用户只写要改成的样子。
- 待处理图片默认保留 15 分钟，超时需重新发图。

### 10.7 回复预算与消息时序

QQ 单聊被动回复：**一条用户消息 60 分钟内最多 4 条**；主动消息 **4 条/月/人**。因此：

```
用户: [文字]（已在文生图模式）
Bot : 已收到，正在生成。seed xxx  (1/4)
Bot : 结果图（带 seed/耗时）       (2/4)
Bot : 菜单                        (3/4)
```

- 不使用 `input_notify`（输入中）作为常规手段；仅在排队超过 30 秒时发一次，且要计入预算。
- 生成超过 60 分钟视为失败并中断，不尝试用主动消息补救。
- 结果 caption 保持纯文本；markdown 能力涉及模板权限，不作为依赖。

### 10.8 会话状态

```
idle → menu_sent → generating → idle
```

- 每个 `openid` 一份状态：`{mode, step, prompt, profile_id, last_result_path, expires_at}`。
- 模式一次性有效；`last_result_path` 供 `/again` 与"重绘这张图"使用，默认缓存 24 小时、每个用户最多 5 张，存 `input/qq_bridge/<openid>/`。
- 同一用户同时只允许一个任务；队列有任务时新消息提示"上一个还在跑"。
- 不做群聊，因此无需处理 5 分钟窗口问题。

### 10.9 结果消息尾部的动作

已确认要放：`再抽一张`（`/again`）、`切换工作流档案`（`/mode`）、`重绘这张图`（`/redraw`）。

- `/again`：复用上次 prompt + profile，重新随机 seed。
- `/redraw`：直接用缓存的上次结果图作为 `image`，省去转发（转发会掉画质）；仍走 `/重绘 A | B` 语法。
- `/mode`：列出所有档案，回复序号切换；切换后不自动执行。

### 10.10 待验证风险

| 风险 | 说明 | 处理 |
| --- | --- | --- |
| 开源用户的按钮权限 | 作者账号已验证可用；但模板 ID 不能随插件分发，自定义键盘需各账号内邀开通，其他安装者可能没有 | 统一用 `keyboard.content`；首次发送探测失败即降级文字菜单并记录 `keyboard_ok=false` |
| 按钮依赖 markdown | 文档说明仅 markdown 消息支持按钮，markdown 自身也可能有模板权限限制 | 发送链路整体降级，不拆开处理 |
| 点击响应超时 | `INTERACTION_CREATE` 需 3 秒内 `PUT /interactions/{id}` | 先应答，再入队生成 |
| 工作流被改动 | 用户重新导出 API JSON 后节点 id 变化 | 导入时重新自动识别并提示确认，不做静默迁移 |
| 双采样器种子 | 重绘工作流有两处 seed | schema 用数组强制覆盖 |
| 底图来源 | `VAEEncodeForInpaint.pixels` 当前取 SAM3 输出 | 做成 `base_image` 可选角色，默认原图 |
| 生成超时 | 精修节点使耗时接近两倍 | `timeout` 按档案配置，重绘默认 300s |
| 图片体积 | 富媒体有大小限制 | 保留现有压缩逻辑，阈值可配 |
