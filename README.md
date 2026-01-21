# Bilibili Dynamic Subscription Plugin (B站动态/直播监控插件)

这是一个用于监控 Bilibili UP主 **动态更新** 和 **直播状态** 的插件。支持多群订阅、多用户监控，并具备防检测的随机查询间隔机制。

## ✨ 主要功能

* **动态推送**：实时监控 UP 主发布的图文动态、视频投稿、文章等，并推送到指定群组。
* 支持转发动态显示原作者。
* 智能折叠：当动态图片超过设定数量（如 9 张）时，自动转为纯文本提示，防止刷屏。


* **直播通知**：
* 🔴 开播提醒（包含封面、标题、链接）。
* 🏁 下播提醒。


* **防检测机制**：支持配置查询间隔抖动（Jitter），模拟真人行为，降低被 B 站 API 封禁的风险。
* **热加载配置**：支持通过指令热控制插件启停。

## 📦 依赖项

在使用前，请确保安装了以下 Python 库：

```bash
pip install bilibili-api-python aiohttp

```

## ⚙️ 配置文件说明 (`config.toml`)

插件首次运行后会自动生成配置模板。以下是关键配置项的详细说明：

```toml
[plugin]
enabled = true  # 插件总开关

[settings]
poll_interval = 120    # 基础查询周期（秒），建议不低于 60
poll_jitter = 10       # [新增] 随机抖动时间（秒）。
                       # 实际查询间隔 = base ± jitter。
                       # 例如：设为 120 和 10，则每次间隔在 110s~130s 之间随机。

max_images = 3         # 单条推送最大图片数，超过此数量将不发送图片，仅发送链接。

[settings.credential]
# B站凭证（可选，建议配置以提高访问稳定性或查看受限内容）
# 获取方式：浏览器登录B站 -> F12 -> Application -> Cookies
sessdata = "your_sessdata_here"
bili_jct = "your_bili_jct_here"
buvid3 = "your_buvid3_here"

[subscriptions]
# 订阅列表
# uid: UP主的数字ID
# groups: 需要推送到的群号列表
users = [
    { uid = "114514", groups = ["12345678", "87654321"] },
    { uid = "36081646", groups = ["12345678"] }
]

```
## 📂 文件结构

* `plugin.py`: 插件主代码。
* `history.json`: **(自动生成)** 存储每个 UID 上次检测到的动态 ID 和直播状态，用于去重。请勿手动修改。
* `config.toml`: 配置文件。

## ⚠️ 注意事项

1. **关于风控**：虽然插件加入了 `poll_jitter` 抖动机制，但仍建议不要将 `poll_interval` 设置得过短（建议大于 60秒）。
2. **凭证刷新**：插件内置了凭证（Cookie）自动刷新机制（每 6 小时检查一次），但如果 Cookie 彻底失效，仍需手动在配置文件中更新。
3. **首次运行**：添加新订阅的 UID 后，第一次轮询会将其当前最新动态标记为“已读”（基准点），**不会**推送旧动态，只有之后产生的新动态才会推送。

---

**License**: MIT
