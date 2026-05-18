import asyncio
import json
import base64
import aiohttp
import os
import time
import random
import re
import hashlib
from datetime import datetime
from urllib.parse import unquote
from typing import Dict, Any, List, Optional, Tuple
from asyncio import Lock

from maibot_sdk import MaiBotPlugin, Command, Field, PluginConfigBase, CONFIG_RELOAD_SCOPE_SELF
from bilibili_api import user, Credential
import logging

logger = logging.getLogger("bilibili_dynamic_push")

# ================= 1. 配置模型 =================
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
    max_dynamic_age: int = Field(default=3600, description="动态最大有效时长(秒)，超过则不推送，默认3600=1小时")

class SubscriptionsSection(PluginConfigBase):
    __ui_label__ = "订阅"
    users: list[dict] = Field(
        default_factory=lambda: [{"uid": "114514", "groups": ["1919810"]}], 
        description="订阅列表"
    )

class BiliPluginConfig(PluginConfigBase):
    plugin: PluginSection = Field(default_factory=PluginSection)
    settings: SettingsSection = Field(default_factory=SettingsSection)
    subscriptions: SubscriptionsSection = Field(default_factory=SubscriptionsSection)

# ================= 工具函数 =================
async def fetch_uname(uid: str, credential) -> str:
    """根据 UID 拉取 B 站昵称，失败返回空串"""
    try:
        u = user.User(int(uid), credential=credential)
        info = await u.get_user_info()
        return info.get('name', '') or ''
    except Exception as e:
        logger.error(f"获取 UID {uid} 昵称失败: {e}")
        return ''

# ================= 2. 订阅管理器 (动静结合) =================
class SubscriptionManager:
    def __init__(self):
        self.file_path = os.path.join(os.path.dirname(__file__), "subscriptions.json")
        self.lock = asyncio.Lock()
        self.data = {"static": {}, "custom": {}, "names": {}}  # 👈 新增 names
        self._load_sync()
    def _load_sync(self):
        if os.path.exists(self.file_path):
            try:
                with open(self.file_path, 'r', encoding='utf-8') as f:
                    self.data = json.load(f)
            except Exception:
                self.data = {"static": {}, "custom": {}, "names": {}}
        else:
            self.data = {"static": {}, "custom": {}, "names": {}}
        # 兼容老文件
        self.data.setdefault("static", {})
        self.data.setdefault("custom", {})
        self.data.setdefault("names", {})

    async def save(self):
        async with self.lock:
            def _write():
                with open(self.file_path, 'w', encoding='utf-8') as f:
                    json.dump(self.data, f, indent=2, ensure_ascii=False)
            await asyncio.to_thread(_write)

    async def sync_static(self, config_users: list):
        new_static = {}
        for sub in config_users:
            raw_groups = sub.get("groups", [])
            uids = []
            if sub.get("uid"): uids.append(str(sub["uid"]))
            if sub.get("uids") and isinstance(sub.get("uids"), list): 
                uids.extend([str(x) for x in sub["uids"]])
            
            for uid in uids:
                if uid not in new_static:
                    new_static[uid] = []
                new_static[uid].extend([int(g) for g in raw_groups])
        
        async with self.lock:
            self.data["static"] = new_static
        await self.save()

    def get_merged_map(self) -> dict:
        merged = {}
        for source in ["static", "custom"]:
            for uid, groups in self.data.get(source, {}).items():
                if uid not in merged:
                    merged[uid] = set()
                merged[uid].update(groups)
        return merged

    def get_name(self, uid: str) -> str:
        return self.data.get("names", {}).get(str(uid), "")
    async def set_name(self, uid: str, name: str):
        if not name:
            return
        async with self.lock:
            self.data.setdefault("names", {})[str(uid)] = name
        await self.save()

sub_manager = SubscriptionManager()

# ================= 3. 辅助工具类 =================
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
                    return base64.b64encode(data).decode('utf-8')
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
                with open(path, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    # 修改 BiliUtils 的保存方法
    @staticmethod
    async def save_history(data: Dict[str, Any]):
        def _write():
            try:
                with open(BiliUtils.get_history_path(), 'w', encoding='utf-8') as f:
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
        else:
            return f"{m}分{s}秒"

# ================= 4. 核心监控逻辑 =================
class BiliMonitor:
    def __init__(self):
        self.running = False
        self.history = BiliUtils.load_history()
        self.credential = None
        self._tasks = []
        self.ctx = None
        self.config = None 
        self.session = None 
        self.uid_to_stream_ids = {} 
    
    @staticmethod
    def _is_top_dynamic(item: Dict) -> bool:
        try:
            modules = item.get('modules') or {}
            module_tag = modules.get('module_tag') or {}
            tag_text = module_tag.get('text') or ''
            if '置顶' in tag_text:
                return True
        except Exception:
            pass
        try:
            if (item.get('modules', {}).get('module_author', {}) or {}).get('is_top'):
                return True
        except Exception:
            pass
        return False

    async def update_subscription_map(self):
        if self.config and self.config.subscriptions.users:
            await sub_manager.sync_static(self.config.subscriptions.users)
        self.uid_to_stream_ids = sub_manager.get_merged_map()
        if self.ctx:
            self.ctx.logger.info(f"🔄 订阅映射已更新：当前共监控 {len(self.uid_to_stream_ids)} 个 B站 UID")

    async def start(self, ctx, config):
        if self.running:
            return
        self.running = True
        self.ctx = ctx
        self.config = config
        
        if not self.session or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=30)
            self.session = aiohttp.ClientSession(timeout=timeout)

        self.ctx.logger.info("🟢 启动 Bilibili 监控任务...")
        
        cred_dict = self.config.settings.credential
        if cred_dict and isinstance(cred_dict, dict):
            valid_cred = {}
            for k, v in cred_dict.items():
                if v:
                    if isinstance(v, str) and '%' in v:
                        try:
                            decoded_v = unquote(v)
                            valid_cred[k] = decoded_v
                        except Exception:
                            valid_cred[k] = v
                    else:
                        valid_cred[k] = v

            if valid_cred:
                try:
                    self.credential = Credential(**valid_cred)
                    self.ctx.logger.info("✅ B站凭证加载成功 (已自动解码)")
                except Exception as e: 
                    self.ctx.logger.error(f"❌ 凭证加载失败: {e}")
        
        await self.update_subscription_map()
        self._tasks.append(asyncio.create_task(self.loop()))
        self._tasks.append(asyncio.create_task(self.refresh_credential_loop()))

    async def stop(self):
        self.running = False
        for task in self._tasks:
            task.cancel()
        # 统一等待所有任务取消，忽略 CancelledError
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

        if self.session and not self.session.closed:
            await self.session.close()
            self.session = None

        if self.ctx:
            self.ctx.logger.info("🛑 Bilibili 监控停止")

    async def refresh_credential_loop(self):
        while self.running:
            await asyncio.sleep(3600 * 6)
            if self.credential:
                try:
                    if await self.credential.check_refresh():
                        await self.credential.refresh()
                        self.ctx.logger.info("🔄 B站凭据已自动刷新")
                except Exception as e:
                    self.ctx.logger.error(f"凭据刷新失败: {e}")

    async def loop(self):
        while self.running:
            try:
                if not self.config.plugin.enabled:
                    await asyncio.sleep(10)
                    continue

                base_interval = self.config.settings.poll_interval
                jitter = self.config.settings.poll_jitter
                max_imgs = self.config.settings.max_images

                if not self.uid_to_stream_ids:
                    await asyncio.sleep(base_interval)
                    continue

                actual_interval = base_interval
                if jitter > 0:
                    min_time = max(5, base_interval - jitter)
                    max_time = base_interval + jitter
                    actual_interval = random.randint(min_time, max_time)

                found_new_things = False
                for uid, stream_ids_set in self.uid_to_stream_ids.items():
                    target_stream_ids = list(stream_ids_set)
                    
                    pushed_dyn = await self.check_dynamic(uid, target_stream_ids, max_imgs)
                    pushed_live = await self.check_live(uid, target_stream_ids)
                    
                    if pushed_dyn or pushed_live:
                        found_new_things = True
                    
                    await asyncio.sleep(1)

                if found_new_things:
                    self.ctx.logger.info(f"✅ 本轮轮询完成：发现新动态/直播事件！等待 {actual_interval} 秒后进行下一轮。")
                else:
                    self.ctx.logger.info(f"💤 本轮轮询完成：未发现新动态。等待 {actual_interval} 秒后进行下一轮。")
                    
                await asyncio.sleep(actual_interval)
            except Exception as e:
                self.ctx.logger.error(f"❌ 轮询错误: {e}")
                await asyncio.sleep(60)

    async def check_dynamic(self, uid: str, stream_ids: List[str], max_imgs: int) -> bool:
        try:
            u = user.User(int(uid), credential=self.credential)
            dynamics = await u.get_dynamics_new()
            items = dynamics.get('items', [])
            if not items:
                return False

            user_hist = self.history.get(uid, {})
            if isinstance(user_hist, str):
                user_hist = {'dyn_id': user_hist}

            last_saved_id = user_hist.get('dyn_id')
            last_top_id = user_hist.get('top_dyn_id')  # 上次记录的置顶ID

            top_item = None
            normal_items = []
            for item in items:
                if item.get('type') == 'DYNAMIC_TYPE_LIVE_RCMD':
                    continue
                try:
                    major_type = (item.get('modules', {})
                                .get('module_dynamic', {})
                                .get('major', {}) or {}).get('type')
                    if major_type == 'MAJOR_TYPE_LIVE_RCMD':
                        continue
                except Exception:
                    pass

                if self._is_top_dynamic(item) and top_item is None:
                    top_item = item
                else:
                    normal_items.append(item)

            if not last_saved_id:
                if normal_items:
                    latest_id = max(int(it['id_str']) for it in normal_items)
                    user_hist['dyn_id'] = str(latest_id)
                else:
                    user_hist['dyn_id'] = str(items[0]['id_str'])

                if top_item:
                    user_hist['top_dyn_id'] = str(top_item['id_str'])

                self.ctx.logger.info(
                    f"UID {uid} 首次初始化动态，基准ID: {user_hist['dyn_id']}, "
                    f"置顶ID: {user_hist.get('top_dyn_id', '无')}"
                )
                self.history[uid] = user_hist
                await BiliUtils.save_history(self.history)
                return False

            new_items = []

            for item in normal_items:
                curr_id = int(item['id_str'])
                if curr_id > int(last_saved_id):
                    new_items.append(item)
                else:
                    break

            if top_item:
                top_id_str = str(top_item['id_str'])
                if top_id_str != str(last_top_id or ''):
                    if int(top_id_str) > int(last_saved_id):
                        new_items.append(top_item)
                        self.ctx.logger.info(
                            f"UID {uid} 检测到新的置顶动态: {top_id_str}（将推送）"
                        )
                    else:
                        self.ctx.logger.info(
                            f"UID {uid} 置顶动态变更为旧动态 {top_id_str}，"
                            f"仅更新记录，不推送"
                        )
                    user_hist['top_dyn_id'] = top_id_str
            else:
                if last_top_id:
                    self.ctx.logger.info(f"UID {uid} 置顶动态已被取消")
                    user_hist['top_dyn_id'] = None

            if not new_items:
                self.history[uid] = user_hist
                await BiliUtils.save_history(self.history)
                return False

            latest_item_to_push = max(new_items, key=lambda it: int(it['id_str']))
            latest_id_str = str(latest_item_to_push['id_str'])

            max_age = self.config.settings.max_dynamic_age if self.config else 3600
            try:
                raw_pub_ts = (latest_item_to_push.get('modules', {})
                            .get('module_author', {}).get('pub_ts', 0))
                pub_ts = int(raw_pub_ts) if raw_pub_ts else 0
            except (ValueError, TypeError, AttributeError):
                pub_ts = 0

            now_ts = time.time()
            if pub_ts > 0 and (now_ts - pub_ts) > max_age:
                age_str = BiliUtils.format_duration(now_ts - pub_ts)
                self.ctx.logger.info(
                    f"⏳ UID {uid} 发现新动态 {latest_id_str}，但发布于 {age_str} 前，"
                    f"超过设定阈值 {max_age} 秒，静默更新基准ID不推送。"
                )
                if not self._is_top_dynamic(latest_item_to_push):
                    user_hist['dyn_id'] = latest_id_str
                self.history[uid] = user_hist
                await BiliUtils.save_history(self.history)
                return False

            is_top_push = self._is_top_dynamic(latest_item_to_push)
            tag_str = "（📌置顶）" if is_top_push else ""
            self.ctx.logger.info(
                f"🎉 UID {uid} 发现新动态{tag_str}: {latest_id_str} "
                f"(准备推送到 {len(stream_ids)} 个流节点)"
            )
            await self.process_and_push(latest_item_to_push, stream_ids, max_imgs)

            if not is_top_push:
                user_hist['dyn_id'] = latest_id_str
            normal_new = [it for it in new_items if not self._is_top_dynamic(it)]
            if normal_new:
                max_normal_id = str(max(int(it['id_str']) for it in normal_new))
                if int(max_normal_id) > int(user_hist.get('dyn_id', 0)):
                    user_hist['dyn_id'] = max_normal_id

            self.history[uid] = user_hist
            await BiliUtils.save_history(self.history)
            return True

        except Exception as e:
            self.ctx.logger.error(f"UID {uid} 动态检查失败: {e}")
            return False

    async def check_live(self, uid: str, stream_ids: List[str]) -> bool:
        try:
            u = user.User(int(uid), credential=self.credential)
            raw_info = await u.get_live_info()
            
            live_room = raw_info.get('live_room', {})
            current_status = live_room.get('liveStatus', 0)
            room_title = live_room.get('title', '直播间')
            url = live_room.get('url', '')
            cover = live_room.get('cover', '') 
            uname = raw_info.get('name', 'UP主')

            user_hist = self.history.get(uid, {})
            if isinstance(user_hist, str):
                user_hist = {'dyn_id': user_hist}
            
            last_status = user_hist.get('live_status', 0)
            start_time = user_hist.get('live_start_time', 0)

            if 'live_status' not in user_hist:
                user_hist['live_status'] = current_status
                if current_status == 1:
                    user_hist['live_start_time'] = time.time()
                self.history[uid] = user_hist
                await BiliUtils.save_history(self.history)
                return False

            has_event = False
            if current_status == 1 and last_status == 0:
                self.ctx.logger.info(f"UID {uid} 开播")
                current_time = time.time()
                
                msg = (
                    f"🔴 【{uname}】开播了！\n"
                    f"📺 标题：{room_title}\n"
                    f"🔗 传送门：{url}\n"
                    f"⏰ 时间：{datetime.now().strftime('%H:%M:%S')}"
                )
                await self.push_simple(msg, cover, stream_ids)
                user_hist['live_start_time'] = current_time
                has_event = True
            
            elif current_status == 0 and last_status == 1:
                self.ctx.logger.info(f"UID {uid} 下播")
                
                duration_str = "未知"
                if start_time:
                    duration_sec = time.time() - start_time
                    duration_str = BiliUtils.format_duration(duration_sec)
                
                msg = (
                    f"🏁 【{uname}】下播了~\n"
                    f"⏱️ 本次直播时长：{duration_str}"
                )
                await self.push_simple(msg, "", stream_ids)
                
                user_hist['live_start_time'] = 0
                has_event = True

            if current_status != last_status:
                user_hist['live_status'] = current_status
                self.history[uid] = user_hist
                await BiliUtils.save_history(self.history)

            return has_event

        except Exception:
            return False

    async def push_simple(self, text: str, image_url: str, group_ids: List[int]):
        b64 = None
        if image_url:
            b64 = await BiliUtils.url_to_base64(image_url, self.session)
        
        for gid in group_ids:
            message_chain = [{"type": "text", "data": {"text": text}}]
            if b64:
                message_chain.append({"type": "text", "data": {"text": "\n"}})
                message_chain.append({"type": "image", "data": {"file": f"base64://{b64}"}})
            
            try:
                await self.ctx.api.call(
                    "adapter.napcat.message.send_msg",
                    params={
                        "message_type": "group",
                        "group_id": gid,
                        "message": message_chain
                    }
                )
            except Exception as e:
                self.ctx.logger.error(f"发送普通消息失败: {e}")

    async def process_and_push(self, item: Dict, group_ids: List[int], max_imgs: int):
        parsed = self.parse_dynamic(item)
        if not parsed:
            return

        author = parsed.get('author', 'UP主')
        pub_ts = parsed.get('pub_ts', 0)
        try:
            pub_ts = int(pub_ts) if pub_ts else 0
        except (ValueError, TypeError):
            pub_ts = 0

        if pub_ts > 0:
            try:
                pub_time_str = datetime.fromtimestamp(pub_ts).strftime('%Y-%m-%d %H:%M:%S')
                time_line = f"🕒 发布时间: {pub_time_str}\n"
            except Exception as e:
                self.ctx.logger.warning(f"格式化发布时间失败: {e}, pub_ts={pub_ts}")
                time_line = ""
        else:
            time_line = ""
        
        text = f"📢 【{author}】发布了新动态！\n{time_line}{parsed['text']}\n🔗 链接: {parsed['url']}"

        images = parsed['images'][:9] 
        
        cached_b64s = []
        for img_url in images:
            b64 = await BiliUtils.url_to_base64(img_url, self.session)
            if b64:
                cached_b64s.append(b64)
        
        num_imgs = len(cached_b64s)

        if num_imgs > max_imgs:
            bot_name = author
            bot_uin = "10000"
            forward_nodes = []
            for b64 in cached_b64s:
                forward_nodes.append({
                    "type": "node",
                    "data": {
                        "name": bot_name,
                        "uin": bot_uin,
                        "content": [{"type": "image", "data": {"file": f"base64://{b64}"}}]
                    }
                })

            for gid in group_ids:
                try:
                    await self.ctx.api.call(
                        "adapter.napcat.message.send_msg",
                        params={
                            "message_type": "group",
                            "group_id": gid,
                            "message": [{"type": "text", "data": {"text": text}}]
                        }
                    )
                    await self.ctx.api.call(
                        "adapter.napcat.message.send_group_forward_msg",
                        params={
                            "group_id": gid,
                            "message": forward_nodes
                        }
                    )
                    await asyncio.sleep(0.5)
                except Exception as e:
                    self.ctx.logger.error(f"发送合并转发(仅图片)失败: {e}")
        else:
            message_chain = [{"type": "text", "data": {"text": text + "\n"}}]
            for b64 in cached_b64s:
                message_chain.append({"type": "image", "data": {"file": f"base64://{b64}"}})
            
            for gid in group_ids:
                try:
                    await self.ctx.api.call(
                        "adapter.napcat.message.send_msg",
                        params={
                            "message_type": "group",
                            "group_id": gid,
                            "message": message_chain
                        }
                    )
                    await asyncio.sleep(0.5)
                except Exception as e:
                    self.ctx.logger.error(f"发送同气泡图文失败: {e}")

    def _extract_major_data(self, module_dynamic: Dict) -> Tuple[str, List[str]]:
        text = ""
        images = []
        major = module_dynamic.get('major') or {}
        major_type = major.get('type')

        if major_type in ['MAJOR_TYPE_OPUS', 'MAJOR_TYPE_ARTICLE']:
            opus = major.get('opus') or {}
            text = opus.get('summary', {}).get('text', '')
            if not text:
                text = opus.get('title', '')
            pics = opus.get('pics', [])
            images = [p.get('url') for p in pics]
        
        elif major_type == 'MAJOR_TYPE_DRAW':
            items = major.get('draw', {}).get('items', [])
            images = [i.get('src') for i in items]
            
        elif major_type in ['MAJOR_TYPE_ARCHIVE', 'MAJOR_TYPE_VIDEO']:
            video_data = major.get('archive') or major.get('video') or {}
            title = video_data.get('title', '视频投稿')
            desc = video_data.get('desc', '')
            cover = video_data.get('cover', '')
            text = f"📺 {title}\n{desc}"
            if cover:
                images.append(cover)
            
        return text, images

    def parse_dynamic(self, item: Dict) -> Optional[Dict]:
        try:
            id_str = item.get('id_str')
            modules = item.get('modules') or {}
            module_dynamic = modules.get('module_dynamic') or {}
            module_author = modules.get('module_author') or {}
            
            main_text, main_images = self._extract_major_data(module_dynamic)
            desc_text = (module_dynamic.get('desc') or {}).get('text', '')

            ignore_lottery = self.config.settings.ignore_lottery if self.config else True
            
            if ignore_lottery:
                full_text_for_check = f"{desc_text}\n{main_text}"
                if re.search(r'恭喜@.*?中奖.*?详情请点击.*?查看', full_text_for_check, re.DOTALL):
                    self.ctx.logger.info(f"🛑 拦截到开奖通知动态 (ID: {id_str})，已丢弃，不进行推送。")
                    return None

            raw_pub_ts = module_author.get('pub_ts', 0)
            try:
                pub_ts = int(raw_pub_ts) if raw_pub_ts else 0
            except (ValueError, TypeError):
                pub_ts = 0

            result = {
                "type": "unknown",
                "text": "",
                "images": [], 
                "url": f"https://t.bilibili.com/{id_str}",
                "author": module_author.get('name', 'UP主'),
                "pub_ts": pub_ts,
            }

            if desc_text:
                result['text'] += desc_text
            if main_text:
                result['text'] += f"\n{main_text}"
            result['images'].extend(main_images)

            if item.get('type') == 'DYNAMIC_TYPE_FORWARD':
                orig = item.get('orig') or {}
                if orig.get('type') == 'DYNAMIC_TYPE_NONE':
                    result['text'] += "\n\n[原动态已被删除]"
                else:
                    orig_modules = orig.get('modules') or {}
                    orig_author = (orig_modules.get('module_author') or {}).get('name', '未知用户')
                    orig_dynamic = orig_modules.get('module_dynamic') or {}
                    
                    orig_desc = (orig_dynamic.get('desc') or {}).get('text', '')
                    orig_major_text, orig_major_images = self._extract_major_data(orig_dynamic)
                    
                    result['text'] += f"\n\n🔁 转发 @{orig_author}:"
                    if orig_desc:
                        result['text'] += f"\n{orig_desc}"
                    if orig_major_text:
                        result['text'] += f"\n{orig_major_text}"
                    result['images'].extend(orig_major_images)

            return result
        except Exception as e:
            self.ctx.logger.error(f"解析出错: {e}")
            return None

monitor_instance = BiliMonitor()

# ================= 5. 插件注册入口 =================
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
        pattern=r"^/B动态\s+(?P<action>start|stop|status|test|info|add|remove|list|help)(?:\s+(?P<arg>.*))?\s*$"
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
                        "message": [{"type": "text", "data": {"text": text}}]
                    }
                )
            except Exception as e:
                self.ctx.logger.error(f"群消息反馈失败: {e}")

        action = matched_groups.get("action") if matched_groups else None
        arg = matched_groups.get("arg").strip() if matched_groups and matched_groups.get("arg") else None

        # /B动态 start
        if action == "start":
            if monitor_instance.running:
                await reply_group("⚠️ B站监控已在运行中，无需重复启动。")
            else:
                await monitor_instance.start(self.ctx, self.config)
                await reply_group("✅ B站监控已成功启动。")
            return True, None, True

        # /B动态 stop
        elif action == "stop":
            await monitor_instance.stop()
            await reply_group("🛑 B站监控已停止运行。")
            return True, None, True
        
        elif action == "status":
            st = "🟢 运行中" if monitor_instance.running else "🔴 已停止"
            cnt = len(monitor_instance.uid_to_stream_ids)
            msg = f"📊 B站监控状态: {st}\n当前共监控 {cnt} 个 B站 UID。"
            await reply_group(msg)
            return True, None, True

        # /B动态 info
        elif action == "info":
            if not arg:
                await reply_group("❌ 用法错误: /B动态 info <uid>")
                return True, None, True
            try:
                u = user.User(int(arg), credential=monitor_instance.credential)
                raw_info = await u.get_live_info()
                
                live_room = raw_info.get('live_room', {})
                status = live_room.get('liveStatus', 0)
                uname = raw_info.get('name', '未知')
                
                if status == 1:
                    user_hist = monitor_instance.history.get(arg, {})
                    start_time = user_hist.get('live_start_time', 0) if isinstance(user_hist, dict) else 0
                    
                    duration_text = ""
                    if start_time:
                        sec = time.time() - start_time
                        duration_text = f"\n⏱️ 已直播: {BiliUtils.format_duration(sec)}"

                    msg = (
                        f"🟢 【{uname}】正在直播中！\n"
                        f"📺 {live_room.get('title')}\n"
                        f"🔗 {live_room.get('url')}"
                        f"{duration_text}"
                    )
                    cover = live_room.get('cover', '')
                    await monitor_instance.push_simple(msg, cover, [int(group_id)])
                    return True, "✅ 直播状态已推送到当前群聊。", True
                else:
                    return True, f"⚪ 状态查询结果：【{uname}】未开播。", True
            except Exception as e:
                return True, f"❌ 查询失败: {e}", True

        # /B动态 test
        elif action == "test":
            if not arg:
                return True, "❌ 用法错误: /B动态 test <uid>", True
            
            try:
                u = user.User(int(arg), credential=monitor_instance.credential)
                dyn = await u.get_dynamics_new()
                items = dyn.get('items', [])
                if not items: 
                    return True, "⚠️ 该 UID 暂无动态", True

                item_to_push = None
                for it in items:
                    if it.get('type') == 'DYNAMIC_TYPE_LIVE_RCMD':
                        continue
                    try:
                        major_type = (it.get('modules', {})
                                    .get('module_dynamic', {})
                                    .get('major', {}) or {}).get('type')
                        if major_type == 'MAJOR_TYPE_LIVE_RCMD':
                            continue
                    except Exception:
                        pass
                    if monitor_instance._is_top_dynamic(it):
                        continue
                    item_to_push = it
                    break

                if not item_to_push:
                    return True, "⚠️ 该 UID 除置顶外暂无可推送的普通动态", True

                current_max_imgs = self.config.settings.max_images
                await monitor_instance.process_and_push(item_to_push, [int(group_id)], current_max_imgs)
                return True, "✅ 测试推送已成功发送到群聊", True
            except Exception as e: 
                return True, f"❌ 推送错误: {e}", True

        # /B动态 add
        elif action == "add":
            if not arg or not arg.isdigit():
                await reply_group("❌ 参数错误！请提供正确的纯数字UID。\n用法: /B动态 add <UID>")
                return True, None, True

            uid = str(arg)
            gid = int(group_id)

            uname = await fetch_uname(uid, monitor_instance.credential)
            if uname:
                await sub_manager.set_name(uid, uname)
            display = f"{uname}（UID:{uid}）" if uname else f"UID:{uid}"

            async with sub_manager.lock:
                if uid not in sub_manager.data["custom"]:
                    sub_manager.data["custom"][uid] = []
                if gid not in sub_manager.data["custom"][uid]:
                    sub_manager.data["custom"][uid].append(gid)

            await sub_manager.save()
            await monitor_instance.update_subscription_map()
            await reply_group(f"✅ 已成功订阅 {display} 的动态！")
            return True, None, True

        # /B动态 remove
        elif action == "remove":
            if not arg or not arg.isdigit():
                await reply_group("❌ 参数错误！请提供正确的数字UID。\n用法: /B动态 remove <UID>")
                return True, None, True
            
            uid = str(arg)
            gid = int(group_id)
            need_save = False
            
            static_groups = sub_manager.data["static"].get(uid, [])
            if gid in static_groups:
                await reply_group("⚠️ 无法移除！\n该UID是在 config 配置文件中固定订阅的。")
                return True, None, True

            async with sub_manager.lock:
                custom_groups = sub_manager.data["custom"].get(uid, [])
                if gid in custom_groups:
                    sub_manager.data["custom"][uid].remove(gid)
                    if not sub_manager.data["custom"][uid]:
                        del sub_manager.data["custom"][uid]
                    need_save = True
                else:
                    await reply_group("⚪ 当前群聊并没有通过指令订阅过此UID，无需移除。")
                    return True, None, True

            if need_save:
                await sub_manager.save()
                await monitor_instance.update_subscription_map()
                await reply_group("🗑️ 已成功将此UID 从当前群聊的动态订阅中移除。")
                return True, None, True

        # /B动态 list
        elif action == "list":
            gid = int(group_id)
            static_list, custom_list = [], []

            for uid, groups in sub_manager.data["static"].items():
                if gid in groups: static_list.append(uid)
            for uid, groups in sub_manager.data["custom"].items():
                if gid in groups: custom_list.append(uid)

            if not static_list and not custom_list:
                await reply_group("📭 当前群聊暂无任何B站订阅。")
                return True, None, True

            missing = [u for u in static_list + custom_list if not sub_manager.get_name(u)]
            for uid in missing:
                uname = await fetch_uname(uid, monitor_instance.credential)
                if uname:
                    await sub_manager.set_name(uid, uname)
                await asyncio.sleep(0.3)  # 避免风控

            def fmt(uid: str) -> str:
                name = sub_manager.get_name(uid) or "未知UP主"
                return f"{name} (UID:{uid})"

            msg = "📋 【当前群聊订阅列表】"
            if static_list:
                msg += f"\n[固定配置] ({len(static_list)}个):\n- " + "\n- ".join(fmt(u) for u in static_list)
            if custom_list:
                msg += f"\n\n[动态添加] ({len(custom_list)}个):\n- " + "\n- ".join(fmt(u) for u in custom_list)
            msg += "\n\n💡 使用 /B动态 remove [UID] 仅可移除[动态添加]的订阅。"

            await reply_group(msg)
            return True, None, True

        # /B动态 help
        elif action == "help":
            help_text = (
                "🛠️ Bilibili 订阅管理指令\n"
                "------------------\n"
                "➕ /B动态 add [UID]\n   添加当前群聊对该 UID 的订阅\n"
                "➖ /B动态 remove [UID]\n   移除当前群聊的动态订阅\n"
                "📋 /B动态 list\n   列出当前群组的所有订阅源\n"
                "🔍 /B动态 info [UID]\n   查询该 UID 实时直播状态\n"
                "🧪 /B动态 test [UID]\n   触发一次动态推送测试\n"
                "------------------\n"
                "⚠️ 仅管理员可用，固定订阅需改后台 Config"
            )
            await reply_group(help_text)  
            return True, None, True       

        return True, f"❌ 未知指令: {action}。发送 /B动态 help 查看帮助。", True

# ================= 5. 工厂函数入口 =================
def create_plugin():
    return BiliPlugin()
