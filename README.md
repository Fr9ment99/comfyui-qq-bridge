# comfyui-qq-bridge

用 QQ 单聊遥控本地 ComfyUI。手机上发一句话、发一张图，PC 上的工作流出图后自动回传。

插件本身**不带模型、不带工作流**，只做三件事：接收 QQ 消息、把内容注入你指定的 API 工作流、把结果发回去。要跑什么模型、什么采样器，由你导入的工作流档案决定。

## 适用范围

- **只支持单聊（C2C）**，不支持群聊、频道，也没有多人共享的打算。
- **只给安装者自己用**。空白名单时会把第一个说话的账号绑定为安装者，之后其他人发消息一律忽略。
- 出于对滥用风险的顾虑，本项目不提供任何多用户、公开机器人、内容农场式的用法支持。

## 功能

| 模式 | 输入 | 说明 |
| --- | --- | --- |
| 文生图 | 文字 | 把文字写进正向提示词节点 |
| 图生图 | 图片（可附一句描述） | 图片上传到 ComfyUI，描述写进工作流的提示词节点 |
| 局部重绘 | 图片 + `遮罩目标；重绘内容` | 分两步发：先发图片，再发提示词组。遮罩目标交给 SAM3 之类的文字分割节点（必须用英文），重绘内容写进正向提示词 |
| 再来一张 | 一个按钮 | 复用上一次的输入，换一个种子 |

进入某个模式后只对下一条消息生效，出图后自动回到菜单，避免在手机上误触。

## 安装

1. 把本目录放进 `ComfyUI/custom_nodes/`。
2. 重启 ComfyUI，右下角会出现「QQ 机器人」徽标。
3. 在 [QQ 开放平台](https://q.qq.com/) 创建一个机器人，拿到 AppID / AppSecret，填进徽标里的表单。
4. 用自己的 QQ 给机器人发一条消息，插件会自动把该账号绑定进白名单。

依赖只有 `aiohttp` 和 `Pillow`，ComfyUI 自带，一般不用额外安装。

## 工作流档案

「档案」是一份 **API 格式**的 ComfyUI 工作流 + 一张「角色 → 节点输入」的映射表，保存在 `workflows/*.json`。

### 导入方式

徽标里有三个导入按钮，分别对应文生图 / 图生图 / 局部重绘。点按钮后选择 ComfyUI 用「另存为 API 格式」导出的 JSON（不是 UI 格式的 `.json`）。

导入时会先做一次节点体检，不通过就不保存：

| 模式 | 必须找到的节点 |
| --- | --- |
| 文生图 | 可写入文字的 CLIPTextEncode、SaveImage |
| 图生图 | LoadImage、SaveImage |
| 局部重绘 | LoadImage、带文字输入的 SAM3 分割节点、CLIPTextEncode（重绘描述）、SaveImage |

三种模式都还要求工作流里有 KSampler 的 seed；文件看起来属于别的模式时会提示你换按钮。

- **手工放入**：把 API 工作流按下面的格式写成 `workflows/你的档案.json`。
- **切换**：徽标里点档案名即可启用；手机上也可以发 `/profiles` 看列表、`/use 档案id` 启用。

同一个模式可以有多份档案，但必须指定当前使用哪一份（`active_profiles`）。只有一个候选时会自动选中。

仓库里的 `workflows/zimage-*.json` 是作者的示例（Z-Image Turbo + SAM3），缺少对应自定义节点时会在 Badge 里标红，按需删改或换成自己的。

### 档案格式

```json
{
  "id": "my-inpaint",
  "label": "局部重绘",
  "mode": "inpaint",
  "targets": {
    "image":          [{ "node": "125", "input": "image" }],
    "region_prompt":  [{ "node": "86",  "input": "prompt" }],
    "draw_prompt":    [{ "node": "77",  "input": "text" }],
    "seed":           [{ "node": "79",  "input": "seed" },
                       { "node": "105", "input": "seed" }]
  },
  "output": { "node": "82" },
  "timeout": 300,
  "defaults": { "width": 768, "height": 1280 },
  "workflow": { "125": { "class_type": "LoadImage", "inputs": { "image": "" } } }
}
```

字段说明：

- **角色**：`prompt` `negative` `region_prompt` `draw_prompt` `image` `mask` `seed` `width` `height` `strength` `batch`。
- **targets 的值一律是数组**：一个角色可以写多个节点，比如基础采样器和精修采样器都要同一个种子。
- **`join`**：`replace`（默认）/ `prefix` / `append`，配 `separator`。用于把用户输入拼到工作流原有文字的前面或后面。
- **`link`**：`{ "node": "77", "input": "text", "link": ["50", 0] }` 表示把该输入直接改成一条连线，而不是写字面量。
- **`output.node`**：取哪张图，一般指向 `SaveImage`。
- **`defaults`**：该档案自己的尺寸等默认值，优先级高于全局配置。

模式与必需角色：

| mode | 必需角色 |
| --- | --- |
| `txt2img` | `prompt`、`seed` |
| `img2img` | `image`、`seed` |
| `inpaint` | `image`、`region_prompt`、`draw_prompt`、`seed` |

常见坑：目标输入如果已经被连线占用（值是 `["节点", 0]` 这样的数组），写入会切断上游链路，导入时会被拦下。图生图里提示词常写成 `StringFunction.text_a` 这种上游字面量节点，再配 `join: prefix`。

## 指令

| 指令 | 作用 |
| --- | --- |
| `/menu` | 重新弹出菜单 |
| `/mode txt2img\|img2img\|inpaint` | 选择模式，等同于点按钮 |
| `/again` | 再来一张 |
| `/redraw legs；改成白色过膝袜` | 用上一次的图重绘（遮罩目标用英文） |
| `/profiles` | 列出工作流档案 |
| `/use 档案id` | 启用档案 |
| `/status` | ComfyUI 连接、队列、当前任务 |
| `/cancel` | 中断当前生成 |
| `/save 名字 提示词`、`/list`、`/get 名字`、`/del 名字` | 提示词收藏夹 |

## 平台限制

以下都是 QQ 开放平台的规则，跟本地算力无关：

- **被动回复**：同一个消息锚点最多回 4 条，窗口约 5 分钟。任务提交成功会先回一条「已收到，正在生成」，出图后回结果图 + 菜单，共 3 条。
- **主动消息**：每月 4 条。插件启动时拿不到 openid，所以不会主动推送，只在你发消息后响应。
- **消息按钮**：使用 `keyboard.content` 自定义键盘，首次发送失败会自动降级成文字菜单（回复序号），并记住这个结果。
- **富媒体**：图片以 base64 直传，单张控制在 3.5MB 以内，超了会自动压缩。

## 常见问题

**徽标显示「未配置」**：没填 AppID / AppSecret，或者填完没有重启子进程。

**手机点了按钮没反应**：部分账号或版本不支持自定义键盘，插件会退回文字菜单，直接回复 1-4 即可。

**生成失败，提示节点不存在**：导入的档案用到了你本机没装的自定义节点，按提示补装或改映射。

**出图后没收到图**：多半是超过了被动回复窗口（比如生成耗时超过 5 分钟）。可以把 `/status`、`gen_timeout` 调小，或换更快的工作流。

**端口冲突**：`status_port` 默认 8189，被占用时可以在 `qq_bot_config.json` 里改。

## 目录结构

```
comfyui-qq-bridge/
├─ __init__.py           宿主插件：进程管理 + HTTP 路由（配置、档案、状态）
├─ qq_comfy_bridge.py    桥接进程入口
├─ qq_bridge/            桥接实现（config / profiles / workflow / comfy / qq_api / session / menu / bot / app）
├─ web/qq_bridge.js      ComfyUI 前端徽标与设置项
├─ workflows/            工作流档案
├─ qq_bot_config.json    本地配置（含密钥，已被 .gitignore 忽略）
└─ prompts.json          提示词收藏夹（已被忽略）
```

`qq_bot_panel.py` 和 `启动QQ机器人面板.bat` 是早期独立运行的 Tkinter 面板，功能已被 ComfyUI 内的徽标取代，保留仅为兼容旧用法。

## License

MIT，见 [LICENSE](LICENSE)。请勿将本项目用于违法或违反平台规则的内容生成。
