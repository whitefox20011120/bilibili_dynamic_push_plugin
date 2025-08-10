# BilibiliDynamicPush —— B 站动态推送插件（Napcat / MaiBot）

> 把指定 UP 主的新动态，自动推送到你的 QQ 群。  
> 支持转发动态解析、富文本还原、配图发送（Base64→URL→file 兜底）、`-352` 风控重试、旧/新接口与 HTML 兜底、静默日志、调试落盘等。

---

## ✨ 功能一览

- **多 UID 监听 → 多群推送**：一份配置同时监听多个 UP 主，并路由到多个群。
- **接口兼容**：优先 `polymer/web-dynamic`（含 `desktop` 变体），自动处理 `-352`，必要时回退到旧接口 `space_history`，仍失败再走 **HTML 兜底**。
- **去顶置/去重**：自动忽略“置顶”动态；持久化 `last_seen`，只推送真正的新动态；冷启动**只记录不推送**。
- **文案增强**：解析 `module_dynamic.desc` 与 `major.opus/article/archive/...` 的富文本节点（AT、话题、换行、表情、URL、换行 `BR` 等），尽量还原发布文字；遇到**纯图片/无文字**会给出明确提示。
- **转发动态识别**：识别 `dyn_forward`/`orig` 结构，标题自动带“🔁 转发了 xxx 的动态”，并提取原文与原图。
- **配图发送（Napcat 友好）**：  
  1) **Base64**（纯 Base64 字符串，不加 `base64://` 前缀）  
  2) **URL 直链**  
  3) **file:/// 落盘兜底**  
  同时可自动压缩为 JPG（限宽/质量），缓解 “rich media transfer failed”。
- **自定义发送速率**：逐张图片之间可设置间隔，降低风控概率。
- **静默日志**：`monitor.silent=true` 时仅输出错误；默认详细日志。
- **调试落盘**：将原始 `modules` 与解析结果落盘便于对照排查（可按 UID 白名单）。
- **可开关配图**：需要纯文本推送时可全局关闭发图。

---

## 📦 安装

> 以 **MaiBot 一键包**（Windows）为例：

1. 将整个插件目录（例如 `bilibili_push_plugin`，内含 `plugin.py`）放入：  
   ```text
   D:\MaiBotOneKey\modules\MaiBot\plugins\bilibili_push_plugin
   ```
2. **（可选）安装 Pillow**：用于压缩/转 JPG，提高发图成功率（未安装也可运行，只是少一步优化）。  
   ```powershell
   # 进入 MaiBot 的 Python 环境后执行
   pip install pillow -i https://mirrors.aliyun.com/pypi/simple
   ```
3. 启动一次 MaiBot，让插件创建默认数据目录；然后按下文**配置**。
4. 重启生效。

> **Napcat 适配器**：插件通过 MaiBot 的 `send_api.custom_message` 发送消息，无需单独配置 Napcat 端口；确保 Napcat 在线且对目标群具备发图/发文本权限。

---

## ⚙️ 配置（`config.toml` 示例）

> 只展示关键项；你可以合并到总配置或插件私有配置里。

```toml
[monitor]
enable = true              # 是否启用插件
interval_minutes = 3       # 轮询间隔（分钟）
jitter_seconds = 15        # 抖动（避免固定周期被限流）
silent = false             # 静默日志：true=仅输出错误

[bilibili]
# 建议使用登录 Cookie（整条放入，至少有 SESSDATA 与 bili_jct），可显著减少 -352 风控
cookie = "SESSDATA=xxx; bili_jct=xxx; ..."

# 方式一：老式简单路由（所有 UID 推同一批群）
uids   = ["114514", "36081646"]
groups = ["1919810"]

# 方式二：细粒度路由（推荐）
# routes = [
#   { uids=["114514","36081646"], groups=["1919810"] },
#   { uids=["1919810","111222333"],       groups=["31415926535","123456789"] }
# ]

[api]
prefer_old = true          # true=先走旧接口 space_history；false=先走新接口 polymer/web

[fallback]
enable_html = true         # 两套接口都失败时，解析空间 HTML 兜底

[image]
send_images = true         # 关闭后只发文字与链接
force_base64 = true        # 优先 Base64（Napcat 最稳）
downscale_width = 720      # 转 JPG 限宽（建议 720~1080）
jpeg_quality = 85          # JPG 质量
base64_chunk_limit = 5500000  # Base64 最大字节（约 5.5MB）
per_image_delay_ms = 1600  # 逐张发图的间隔，避免风控

[debug]
dump_json = false          # 开启后落盘解析用 JSON（位于插件同级的 debug/）
dump_uid  = ["114514"]   # 仅对这些 UID 落盘（留空=全部）
# output_dir = "D:/tmp/bili_debug" # 指定调试输出目录；缺省写到插件同级的 debug/
```

---

## ▶️ 运行逻辑

1. 按 `interval_minutes` 定时轮询：
   - 使用 **旧接口** 或 **新接口** 拉取（由 `prefer_old` 决定优先级）；
   - 遇到 `code = -352` 自动刷新 WBI 再重试；
   - 两套接口都拿不到有效数据时走 **HTML 兜底**。
2. 过滤“置顶”动态 → 与 `last_seen` 对比 → **只推送最新一条**。
3. 文案优先从 `module_dynamic.desc.rich_text_nodes` 提取；同时解析 **AT/话题/表情/URL/换行** 等富文本节点。
4. 识别**转发动态**（`dyn_forward`/`orig`）：标题自动带“🔁 转发了 xxx 的动态”，并附原文摘要/配图。
5. 发送顺序：**文本** → **图片（逐张）**。图片失败会补发直链文本兜底。

> **冷启动**：首次运行只记录 `last_seen`，**不推送**历史动态，避免刷屏。

---

## 📝 推送效果示例

- 发布新动态（纯文字/图文）：
  ```text
  📢 text_test账号 发布了新动态：
  做一个测试
  🔗 https://t.bilibili.com/114514
  ```

- 转发动态：
  ```text
  🔁 text_test账号 转发了 text_test账号 的动态：
  这是一个转发测试
  ——原文：继续测试
  🔗 https://t.bilibili.com/114514
  ```

- 无正文场景会给出提示：
  ```text
  📢 UID:354657... 发布了新动态：
  （无文字内容）
  🔗 https://t.bilibili.com/xxxxxxxxxxxxxxxxx
  ```

> 实际文案会随富文本与动态类型（`opus / article / archive / live / pgc` 等）自动调整。

---

## 🖼️ 图片发送策略（Napcat 友好）

- 首选 **Base64**（只放**纯 Base64** 字符串，不要加 `base64://` 前缀）；
- 失败回退到 **URL 直链**；再失败回退到 **`file:///` 本地临时文件**；
- 可自动 **转 JPG** + **限宽/质量**，降低“富媒体失败”的概率；
- 多图按 `per_image_delay_ms` 逐张发送，避免触发风控。

---

## ❓ 常见问题（FAQ）

### Q1. 日志里出现 `code = -352`
- 多半是 **未登录/风控**。请确认 `[bilibili].cookie` 是**完整的一条**（含 `SESSDATA`、`bili_jct` 等）且未换行；
- 插件会自动刷新 WBI 并重试；若仍失败，适当加大 `interval_minutes` 并等待一段时间。

### Q2. 发送图片报错 `rich media transfer failed`
- 这是 QQ 侧富媒体失败的通用报错。建议：  
  - 保持 `image.force_base64 = true`（**纯 Base64** 最稳，**不要**加 `base64://`）；  
  - 降低图片宽度（如 `downscale_width = 720`）、质量（如 `jpeg_quality = 80`）；  
  - 调大间隔 `per_image_delay_ms`（如 1800–2200ms）；  
  - 仍不稳定时，可将 `image.send_images = false`，仅推文本+链接。

### Q3. 控制台太吵 / 只想看错误
- 设置 `monitor.silent = true` 即可仅输出错误日志。

### Q4. 我看不到 debug JSON
- 打开 `debug.dump_json = true`，并确认：  
  - `debug.dump_uid` 包含目标 UID（或留空表示全部）；  
  - `debug.output_dir` 指向可写目录。  
- 文件名形如：`debug_module_<uid>_<dynamic_id>.json`。

### Q5. 我只想把 A、B 两个 UP 推到群 X，C 推到群 Y
- 使用 `[bilibili].routes` 配置，分别写两条路由即可（见上方示例）。

---

## 📁 目录结构（建议）

```
bilibili_push_plugin/
├── plugin.py          # 插件主体
├── debug/             # （可选）调试落盘目录
└── tmp_images/        # （运行时）发图兜底的本地缓存
```
> `last_seen.json` 会保存在插件的数据目录（MaiBot 的 data 路径）下，自动创建。

---

## 🔒 注意与声明

- 请遵守 B 站与 QQ 的相关条款与限制，勿恶意抓取与滥用推送；
- Cookie 仅用于访问你授权的 B 站接口，请妥善保管，**不要泄露**。

---

## 🙏 鸣谢

- 感谢 MaiBot / Napcat 生态与所有贡献者；
- 部分思路参考了社区内的图片发送实践与轮询稳定性经验。
