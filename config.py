"""Bilibili 动态订阅插件配置模型。"""

from __future__ import annotations

from typing import ClassVar, List

from maibot_sdk import Field, PluginConfigBase


class PluginSection(PluginConfigBase):
    __ui_label__: ClassVar[str] = "插件开关"
    __ui_order__: ClassVar[int] = 0

    enabled: bool = Field(
        default=True,
        description="是否启用本插件。",
        json_schema_extra={
            "hint": "插件开关。",
            "label": "启用插件",
            "order": 0,
        },
    )
    config_version: str = Field(
        default="1.0.0",
        json_schema_extra={"disabled": True, "hidden": True, "label": "配置版本", "order": 99},
    )


class CredentialSection(PluginConfigBase):
    """Bilibili 登录 Cookie 凭证。"""

    __ui_label__: ClassVar[str] = "Cookie 凭证"
    __ui_order__: ClassVar[int] = 1

    sessdata: str = Field(
        default="",
        description="B 站 Cookie 中的 SESSDATA（必填）。",
        json_schema_extra={
            "hint": "必填。F12 → Application → Cookies → bilibili.com 复制 SESSDATA。",
            "label": "SESSDATA（必填）",
            "placeholder": "xxxxxxxx%2Cxxxx%2Cxxxxx*xx",
            "order": 0,
            "required": True,
        },
    )
    bili_jct: str = Field(
        default="",
        description="B 站 Cookie 中的 bili_jct（必填）。",
        json_schema_extra={
            "hint": "必填。32 位十六进制字符串。",
            "label": "bili_jct（必填）",
            "placeholder": "32 位 hex",
            "order": 1,
            "required": True,
        },
    )
    buvid3: str = Field(
        default="",
        description="buvid3（选填）。",
        json_schema_extra={
            "hint": "选填。部分接口需要，填上更稳。",
            "label": "buvid3（选填）",
            "placeholder": "可留空",
            "order": 2,
        },
    )
    dedeuserid: str = Field(
        default="",
        description="DedeUserID（选填）。",
        json_schema_extra={
            "hint": "选填。即你的 B 站 UID。",
            "label": "DedeUserID（选填）",
            "placeholder": "可留空",
            "order": 3,
        },
    )
    ac_time_value: str = Field(
        default="",
        description="ac_time_value（选填）。",
        json_schema_extra={
            "hint": "选填。配置后可自动刷新 Cookie，避免过期。",
            "label": "ac_time_value（选填）",
            "placeholder": "可留空",
            "order": 4,
        },
    )


class SettingsSection(PluginConfigBase):
    __ui_label__: ClassVar[str] = "设置"
    __ui_order__: ClassVar[int] = 2

    poll_interval: int = Field(
        default=120,
        json_schema_extra={
            "hint": "每隔该秒数轮询一次，建议不低于 60 秒。",
            "label": "轮询基准（秒）",
            "order": 0, "step": 1,
        },
    )
    poll_jitter: int = Field(
        default=10,
        json_schema_extra={
            "hint": "实际间隔 = 基准 ± 抖动。",
            "label": "轮询抖动（秒）",
            "order": 1, "step": 1,
        },
    )
    admin_qqs: list[str] = Field(
        default_factory=list,
        json_schema_extra={
            "hint": "管理员可以通过命令操作订阅。",
            "label": "管理员 QQ",
            "order": 2,
            "placeholder": "请输入 QQ 号",
        },
    )
    max_images: int = Field(
        default=3,
        json_schema_extra={
            "hint": "图片超过该数量自动改用合并转发。",
            "label": "最大图片数",
            "order": 3, "step": 1,
        },
    )
    ignore_lottery: bool = Field(
        default=True,
        json_schema_extra={
            "hint": "开启后含开奖关键字的动态会被丢弃。",
            "label": "忽略开奖动态",
            "order": 4,
        },
    )
    max_dynamic_age: int = Field(
        default=3600,
        json_schema_extra={
            "hint": "超过该时长的动态不再推送（秒）。",
            "label": "动态最大有效时长（秒）",
            "order": 5, "step": 60,
        },
    )
    auto_like: bool = Field(
        default=False,
        json_schema_extra={
            "hint": "开启后，监控到 UP 主新动态时会自动点赞（需填写 SESSDATA 与 bili_jct）。",
            "label": "自动点赞新动态",
            "order": 6,
        },
    )


class SubscriptionsSection(PluginConfigBase):
    """订阅配置：每行一组 = "UID => 群号1, 群号2"。"""

    __ui_label__: ClassVar[str] = "订阅"
    __ui_order__: ClassVar[int] = 3

    users: List[str] = Field(
        default_factory=lambda: ["114514 => 1919810"],
        description='每行一组订阅，格式："UID => 群号1, 群号2"。',
        json_schema_extra={
            "hint": (
                "每行一组订阅。\n"
                "格式：UID => 群号1, 群号2\n"
                "示例：114514 => 1919810, 123456\n"
                "支持的分隔符：=> | : ｜ ， ,"
            ),
            "label": "订阅列表（每行一组）",
            "placeholder": "114514 => 1919810, 123456",
            "order": 0,
            "required": True,
        },
    )


class BiliPluginConfig(PluginConfigBase):
    plugin: PluginSection = Field(default_factory=PluginSection)
    credential: CredentialSection = Field(default_factory=CredentialSection)  
    settings: SettingsSection = Field(default_factory=SettingsSection)
    subscriptions: SubscriptionsSection = Field(default_factory=SubscriptionsSection)
