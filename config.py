"""Bilibili 动态订阅插件配置模型。"""

from __future__ import annotations

from typing import ClassVar

from maibot_sdk import Field, PluginConfigBase


class PluginSection(PluginConfigBase):
    """插件级开关。"""

    __ui_label__: ClassVar[str] = "插件开关"
    __ui_order__: ClassVar[int] = 0

    enabled: bool = Field(
        default=True,
        description="是否启用本插件。",
        json_schema_extra={
            "hint": "关闭后插件保持空闲，不再轮询 Bilibili 动态。",
            "label": "启用插件",
            "order": 0,
        },
    )
    config_version: str = Field(
        default="1.0.0",
        description="当前配置结构版本。",
        json_schema_extra={
            "disabled": True,
            "hidden": True,
            "label": "配置版本",
            "order": 99,
        },
    )


class SettingsSection(PluginConfigBase):
    """运行参数设置。"""

    __ui_label__: ClassVar[str] = "设置"
    __ui_order__: ClassVar[int] = 1

    poll_interval: int = Field(
        default=120,
        description="轮询基准秒数。",
        json_schema_extra={
            "hint": "每隔该秒数轮询一次 Bilibili 动态，建议不低于 60 秒。",
            "label": "轮询基准（秒）",
            "order": 0,
            "step": 1,
        },
    )
    poll_jitter: int = Field(
        default=10,
        description="轮询抖动秒数。",
        json_schema_extra={
            "hint": "实际轮询间隔 = 基准 ± 抖动，避免请求过于规律。",
            "label": "轮询抖动（秒）",
            "order": 1,
            "step": 1,
        },
    )
    admin_qqs: list[str] = Field(
        default_factory=list,
        description="管理员 QQ 列表。",
        json_schema_extra={
            "hint": "管理员可以通过命令操作订阅。",
            "label": "管理员 QQ",
            "order": 2,
            "placeholder": "请输入 QQ 号",
        },
    )
    credential: dict = Field(
        default_factory=dict,
        description="Bilibili Cookie 凭证。",
        json_schema_extra={
            "hint": "包含 SESSDATA、bili_jct 等字段，用于访问需要登录的接口。",
            "label": "Cookie 凭证",
            "order": 3,
        },
    )
    max_images: int = Field(
        default=3,
        description="动态推送时最多附带的图片数量。",
        json_schema_extra={
            "hint": "超过该数量的图片会被丢弃，0 表示不发送图片。",
            "label": "最大图片数",
            "order": 4,
            "step": 1,
        },
    )
    ignore_lottery: bool = Field(
        default=True,
        description="是否自动丢弃开奖动态。",
        json_schema_extra={
            "hint": "开启后含有抽奖/开奖关键字的动态将不会被推送。",
            "label": "忽略开奖动态",
            "order": 5,
        },
    )
    max_dynamic_age: int = Field(
        default=3600,
        description="动态最大有效时长（秒）。",
        json_schema_extra={
            "hint": "超过该时长的动态不再推送，默认 3600 秒（1 小时）。",
            "label": "动态最大有效时长（秒）",
            "order": 6,
            "step": 60,
        },
    )


class SubscriptionsSection(PluginConfigBase):
    """订阅配置。"""

    __ui_label__: ClassVar[str] = "订阅"
    __ui_order__: ClassVar[int] = 2

    users: list[dict] = Field(
        default_factory=lambda: [{"uid": "114514", "groups": ["1919810"]}],
        description="订阅的 UP 主及其推送目标群。",
        json_schema_extra={
            "hint": "每项包含 uid（UP 主 UID）和 groups（推送的群号列表）。",
            "label": "订阅列表",
            "order": 0,
        },
    )


class BiliPluginConfig(PluginConfigBase):
    """Bilibili 插件完整配置。"""

    plugin: PluginSection = Field(default_factory=PluginSection)
    settings: SettingsSection = Field(default_factory=SettingsSection)
    subscriptions: SubscriptionsSection = Field(default_factory=SubscriptionsSection)
