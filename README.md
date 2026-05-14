# B站动态/直播监控插件（已适配maisaka）

这是一个用于监控 Bilibili UP主 **动态更新** 和 **直播状态** 的插件。支持多群订阅、多用户监控，并具备防检测的随机查询间隔机制。使用/B动态 help即可查询指令（仅配置的管理员可用）。

## ✨ 主要功能

* **动态推送**：实时监控 UP 主发布的图文动态、视频投稿、文章等，并推送到指定群组。
* 支持转发动态显示原作者。
* 智能折叠：当动态图片超过设定数量（如 5 张）时，则将图片打包，合并转发到群聊，防止刷屏。


* **直播通知**：
* 🔴 开播提醒（包含封面、标题、链接）。
* 🏁 下播提醒。


* **防检测机制**：支持配置查询间隔抖动，模拟真人行为，降低被B站API封禁的风险。

* **手动指令**：支持手动添加/删除订阅。
* 🛠️ Bilibili 订阅管理指令
* ➕ /B动态 add [UID] 添加当前群聊对该 UID 的订阅
* ➖ /B动态 remove [UID] 移除当前群聊的动态订阅
* 📋 /B动态 list 列出当前群组的所有订阅源
* 🔍 /B动态 info [UID] 查询该 UID 实时直播状态
* 🧪 /B动态 test [UID] 触发一次动态推送测试

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
poll_jitter = 10       # 随机抖动时间（秒）。
                       # 例如：设为 120 和 10，则每次间隔在 110s~130s 之间随机。
admin_qqs = ["114514"] # 管理员QQ号。

max_images = 3         # 单条推送最大图片数，超过此数量则图片打包，合并转发到群聊中。
ignore_lottery = true  # 启用后自动丢弃开奖类动态。
max_dynamic_age = 600  # 动态最大有效时长(秒)，超过则不推送。目的为了维护期间错过的旧动态不在重启时推送。

#建议生成config后将以下内容（模板）粘过去替换，按一下格式填写配置信息。
[settings.credential]
# B站凭证（可选，建议配置以提高访问稳定性或查看受限内容）
# 获取方式：浏览器登录B站 -> F12 -> Application（应用） -> Cookies（左边那一堆），去 https://www.bilibili.com 那里
sessdata = "your_sessdata_here"
bili_jct = "your_bili_jct_here"
buvid3 = "your_buvid3_here"

[subscriptions]
# 订阅列表
# uid: UP主的UID（多个ID请使用uids）
# groups: 需要推送到的群号列表
# 如果你觉得麻烦就去制定群聊使用/B动态 add和/B动态 remove增删订阅
users = [
    { uid = "114514", groups = ["12345678", "87654321"] },
    { uids = ["114514", "1919810"], groups = ["12345678", "87654321"] },
    { uid = "36081646", groups = ["12345678"] }
]

```
## 📂 文件结构

* `plugin.py`: 插件主代码。
* `history.json`: **(自动生成)** 存储每个 UID 上次检测到的动态 ID 和直播状态，用于去重。请勿手动修改。
* `subscriptions.json`: **(自动生成)** 存储推送信息，用于指令增删配置使用。请勿手动修改。
* `config.toml`: 配置文件。

## ⚠️ 注意事项

1. **关于风控**：虽然插件加入了 `poll_jitter` 抖动机制，但仍建议不要将 `poll_interval` 设置得过短（建议大于 60秒）。
2. **凭证刷新**：插件内置了凭证（Cookie）自动刷新机制（每 6 小时检查一次），但如果 Cookie 彻底失效，仍需手动在配置文件中更新。
3. **首次运行**：添加新订阅的 UID 后，第一次轮询会将其当前最新动态标记为“已读”（基准点），**不会**推送旧动态，只有之后产生的新动态才会推送。
4. **这点很重要**：请不要手动去更改插件目录下自动生成的history.json和subscriptions.json！！！
---

**License**: MIT
