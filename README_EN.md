# comfyui-qq-bridge

[中文](README.md) | **English**

A way to use a locally deployed image generation model from a mobile device on restricted networks such as a campus network.

Drive your local ComfyUI from a QQ bot private chat.

The plugin ships **no models and no workflows**.

It does three things: receive QQ messages, inject their content into a designated API workflow, and send the result back to the QQ bot on your phone.

Which model and sampler to use is decided by the workflow profile you import.

<img src="docs/preview-chat.jpg" alt="Menu and buttons in mobile QQ" width="380">

## Scope

- **Private chat (C2C) only.** No group chats, no channels, and no plans for multi-user sharing.
- **For the installer only.** With an empty allowlist, the first account that messages the bot is bound as the owner; messages from anyone else are ignored.
- Out of concern for abuse, this project offers no support for multi-user, public-bot, or content-farm style usage.

## Features

| Mode | Input | Behavior |
| --- | --- | --- |
| Text to image | Text | Writes the text into the positive prompt node |
| Image to image | Image (optionally with a caption) | Uploads the image to ComfyUI, writes the caption into the workflow prompt node |
| Masked inpaint | Image + `mask target; redraw content` | Sent in two steps: image first, then the prompt pair. The mask target goes to a text-guided segmentation node such as SAM3 (English only); the redraw content goes into the positive prompt and is automatically suffixed with `smooth edges, seamless blending, highly detailed, natural lighting, high quality` |
| One more | A single button | Reuses the previous input with a new seed |

Selecting a mode applies to the next message only; after the image is delivered the bot returns to the menu, so you cannot mis-tap on a phone.

Mobile QQ cannot put an image and text into one message, which is why the img2img caption is optional and why inpaint is split into "send image, then send prompts".

## Installation

1. Put this directory into `ComfyUI/custom_nodes/`.
2. Restart ComfyUI; a "QQ 机器人" badge appears in the bottom-right corner.
3. Create a bot on the [QQ Open Platform](https://q.qq.com/), get its AppID / AppSecret, and fill them into the badge form.
4. Send a message to the bot from your own QQ account; the plugin binds that account into the allowlist automatically.

The only dependencies are `aiohttp` and `Pillow`, both bundled with ComfyUI, so normally nothing extra needs installing.

The expanded badge looks like this (AppID redacted):

<img src="docs/badge.png" alt="QQ Bridge badge in the bottom-right corner of ComfyUI" width="320">

## Example environment (author's test setup)

The three sample profiles in this repository were verified on the setup below. They are reference only, not a requirement of the plugin.

Test machine: Windows laptop (i7-13700H + RTX 4060 Laptop 8GB VRAM + 16GB DDR5), running the Aki bundle (ComfyUI-aki-v3.2) with `CUDA GPU 0` and "standard optimization (4GB+)" in the launcher.

Model files:

| File | Location | Purpose |
| --- | --- | --- |
| `z_image_turbo_fp32-Q4_K_M.gguf` | `models/unet/` | Main model, GGUF quantization of Z-Image Turbo (~4.6GB) |
| `qwen_3_4b.safetensors` | `models/clip/` | Text encoder; the samples use `CLIPLoader` with `type=lumina2` |
| `ae.safetensors` | `models/vae/` | VAE |
| `Z-Image-Turbo-Fun-Controlnet-Union.safetensors` | `models/loras/` | LoRA used by the samples (strength 0.65), removable |
| `sam3.safetensors` | `models/sam3/` | Text-guided segmentation model for inpaint |

Custom nodes:

| Package | Node used | When it is needed |
| --- | --- | --- |
| [ComfyUI-GGUF](https://github.com/city96/ComfyUI-GGUF) | `UnetLoaderGGUF` | All three samples |
| [ComfyUI-RMBG](https://github.com/1038lab/ComfyUI-RMBG) | `SAM3Segment` | Inpaint sample only |
| [ComfyUI_UltimateSDUpscale](https://github.com/ssitu/ComfyUI_UltimateSDUpscale) | `UltimateSDUpscaleNoUpscale` | Inpaint sample only |
| [ComfyUI_essentials](https://github.com/cubiq/ComfyUI_essentials) | `MaskBlur+` | Inpaint sample only |

To change the model or LoRA, edit literals such as `unet_name` / `lora_name` in the sample profile; no plugin code changes are needed.

## Workflow profiles

A "profile" is an **API-format** ComfyUI workflow plus a "role to node input" mapping, stored as `workflows/*.json`.

### Importing

The badge has three import buttons, one per mode. Click one and pick a JSON exported from ComfyUI with "Save as API format" (not the UI-format `.json`).

Import runs a node check first and refuses to save if it fails:

| Mode | Required nodes |
| --- | --- |
| Text to image | A CLIPTextEncode that accepts text, plus SaveImage |
| Image to image | LoadImage and SaveImage |
| Masked inpaint | LoadImage, a SAM3-style segmentation node with a text input, CLIPTextEncode for the redraw prompt, and SaveImage |

All three modes also require a KSampler seed. If the file looks like a different mode, the import tells you to use the matching button.

- **Manually**: write the API workflow as `workflows/your-profile.json` following the format below.
- **Switching**: click a profile name in the badge, or send `/profiles` to list and `/use <id>` to activate on a phone.

A mode may have several profiles, but one must be marked active (`active_profiles`). If only one candidate exists it is selected automatically.

The `workflows/zimage-*.json` files are the author's samples (Z-Image Turbo family); their model files and custom nodes are listed in the previous section. Missing nodes are marked red in the badge; delete or replace them as needed.

If you prefer not to use the badge, copy `qq_bot_config.example.json` to `qq_bot_config.json`, fill it in by hand, and restart ComfyUI.

### Profile format

```json
{
  "id": "my-inpaint",
  "label": "Inpaint",
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

Fields:

- **Roles**: `prompt` `negative` `region_prompt` `draw_prompt` `image` `mask` `seed` `width` `height` `strength` `batch`.
- **Values in `targets` are always arrays**: one role may list several nodes, for example when the base and refiner samplers share a seed.
- **`join`**: `replace` (default) / `prefix` / `append`, together with `separator`. Used to place the user's text before or after existing text in the workflow.
- **`link`**: `{ "node": "77", "input": "text", "link": ["50", 0] }` rewires that input to a connection instead of writing a literal.
- **`output.node`**: which image to fetch, usually a `SaveImage`.
- **`defaults`**: per-profile defaults such as size, taking precedence over the global config.

Modes and required roles:

| mode | Required roles |
| --- | --- |
| `txt2img` | `prompt`, `seed` |
| `img2img` | `image`, `seed` |
| `inpaint` | `image`, `region_prompt`, `draw_prompt`, `seed` |

Common pitfall: if a target input is already connected (its value is an array such as `["node", 0]`), writing into it would cut the upstream link, so the import rejects it. In img2img the prompt is often an upstream literal node such as `StringFunction.text_a`, combined with `join: prefix`.

## Commands

| Command | Purpose |
| --- | --- |
| `/menu` | Show the menu again |
| `/mode txt2img\|img2img\|inpaint` | Select a mode, same as tapping a button |
| `/again` | Generate one more with a new seed |
| `/redraw legs；change to white over-knee socks` | Redraw the previous image (mask target in English) |
| `/profiles` | List workflow profiles |
| `/use <profile id>` | Activate a profile |
| `/status` | ComfyUI connection, queue, current job |
| `/cancel` | Interrupt the current generation |
| `/save <name> <prompt>`, `/list`, `/get <name>`, `/del <name>` | Prompt bookmarks |

## Platform limits

All of these are QQ Open Platform rules, unrelated to local compute:

- **Passive replies**: at most 4 replies per message anchor, within a window of about 5 minutes. A successful submission first replies "received, generating", then the image plus the menu, 3 messages in total.
- **Proactive messages**: 4 per month. The openid is unavailable at startup, so the bot never pushes proactively and only responds after you message it.
- **Message buttons**: custom keyboards via `keyboard.content`. If the first send fails it falls back to a numbered text menu and does not retry during that run.
- **Rich media**: images are sent as base64, kept under 3.5MB each and compressed automatically beyond that.

## FAQ

**The badge says "not configured"**: AppID / AppSecret are empty, or the child process was not restarted after filling them in.

**Tapping a button on the phone does nothing**: some accounts or app versions do not support custom keyboards; the plugin falls back to a text menu, so just reply 1-4.

**Generation fails with a missing node**: the imported profile uses a custom node you have not installed; install it or adjust the mapping.

**No image arrives**: most likely the passive reply window expired, for example when generation took more than 5 minutes. Check `/status`, lower `gen_timeout`, or use a faster workflow.

**Port conflict**: `status_port` defaults to 8189; change it in `qq_bot_config.json` if it is taken.

## Layout

```
comfyui-qq-bridge/
├─ __init__.py           Host plugin: process management + HTTP routes (config, profiles, status)
├─ qq_comfy_bridge.py    Entry point of the bridge process
├─ qq_bridge/            Bridge implementation (config / profiles / workflow / comfy / qq_api / session / menu / bot / app)
├─ web/qq_bridge.js      ComfyUI frontend badge and settings
├─ docs/                 Screenshots used by the README
├─ workflows/            Workflow profiles (zimage-*.json are samples, the rest are yours)
├─ qq_bot_config.json    Local config with secrets (gitignored)
└─ prompts.json          Prompt bookmarks (gitignored)
```

## License

MIT, see [LICENSE](LICENSE). Do not use this project for content that is illegal or violates platform rules.
