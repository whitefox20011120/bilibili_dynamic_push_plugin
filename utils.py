"""
工具函数
"""
import os
import json
import base64
import asyncio
import logging
from typing import Any, Dict, Optional

import aiohttp
from bilibili_api import user

logger = logging.getLogger("bilibili_dynamic_push")


async def fetch_uname(uid: str, credential) -> str:
    """根据 UID 拉取 B 站昵称，失败返回空串"""
    try:
        u = user.User(int(uid), credential=credential)
        info = await u.get_user_info()
        return info.get("name", "") or ""
    except Exception as e:
        logger.error(f"获取 UID {uid} 昵称失败: {e}")
        return ""

async def fetch_fans(uid: str, credential) -> int:
    """获取当前粉丝数，失败返回 -1"""
    try:
        u = user.User(int(uid), credential=credential)
        rel = await u.get_relation_info()
        return int(rel.get("follower", 0))
    except Exception as e:
        logger.error(f"获取 UID {uid} 粉丝数失败: {e}")
        return -1

class BiliUtils:
    @staticmethod
    async def url_to_base64(url: str, session: aiohttp.ClientSession) -> Optional[str]:
        if not url or not session:
            return None
        if "hdslb.com" in url and "@" not in url and not url.lower().endswith(".gif"):
            url = f"{url}@1080w_1e_1c.webp"
        try:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    return base64.b64encode(data).decode("utf-8")
        except Exception as e:
            logger.error(f"图片下载失败: {url}, 错误: {e}")
            return None

    @staticmethod
    def get_history_path() -> str:
        return os.path.join(os.path.dirname(__file__), "history.json")

    @staticmethod
    def load_history() -> Dict[str, Any]:
        path = BiliUtils.get_history_path()
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    @staticmethod
    async def save_history(data: Dict[str, Any]):
        def _write():
            try:
                with open(BiliUtils.get_history_path(), "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
            except Exception:
                pass

        await asyncio.to_thread(_write)

    @staticmethod
    def format_duration(seconds: float) -> str:
        m, s = divmod(int(seconds), 60)
        h, m = divmod(m, 60)
        if h > 0:
            return f"{h}小时{m}分{s}秒"
        return f"{m}分{s}秒"

    @staticmethod
    def get_milestone_step(fans: int) -> int:
        """根据当前粉丝数返回里程碑步长，<1万返回 0 表示不提醒"""
        if fans < 10_000:
            return 0
        if fans < 100_000:        # 1万 ~ 9.9999万
            return 10_000
        if fans < 1_000_000:      # 10万 ~ 99.9999万
            return 100_000
        if fans < 10_000_000:     # 100万 ~ 999.9999万
            return 500_000
        return 1_000_000          # 1000万以上

    @staticmethod
    def get_current_milestone(fans: int) -> int:
        """向下取整到当前里程碑"""
        step = BiliUtils.get_milestone_step(fans)
        if step == 0:
            return 0
        return (fans // step) * step

    @staticmethod
    def format_fans(n: int) -> str:
        """10000 -> 1万, 1000000 -> 100万, 10000000 -> 1000万"""
        if n >= 10000:
            v = n / 10000
            return f"{v:.4f}".rstrip("0").rstrip(".") + "万"
        return str(n)
