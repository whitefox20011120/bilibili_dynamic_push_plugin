# Bilibili 动态推送插件 — 使用说明

---

## 0. 准备条件

- 已部署并可正常发送消息的 QQ 机器人（Napcat 适配器已就绪）。
- Python 环境可用（建议 3.10+）。
- （可选）已安装 `pillow` 用于压缩/转 JPG、`beautifulsoup4` 用于 HTML 兜底解析。

```bash
# 可选依赖（推荐安装）
pip install pillow beautifulsoup4
```

---

## 1. 安装插件

1) 将插件文件夹放到你的机器人插件目录（例如：`modules/MaiBot/plugins/bilibili_dynamic_push_plugin/`）。  
2) 确保其中包含 `plugin.py`。  
3) 在同目录创建并编辑 `config.toml`（下一节有模板与说明）。

---

## 2. 配置插件

在 `bilibili_push_plugin` 目录下新建 `config.toml`，按需修改。**最少需要**：
- 配好要监控的 UID 和要推送的群号；
-（强烈建议）填入你的 B 站 Cookie，以减少风控与昵称解析失败。

### 2.1 最小可用示例

```toml
[monitor]
enable = true
interval_minutes = 3
ignore_history_minutes_on_boot = 180

[bilibili]
routes = [
  { uids = ["123456"], groups = ["10001"] }
]
cookie = "SESSDATA=xxx; bili_jct=xxx; DedeUserID=xxx; ..."
```

> **获取 Cookie 简要方法**：PC 浏览器登录 B 站，打开任意页面，按 F12 打开开发者工具 → Application/存储 → Cookies → 复制整条（含 `SESSDATA`、`bili_jct`、`DedeUserID` 等），粘贴到 `cookie` 字段（**保持单行**）。

### 2.2 完整配置项（常用）

```toml
[monitor]
enable = true
interval_minutes = 3
jitter_seconds = 15
silent = false
ignore_history_minutes_on_boot = 180

[image]
send_images = true
force_base64 = true
base64_chunk_limit = 5500000
downscale_width = 720
jpeg_quality = 85
per_image_delay_ms = 1600

[api]
base_url = "https://api.bilibili.com"
timeout = 10
prefer_old = true

[bilibili]
routes = [
  { uids = ["123456", "654321"], groups = ["10001", "10002"] },
  { uids = ["777777"],            groups = ["10001"] }
]
# 兼容旧写法（二选一；与 routes 同时写时会并入路由）
# uids = ["123456"]
# groups = ["10001"]

cookie = "SESSDATA=xxx; bili_jct=xxx; DedeUserID=xxx; ..."

[fallback]
enable_html = true  # 需要安装 beautifulsoup4

[debug]
dump_json = false
dump_uid = []
output_dir = ""
```

**字段说明（精简版）**

- `monitor.enable`：总开关。  
- `monitor.interval_minutes`：轮询间隔；过小易触发风控。  
- `monitor.ignore_history_minutes_on_boot`：启动时忽略多少分钟以前的动态，防止冷启动推送旧内容。  
- `bilibili.routes`：多路由写法；一个条目可将若干 UID 的动态推送到若干群。  
- `bilibili.cookie`：登录 Cookie（单行）。  
- `image.*`：图片发送策略（是否发图、是否 base64、压缩/限宽、发送间隔等）。  
- `api.prefer_old`：优先旧接口，失败再尝试新接口。  
- `fallback.enable_html`：API 都失败时尝试解析空间 HTML（需 `beautifulsoup4`）。  
- `debug.*`：将解析后的动态落盘以便排错。

---

## 3. 启动与运行

1) 确保 Napcat/机器人框架已启动、能向目标群发消息。  
2) 启动你的机器人主程序。  
3) 插件会按 `interval_minutes` 定时轮询，自动向配置的群推送新动态。

---

## 4. 常用操作场景

- **只推文字不发图**：`[image] send_images = false`。  
- **降低风控/大图失败**：  
  - 开启 `force_base64 = true`；  
  - 下调 `downscale_width`（如 720）；  
  - 增大 `per_image_delay_ms`（如 2000–2500）。  
- **避免冷启动推旧动态**：把 `ignore_history_minutes_on_boot` 调大（如 240–360）。  
- **一个 UID 推多个群**：在 `routes` 里写多个 `groups`。  
- **多个 UID 推同一群**：在同一个 `routes` 条目里把 `uids` 写在一起。  
- **临时停止推送**：`monitor.enable = false`，保存后重启机器人或热重载插件（视框架支持）。

---

## 5. 常见问题与排错

**Q1. 启动后立刻推送了很多旧动态？**  
A：调大 `monitor.ignore_history_minutes_on_boot`（如 240–360），重启后生效。

**Q2. 提示风控 / 返回码 `-352` / 解析不到昵称？**  
A：填写并更新 `bilibili.cookie`；适当加大 `interval_minutes`；保持同一账号的 Cookie。

**Q3. Napcat 报错 “rich media transfer failed” 或发图失败？**  
A：开启 `image.force_base64`、降低 `downscale_width`、提高 `per_image_delay_ms`。确保群允许发图、网络稳定。

**Q4. 只想把 UID=A 推到群 X，UID=B 推到群 Y？**  
A：使用 `routes` 写两条：  
```toml
[bilibili]
routes = [
  { uids = ["A"], groups = ["X"] },
  { uids = ["B"], groups = ["Y"] }
]
```

**Q5. 我用的是旧配置（`uids`/`groups`）还能用吗？**  
A：可以。与 `routes` 同时存在时会一并生效，但**推荐迁移到 `routes`** 便于精细路由。

**Q6. 仍然无推送或间歇性断更？**  
A：检查：  
- `monitor.enable` 是否为 `true`；  
- Cookie 是否过期；  
- 间隔是否太短导致接口限制；  
- 日志中是否有超时/解析失败；  
-（可选）打开 `[debug] dump_json = true` 查看落盘内容定位问题。

---

## 6. 更新/升级

- 替换 `plugin.py` 后，保持 `config.toml` 不变即可。  
- 若新增配置项，参考本文档在你的 `config.toml` 中补齐。  
- 更新后建议先调大 `ignore_history_minutes_on_boot` 做一次“冷静期”。

---

## 7. 日志与调试

- 运行日志中会打印轮询、解析与推送的关键信息。  
- 开启 `[debug] dump_json = true` 后，会在 `debug/`（或 `output_dir`）写入解析结果，便于核对“无文字内容”“时间不对”等问题。

---

## 8. 兼容建议

- 尽量保持 **`interval_minutes ≥ 3`**；  
- 尽量提供 **完整且最新的 Cookie**；  
- 图片较多/较大时，优先 **Base64** 发送并 **限宽**；  
- 群较多时分批添加，观察 24 小时后再扩大覆盖面。

---

## 9. 反馈

使用中遇到问题：  
- 提供你的 `config.toml`（打码敏感信息）、关键日志片段，以及是否开启了 `debug.dump_json`。  
- 说明出现问题的 UID、群号、预期行为与实际行为，便于快速定位。
