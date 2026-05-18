from maibot_sdk import Field, PluginConfigBase


class PluginSection(PluginConfigBase):
    __ui_label__ = "插件开关"
    enabled: bool = Field(default=True, description="是否启用")
    config_version: str = Field(default="1.0.0", description="配置文件版本")


class SettingsSection(PluginConfigBase):
    __ui_label__ = "设置"
    poll_interval: int = Field(default=120, description="轮询基准秒数")
    poll_jitter: int = Field(default=10, description="轮询抖动秒数(实际=基准±抖动)")
    admin_qqs: list[str] = Field(default_factory=list, description="管理员QQ列表")
    credential: dict = Field(default_factory=dict, description="Cookie凭证")
    max_images: int = Field(default=3, description="最大图片数")
    ignore_lottery: bool = Field(default=True, description="自动丢弃开奖动态")
    max_dynamic_age: int = Field(
        default=3600,
        description="动态最大有效时长(秒)，超过则不推送，默认3600=1小时",
    )


class SubscriptionsSection(PluginConfigBase):
    __ui_label__ = "订阅"
    users: list[dict] = Field(
        default_factory=lambda: [{"uid": "114514", "groups": ["1919810"]}],
        description="订阅列表",
    )


class BiliPluginConfig(PluginConfigBase):
    plugin: PluginSection = Field(default_factory=PluginSection)
    settings: SettingsSection = Field(default_factory=SettingsSection)
    subscriptions: SubscriptionsSection = Field(default_factory=SubscriptionsSection)