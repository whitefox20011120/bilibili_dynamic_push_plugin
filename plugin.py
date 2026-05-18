import asyncio

from maibot_sdk import MaiBotPlugin, Command, CONFIG_RELOAD_SCOPE_SELF

from .config import BiliPluginConfig
from .monitor import monitor_instance
from .subscription import sub_manager
from .commands import handle_command
from .utils import BiliUtils, fetch_uname, fetch_fans
from datetime import datetime


class BiliPlugin(MaiBotPlugin):
    config_model = BiliPluginConfig

    async def on_load(self) -> None:
        asyncio.create_task(self._auto_start())

    async def _auto_start(self):
        await asyncio.sleep(5)
        if self.config.plugin.enabled:
            await monitor_instance.start(self.ctx, self.config)

    async def on_unload(self) -> None:
        await monitor_instance.stop()

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        if scope == CONFIG_RELOAD_SCOPE_SELF:
            self.ctx.logger.info(f"B站监控配置已热重载更新: {version}")
            monitor_instance.config = self.config
            await monitor_instance.update_subscription_map()

    @Command(
        "B动态",
        description="B站订阅控制",
        pattern=r"^/B动态\s+(?P<action>start|stop|status|test|info|add|remove|list|help)(?:\s+(?P<arg>.*))?\s*$",
    )
    async def handle_bili_control(self, stream_id: str = "", matched_groups: dict = None, **kwargs) -> tuple:
        base_info = kwargs.get("message_base_info", {})
        current_user = kwargs.get("user_id") or base_info.get("user_info", {}).get("user_id")

        group_id = kwargs.get("group_id")
        if not group_id and "raw_event" in kwargs:
            raw_event = kwargs["raw_event"]
            if isinstance(raw_event, dict):
                group_id = raw_event.get("group_id")
            else:
                group_id = getattr(raw_event, "group_id", None)
        if not group_id:
            group_id = base_info.get("group_id")

        if not group_id:
            self.ctx.logger.error(f"提取群号失败！当前 kwargs 包含的键有: {list(kwargs.keys())}")
            return False, "请在群聊内使用控制指令，以获取准确的真实群号(Group ID)", True

        admin_list = [str(x) for x in self.config.settings.admin_qqs]
        if current_user not in admin_list:
            self.ctx.logger.warning(f"⚠️ 非管理员尝试执行指令: {current_user}")
            return False, None, False

        async def reply_group(text: str):
            try:
                await self.ctx.api.call(
                    "adapter.napcat.message.send_msg",
                    params={
                        "message_type": "group",
                        "group_id": int(group_id),
                        "message": [{"type": "text", "data": {"text": text}}],
                    },
                )
            except Exception as e:
                self.ctx.logger.error(f"群消息反馈失败: {e}")

        action = matched_groups.get("action") if matched_groups else None
        arg = matched_groups.get("arg").strip() if matched_groups and matched_groups.get("arg") else None

        return await handle_command(self, action, arg, group_id, reply_group)

    @Command(
        "B粉丝",
        description="查询B站UP主粉丝数",
        pattern=r"^/B粉丝\s+(?P<uid>\d+)\s*$",
    )
    async def handle_bili_fans(self, matched_groups: dict = None, **kwargs) -> tuple:
        # ---- 取 group_id（复用 handle_bili_control 的逻辑） ----
        base_info = kwargs.get("message_base_info", {})
        group_id = kwargs.get("group_id")
        if not group_id and "raw_event" in kwargs:
            raw_event = kwargs["raw_event"]
            if isinstance(raw_event, dict):
                group_id = raw_event.get("group_id")
            else:
                group_id = getattr(raw_event, "group_id", None)
        if not group_id:
            group_id = base_info.get("group_id")
        if not group_id:
            return False, "请在群聊内使用 /B粉丝", True

        async def reply_group(text: str):
            try:
                await self.ctx.api.call(
                    "adapter.napcat.message.send_msg",
                    params={
                        "message_type": "group",
                        "group_id": int(group_id),
                        "message": [{"type": "text", "data": {"text": text}}],
                    },
                )
            except Exception as e:
                self.ctx.logger.error(f"群消息反馈失败: {e}")

        uid = matched_groups.get("uid") if matched_groups else None
        if not uid:
            await reply_group("❌ 用法：/B粉丝 <UID>")
            return True, None, True

        fans = await fetch_fans(uid, monitor_instance.credential)
        if fans < 0:
            await reply_group(f"❌ 查询失败，请检查 UID 是否正确：{uid}")
            return True, None, True

        uname = sub_manager.get_name(uid) or await fetch_uname(uid, monitor_instance.credential) or "未知UP主"
        if uname and uname != "未知UP主":
            await sub_manager.set_name(uid, uname)

        user_hist = monitor_instance.history.get(uid, {}) or {}
        if isinstance(user_hist, str):
            user_hist = {}
        milestones = user_hist.get("fans_milestones", {}) or {}
        current_ms = BiliUtils.get_current_milestone(fans)

        ms_line = ""
        if current_ms >= 10_000:
            ts = milestones.get(str(current_ms))
            if ts:
                ms_line = (
                    f"\n🏁 当前里程碑：{BiliUtils.format_fans(current_ms)}"
                    f"（{datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')} 达成）"
                )
            else:
                ms_line = f"\n🏁 当前里程碑：{BiliUtils.format_fans(current_ms)}（达成时间未记录）"

        step = BiliUtils.get_milestone_step(fans) or 10_000
        next_ms = (fans // step + 1) * step if fans >= 10_000 else 10_000
        remain = next_ms - fans

        msg = (
            f"📈 【{uname}】粉丝数信息\n"
            f"👥 当前粉丝：{fans:,}（{BiliUtils.format_fans(fans)}）"
            f"{ms_line}\n"
            f"🎯 下一里程碑：{BiliUtils.format_fans(next_ms)}（还差 {remain:,}）\n"
            f"🔗 https://space.bilibili.com/{uid}"
        )
        await reply_group(msg)
        return True, None, True

def create_plugin():
    return BiliPlugin()
