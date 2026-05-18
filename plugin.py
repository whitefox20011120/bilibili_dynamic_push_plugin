import asyncio

from maibot_sdk import MaiBotPlugin, Command, CONFIG_RELOAD_SCOPE_SELF

from .config import BiliPluginConfig
from .monitor import monitor_instance
from .commands import handle_command


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


def create_plugin():
    return BiliPlugin()