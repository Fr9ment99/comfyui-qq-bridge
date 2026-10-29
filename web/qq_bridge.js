import { app } from "../../scripts/app.js";

const API = "/qq_bridge";
const CATEGORY = ["QQ 机器人"];

const badge = {
  el: null,
  status: null,
  expanded: false,
};

async function call(path, body) {
  const options = body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
  const response = await fetch(API + path, options);
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

const pendingPatch = {};
let saveTimer = null;

function queueSave(patch) {
  Object.assign(pendingPatch, patch);
  clearTimeout(saveTimer);
  saveTimer = setTimeout(async () => {
    const body = { ...pendingPatch };
    for (const key of Object.keys(pendingPatch)) delete pendingPatch[key];
    try {
      await call("/config", body);
      await refresh();
    } catch (error) {
      console.error("[QQBot] 保存配置失败", error);
    }
  }, 700);
}

function addSetting(id, name, type, field, defaultValue, tooltip) {
  const settings = app.ui?.settings;
  if (!settings?.addSetting) return false;
  settings.addSetting({
    id,
    name,
    type,
    category: CATEGORY,
    defaultValue,
    tooltip,
    onChange: (value) => {
      if (type === "boolean" || String(value ?? "").trim()) queueSave({ [field]: value });
    },
  });
  return true;
}

const BADGE_CSS = `
#qq-bridge-badge{position:fixed;right:14px;bottom:14px;z-index:10000;
  font:12px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif;color:#dcdcdc;
  background:rgba(22,22,26,.94);border:1px solid rgba(255,255,255,.13);
  border-radius:10px;padding:8px 11px;box-shadow:0 6px 22px rgba(0,0,0,.38);
  user-select:none;min-width:190px}
#qq-bridge-badge .qqb-head{display:flex;align-items:center;gap:7px;cursor:pointer}
#qq-bridge-badge .qqb-dot{width:8px;height:8px;border-radius:50%;background:#666;flex:none}
#qq-bridge-badge .qqb-title{font-weight:600}
#qq-bridge-badge .qqb-title::after{content:" ▾";color:#777;font-weight:400}
#qq-bridge-badge.expanded .qqb-title::after{content:" ▴"}
#qq-bridge-badge .qqb-state{color:#9a9a9a;margin-left:auto}
#qq-bridge-badge button{font:inherit;color:#cfcfcf;background:rgba(255,255,255,.08);
  border:1px solid rgba(255,255,255,.13);border-radius:6px;padding:2px 8px;cursor:pointer}
#qq-bridge-badge button:hover{background:rgba(255,255,255,.16)}
#qq-bridge-badge .qqb-detail{display:none;margin-top:7px;padding-top:7px;
  border-top:1px solid rgba(255,255,255,.1);color:#9a9a9a;white-space:pre-wrap;
  max-width:240px;word-break:break-all}
#qq-bridge-badge.expanded .qqb-detail{display:block}
`;

function buildBadge() {
  if (document.getElementById("qq-bridge-badge")) return;

  const style = document.createElement("style");
  style.textContent = BADGE_CSS;
  document.head.appendChild(style);

  const el = document.createElement("div");
  el.id = "qq-bridge-badge";
  el.innerHTML = `
    <div class="qqb-head">
      <span class="qqb-dot"></span>
      <span class="qqb-title">QQ 机器人</span>
      <span class="qqb-state">检测中</span>
    </div>
    <div class="qqb-detail">
      <div class="qqb-info"></div>
      <div class="qqb-fallback"></div>
    </div>
  `;
  document.body.appendChild(el);
  badge.el = el;

  el.querySelector(".qqb-head").addEventListener("click", () => {
    badge.expanded = !badge.expanded;
    el.classList.toggle("expanded", badge.expanded);
  });

  el.addEventListener("click", async (event) => {
    const act = event.target.dataset?.act;
    if (!act) return;
    event.stopPropagation();
    try {
      if (act === "restart") {
        await call("/restart", {});
      } else if (act === "toggle") {
        const alive = badge.status?.health || badge.status?.managed;
        await call(alive ? "/stop" : "/start", {});
      } else if (act === "clear") {
        if (!window.confirm("清空 OpenID 白名单？下一次对话会重新绑定第一个用户。")) return;
        await call("/openids/clear", {});
      } else if (act === "save-config") {
        const host = badge.el.querySelector(".qqb-fallback");
        const patch = {};
        const appid = host.querySelector('[data-field="appid"]').value.trim();
        const appsecret = host.querySelector('[data-field="appsecret"]').value.trim();
        if (appid) patch.appid = appid;
        if (appsecret) patch.appsecret = appsecret;
        if (!Object.keys(patch).length) return;
        await call("/config", patch);
      }
      setTimeout(refresh, 600);
    } catch (error) {
      console.error("[QQBot] 操作失败", error);
    }
  });
}

function buildFallbackForm(status) {
  const host = badge.el?.querySelector(".qqb-fallback");
  if (!host || host.dataset.ready) return;
  host.dataset.ready = "1";

  const style = "width:100%;box-sizing:border-box;margin-top:5px;padding:4px 6px;background:rgba(255,255,255,.06);"
    + "color:#ddd;border:1px solid rgba(255,255,255,.15);border-radius:5px;font:inherit";

  const appid = document.createElement("input");
  appid.dataset.field = "appid";
  appid.placeholder = "AppID";
  appid.value = status?.appid || "";
  appid.style.cssText = style;

  const secret = document.createElement("input");
  secret.dataset.field = "appsecret";
  secret.type = "password";
  secret.placeholder = "AppSecret（留空则不修改）";
  secret.style.cssText = style;

  const save = document.createElement("button");
  save.dataset.act = "save-config";
  save.textContent = "保存并重启";
  save.style.marginTop = "6px";

  host.append(appid, secret, save);

  if (!status?.configured) {
    badge.expanded = true;
    badge.el.classList.add("expanded");
  }
}

function renderStatus(status) {
  badge.status = status;
  if (!badge.el) return;

  const health = status.health;
  const dot = badge.el.querySelector(".qqb-dot");
  const stateText = badge.el.querySelector(".qqb-state");
  const detail = badge.el.querySelector(".qqb-info");

  let color = "#666";
  let text = "未运行";
  if (health?.connected) {
    color = "#3ecf8e";
    text = "已连接";
  } else if (health || status.managed) {
    color = "#e0b341";
    text = "连接中";
  } else if (!status.configured) {
    text = "未配置";
  }
  dot.style.background = color;
  stateText.textContent = text;

  const lines = [];
  if (status.appid) lines.push(`AppID ${status.appid}`);
  if (health) {
    lines.push(`队列 ${health.queue}/${health.queue_limit}`);
    if (health.generating) lines.push(`正在生成：${health.prompt || "…"}`);
    if (health.last_error) lines.push(`最近错误：${health.last_error}`);
  } else if (!status.configured) {
    lines.push("请在上方「设置 → QQ 机器人」里填写 AppID 和 AppSecret。");
  }
  if (status.openids?.length) lines.push(`白名单 ${status.openids.length} 个 OpenID`);

  const alive = health || status.managed;
  detail.innerHTML = "";
  for (const line of lines) {
    const row = document.createElement("div");
    row.textContent = line;
    detail.appendChild(row);
  }
  if (status.openids?.length) {
    const clear = document.createElement("button");
    clear.textContent = "清空白名单";
    clear.dataset.act = "clear";
    clear.style.marginTop = "6px";
    detail.appendChild(clear);
  }

  const actions = document.createElement("div");
  actions.style.cssText = "display:flex;gap:6px;justify-content:flex-end;margin-top:8px";
  const restart = document.createElement("button");
  restart.textContent = "重启";
  restart.dataset.act = "restart";
  const toggle = document.createElement("button");
  toggle.textContent = alive ? "停止" : "启动";
  toggle.dataset.act = "toggle";
  actions.append(restart, toggle);
  detail.appendChild(actions);
}

async function refresh() {
  try {
    renderStatus(await call("/status"));
  } catch (error) {
    if (badge.el) {
      badge.el.querySelector(".qqb-dot").style.background = "#666";
      badge.el.querySelector(".qqb-state").textContent = "插件未响应";
    }
  }
}

app.registerExtension({
  name: "QQBridge.Control",

  async setup() {
    const status = await call("/status").catch(() => null);

    addSetting("QQBridge.AppID", "AppID", "text", "appid", status?.appid || "",
      "QQ 开放平台机器人 AppID");
    addSetting("QQBridge.AppSecret", "AppSecret", "text", "appsecret", "",
      "QQ 开放平台机器人 AppSecret");
    addSetting("QQBridge.AutoStart", "随 ComfyUI 自动启动（密钥在右下角徽标里填）", "boolean", "autostart",
      status?.autostart ?? true, "点击右下角「QQ 机器人」徽标展开后填写 AppID 和 AppSecret");

    buildBadge();
    buildFallbackForm(status);
    if (status) renderStatus(status);
    setInterval(refresh, 3000);
  },
});
