import asyncio
import json
import base64
import aiohttp
import os
import time
import random
import re
from datetime import datetime
from urllib.parse import unquote
from typing import Dict, Any, List, Optional, Tuple, Type

from src.common.logger import get_logger

# 引入基础组件
from src.plugin_system import (
    BasePlugin,
    register_plugin,
    BaseCommand,
    ComponentInfo,
    ConfigField,
)
from src.plugin_system.apis import send_api, chat_api
from bilibili_api import user, Credential

logger = get_logger("bilibili_dynamic_push")

# 1. 辅助工具类
class BiliUtils:
    @staticmethod
    async def url_to_base64(url: str) -> Optional[str]:
        if not url: return None
        try:
            async with aiohttp.ClientSession() as session:
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
        except: pass
        return {}

    @staticmethod
    def save_history(data: Dict[str, Any]):
        try:
            with open(BiliUtils.get_history_path(), 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
        except: pass
    
    @staticmethod
    def format_duration(seconds: float) -> str:
        # 格式化秒数为 时:分:秒
        m, s = divmod(int(seconds), 60)
        h, m = divmod(m, 60)
        if h > 0:
            return f"{h}小时{m}分{s}秒"
        else:
            return f"{m}分{s}秒"

# 2. 核心监控逻辑
class BiliMonitor:
    def __init__(self):
        self.running = False
        self.history = BiliUtils.load_history()
        self.credential = None
        self._tasks = []
        self.config_getter = None  # 用于存储配置获取器

    async def start(self, config_getter):
        if self.running: return
        self.running = True
        self.config_getter = config_getter  # 启动时保存配置获取器
        logger.info("启动 Bilibili 监控任务...")
        
        cred_dict = config_getter("settings.credential")
        if cred_dict and isinstance(cred_dict, dict):
            # 自动解码 Cookie 值
            valid_cred = {}
            for k, v in cred_dict.items():
                if v:
                    if isinstance(v, str) and '%' in v:
                        try:
                            decoded_v = unquote(v)
                            valid_cred[k] = decoded_v
                        except:
                            valid_cred[k] = v
                    else:
                        valid_cred[k] = v

            if valid_cred:
                try:
                    self.credential = Credential(**valid_cred)
                    logger.info("✅ B站凭证加载成功 (已自动解码)")
                except Exception as e: 
                    logger.error(f"❌ 凭证加载失败: {e}")
        
        self._tasks.append(asyncio.create_task(self.loop(config_getter)))
        self._tasks.append(asyncio.create_task(self.refresh_credential_loop()))

    async def stop(self):
        self.running = False
        for task in self._tasks:
            task.cancel()
            try: await task
            except: pass
        self._tasks = []
        logger.info("🛑 Bilibili 监控停止")

    async def refresh_credential_loop(self):
        while self.running:
            await asyncio.sleep(3600 * 6)
            if self.credential:
                try:
                    if await self.credential.check_refresh():
                        await self.credential.refresh()
                        logger.info("🔄 B站凭据已自动刷新")
                except Exception as e:
                    logger.error(f"凭据刷新失败: {e}")

    async def loop(self, config_getter):
        logger.info("开始轮询...")
        while self.running:
            try:
                if not config_getter("plugin.enabled"):
                    await asyncio.sleep(10)
                    continue

                subs = config_getter("subscriptions.users")
                base_interval = config_getter("settings.poll_interval") or 120
                jitter = config_getter("settings.poll_jitter") or 0
                max_imgs = config_getter("settings.max_images") or 3

                if not subs:
                    await asyncio.sleep(base_interval)
                    continue

                actual_interval = base_interval
                if jitter > 0:
                    min_time = max(5, base_interval - jitter)
                    max_time = base_interval + jitter
                    actual_interval = random.randint(min_time, max_time)

                logger.info(f"🔄 检测中... (下次检测将在 {actual_interval}s 后)")

                # 预处理：合并同一 UID 的所有目标群组
                uid_to_stream_ids = {}

                for sub in subs:
                    raw_groups = sub.get("groups", [])
                    if not raw_groups: continue
                    
                    current_entry_stream_ids = set()
                    for gid in raw_groups:
                        gid_str = str(gid)
                        stream_obj = chat_api.get_stream_by_group_id(gid_str, platform="qq")
                        if stream_obj: 
                            current_entry_stream_ids.add(stream_obj.stream_id)
                        else: 
                            current_entry_stream_ids.add(gid_str)

                    if not current_entry_stream_ids: continue

                    target_uids = []
                    if "uid" in sub and sub["uid"]: 
                        target_uids.append(str(sub["uid"]))
                    if "uids" in sub and isinstance(sub["uids"], list):
                        target_uids.extend([str(x) for x in sub["uids"]])
                    
                    for uid in set(target_uids):
                        if not uid: continue
                        if uid not in uid_to_stream_ids:
                            uid_to_stream_ids[uid] = set()
                        uid_to_stream_ids[uid].update(current_entry_stream_ids)

                # 开始遍历去重后的 UID 进行检测
                for uid, stream_ids_set in uid_to_stream_ids.items():
                    target_stream_ids = list(stream_ids_set)
                    if not target_stream_ids: continue
                    
                    await self.check_dynamic(uid, target_stream_ids, max_imgs)
                    await self.check_live(uid, target_stream_ids)
                    
                    await asyncio.sleep(1)

                await asyncio.sleep(actual_interval)
            except Exception as e:
                logger.error(f"❌ 轮询错误: {e}")
                await asyncio.sleep(60)

    async def check_dynamic(self, uid: str, stream_ids: List[str], max_imgs: int):
        try:
            u = user.User(int(uid), credential=self.credential)
            dynamics = await u.get_dynamics_new()
            items = dynamics.get('items', [])
            if not items: return

            user_hist = self.history.get(uid, {})
            if isinstance(user_hist, str): user_hist = {'dyn_id': user_hist}
            
            last_saved_id = user_hist.get('dyn_id')
            
            if not last_saved_id:
                latest_id = str(items[0]['id_str']) 
                for item in items:
                    if int(item['id_str']) > int(latest_id):
                        latest_id = str(item['id_str'])
                
                logger.info(f"UID {uid} 首次初始化动态，基准ID: {latest_id}")
                user_hist['dyn_id'] = latest_id
                self.history[uid] = user_hist
                BiliUtils.save_history(self.history)
                return

            new_items = []
            for item in items:
                curr_id = str(item['id_str'])
                
                # 过滤B站自动生成的直播动态
                # 1. 检查一级类型
                if item.get('type') == 'DYNAMIC_TYPE_LIVE_RCMD':
                    continue
                
                # 2. 检查二级类型 (Major Type)
                try:
                    major_type = item.get('modules', {}).get('module_dynamic', {}).get('major', {}).get('type')
                    if major_type == 'MAJOR_TYPE_LIVE_RCMD':
                        continue
                except: pass

                is_top = False
                try:
                    if item.get('modules', {}).get('module_tag', {}).get('text') == '置顶': is_top = True
                except: pass
                
                if int(curr_id) > int(last_saved_id):
                    new_items.append(item)
                else:
                    if not is_top: break
            
            if not new_items: return

            latest_item_to_push = new_items[0]
            latest_id_str = str(latest_item_to_push['id_str'])

            logger.info(f"🎉 UID {uid} 发现新动态: {latest_id_str} (推送给 {len(stream_ids)} 个群)")
            
            await self.process_and_push(latest_item_to_push, stream_ids, max_imgs)
            
            user_hist['dyn_id'] = latest_id_str
            self.history[uid] = user_hist
            BiliUtils.save_history(self.history)

        except Exception as e:
            logger.error(f"UID {uid} 动态检查失败: {e}")

    async def check_live(self, uid: str, stream_ids: List[str]):
        try:
            u = user.User(int(uid), credential=self.credential)
            raw_info = await u.get_live_info()
            
            # 解析嵌套数据结构
            live_room = raw_info.get('live_room', {})
            current_status = live_room.get('liveStatus', 0)
            room_title = live_room.get('title', '直播间')
            url = live_room.get('url', '')
            cover = live_room.get('cover', '') 
            uname = raw_info.get('name', 'UP主')

            user_hist = self.history.get(uid, {})
            if isinstance(user_hist, str): user_hist = {'dyn_id': user_hist}
            
            last_status = user_hist.get('live_status', 0)
            start_time = user_hist.get('live_start_time', 0)

            if 'live_status' not in user_hist:
                user_hist['live_status'] = current_status
                if current_status == 1:
                    user_hist['live_start_time'] = time.time()
                self.history[uid] = user_hist
                BiliUtils.save_history(self.history)
                return

            # 开播 
            if current_status == 1 and last_status == 0:
                logger.info(f"UID {uid} 开播")
                current_time = time.time()
                
                msg = (
                    f"🔴 【{uname}】开播了！\n"
                    f"📺 标题：{room_title}\n"
                    f"🔗 传送门：{url}\n"
                    f"⏰ 时间：{datetime.now().strftime('%H:%M:%S')}"
                )
                await self.push_simple(msg, cover, stream_ids)
                user_hist['live_start_time'] = current_time
            
            # 下播 
            elif current_status == 0 and last_status == 1:
                logger.info(f"UID {uid} 下播")
                
                duration_str = "未知"
                if start_time:
                    duration_sec = time.time() - start_time
                    duration_str = BiliUtils.format_duration(duration_sec)
                
                msg = (
                    f"🏁 【{uname}】下播了~\n"
                    f"⏱️ 本次直播时长：{duration_str}"
                )
                for sid in stream_ids: 
                    await send_api.text_to_stream(text=msg, stream_id=sid)
                
                user_hist['live_start_time'] = 0

            if current_status != last_status:
                user_hist['live_status'] = current_status
                self.history[uid] = user_hist
                BiliUtils.save_history(self.history)

        except Exception as e:
            pass

    async def push_simple(self, text: str, image_url: str, stream_ids: List[str]):
        b64 = None
        if image_url:
            b64 = await BiliUtils.url_to_base64(image_url)
        
        for sid in stream_ids:
            await send_api.text_to_stream(text=text, stream_id=sid)
            if b64:
                await send_api.image_to_stream(image_base64=b64, stream_id=sid)

    async def process_and_push(self, item: Dict, stream_ids: List[str], max_imgs: int):
        parsed = self.parse_dynamic(item)
        if not parsed: return

        author = parsed.get('author', 'UP主')
        text = f"📢 【{author}】发布了新动态！\n\n{parsed['text']}\n🔗 链接: {parsed['url']}"

        images = parsed['images']
        
        if len(images) > max_imgs:
            text += f"\n\n⚠️ 动态图片过多，共【{len(images)}】张，请点击链接去原动态查看图片。"
            images = []
        
        for sid in stream_ids:
            await send_api.text_to_stream(text=text, stream_id=sid)

        for img_url in images:
            b64 = await BiliUtils.url_to_base64(img_url)
            if b64:
                for sid in stream_ids:
                    await send_api.image_to_stream(image_base64=b64, stream_id=sid)
                    await asyncio.sleep(0.5)

    def _extract_major_data(self, module_dynamic: Dict) -> Tuple[str, List[str]]:
        text = ""
        images = []
        major = module_dynamic.get('major') or {}
        major_type = major.get('type')

        if major_type in ['MAJOR_TYPE_OPUS', 'MAJOR_TYPE_ARTICLE']:
            opus = major.get('opus') or {}
            text = opus.get('summary', {}).get('text', '')
            if not text: text = opus.get('title', '')
            pics = opus.get('pics', [])
            images = [p.get('url') for p in pics]
        
        elif major_type == 'MAJOR_TYPE_DRAW':
            items = major.get('draw', {}).get('items', [])
            images = [i.get('src') for i in items]
            
        elif major_type == 'MAJOR_TYPE_ARCHIVE':
            archive = major.get('archive') or {}
            title = archive.get('title', '视频')
            desc = archive.get('desc', '')
            cover = archive.get('cover', '')
            text = f"📺 {title}\n{desc}"
            if cover: images.append(cover)
            
        return text, images

    def parse_dynamic(self, item: Dict) -> Optional[Dict]:
        try:
            id_str = item.get('id_str')
            modules = item.get('modules') or {}
            module_dynamic = modules.get('module_dynamic') or {}
            module_author = modules.get('module_author') or {}
            
            main_text, main_images = self._extract_major_data(module_dynamic)
            desc_text = (module_dynamic.get('desc') or {}).get('text', '')

            # 拦截中奖动态
            ignore_lottery = True  # 默认开启拦截
            if self.config_getter:
                val = self.config_getter("settings.ignore_lottery")
                if val is not None:
                    ignore_lottery = val
            
            if ignore_lottery:
                full_text_for_check = f"{desc_text}\n{main_text}"
                if re.search(r'恭喜@.*?中奖.*?详情请点击.*?查看', full_text_for_check, re.DOTALL):
                    logger.info(f"🛑 拦截到开奖通知动态 (ID: {id_str})，已丢弃，不进行推送。")
                    return None

            result = {
                "type": "unknown", "text": "", "images": [], 
                "url": f"https://t.bilibili.com/{id_str}",
                "author": module_author.get('name', 'UP主')
            }

            if desc_text: result['text'] += desc_text
            if main_text: result['text'] += f"\n{main_text}"
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
                    if orig_desc: result['text'] += f"\n{orig_desc}"
                    if orig_major_text: result['text'] += f"\n{orig_major_text}"
                    result['images'].extend(orig_major_images)

            return result
        except Exception as e:
            logger.error(f"解析出错: {e}")
            return None

monitor_instance = BiliMonitor()

# 3. 交互指令
class BiliCommand(BaseCommand):
    command_name = "bili_control"
    command_description = "B站订阅控制"
    command_pattern = r"^/bili_control\s+(?P<action>start|stop|status|test|info)(?:\s+(?P<arg>\S+))?$"

    async def execute(self) -> Tuple[bool, str, bool]:
        # ID 获取与管理员权限鉴定
        try:
            current_user = None
            if hasattr(self.message, 'sender_id'):
                current_user = str(self.message.sender_id)
            elif hasattr(self.message, 'user_id'):
                current_user = str(self.message.user_id)
            elif hasattr(self.message, 'uid'):
                current_user = str(self.message.uid)
            elif hasattr(self.message, 'message_info'):
                 info = self.message.message_info
                 if hasattr(info, 'user_info') and hasattr(info.user_info, 'user_id'):
                     current_user = str(info.user_info.user_id)
            
            if current_user is None:
                logger.error(f"❌ 无法获取发送者ID，Message对象属性: {dir(self.message)}")
                return False, "", True

            admin_list = self.get_config("settings.admin_qqs") or []
            admin_list = [str(x) for x in admin_list]

            if current_user not in admin_list:
                return False, "", True

        except Exception as e:
            logger.error(f"❌ 鉴权逻辑发生未知错误: {e}")
            return False, "", True

        action = self.matched_groups.get("action")
        arg = self.matched_groups.get("arg")
        def getter(k): return self.get_config(k)

        if action == "start":
            if monitor_instance.running: await self.send_text("⚠️ 已在运行")
            else:
                await monitor_instance.start(getter)
                await self.send_text("✅ 已启动")
        
        elif action == "stop":
            await monitor_instance.stop()
            await self.send_text("🛑 已停止")
        
        elif action == "status":
            st = "🟢" if monitor_instance.running else "🔴"
            subs = self.get_config("subscriptions.users") or []
            cnt = len(subs)
            await self.send_text(f"📊 状态:{st} | 订阅数:{cnt}")

        elif action == "info":
            if not arg:
                await self.send_text("❌ 用法: /bili_control info <uid>")
            else:
                try:
                    await self.send_text(f"🔍 正在查询 UID {arg} ...")
                    u = user.User(int(arg), credential=monitor_instance.credential)
                    raw_info = await u.get_live_info()
                    
                    # 正确解析嵌套数据
                    live_room = raw_info.get('live_room', {})
                    status = live_room.get('liveStatus', 0)
                    uname = raw_info.get('name', '未知')
                    
                    if status == 1:
                        user_hist = monitor_instance.history.get(arg, {})
                        if isinstance(user_hist, dict):
                            start_time = user_hist.get('live_start_time', 0)
                        else: start_time = 0
                        
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
                        await monitor_instance.push_simple(msg, cover, [self.message.chat_stream.stream_id])
                    else:
                        await self.send_text(f"⚪ 【{uname}】未开播。")
                except Exception as e:
                    await self.send_text(f"❌ 查询失败: {e}")

        elif action == "test":
            if not arg:
                await self.send_text("❌ 用法: /bili_control test <uid>")
                return False, "", True
            await self.send_text(f"🧪 测试动态推送 UID {arg}...")
            try:
                u = user.User(int(arg), credential=monitor_instance.credential)
                dyn = await u.get_dynamics_new()
                items = dyn.get('items', [])
                if not items: 
                    await self.send_text("无动态")
                else:
                    item_to_push = items[0]
                    sid = None
                    try: sid = self.message.chat_stream.stream_id
                    except: pass
                    if sid:
                        await monitor_instance.process_and_push(item_to_push, [sid], 9)
                        await self.send_text("✅ 测试推送已发送")
            except Exception as e: await self.send_text(f"❌ 错误: {e}")

        return True, "done", True

@register_plugin
class BiliPlugin(BasePlugin):
    plugin_name = "bilibili_dynamic_push"
    enable_plugin = True
    dependencies = []
    python_dependencies = ["bilibili_api", "aiohttp"]
    config_file_name = "config.toml"
    config_section_descriptions = {
        "plugin": "插件开关", "settings": "设置", "subscriptions": "订阅"
    }
    config_schema = {
        "plugin": {"enabled": ConfigField(bool, True, "启用")},
        "settings": {
            "poll_interval": ConfigField(int, 120, "轮询基准秒数"),
            "poll_jitter": ConfigField(int, 10, "轮询抖动秒数(实际=基准±抖动)"),
            "admin_qqs": ConfigField(list, [], "管理员QQ列表"),
            "credential": ConfigField(dict, {}, "Cookie"),
            "max_images": ConfigField(int, 3, "最大图片数"),
            "ignore_lottery": ConfigField(bool, True, "自动丢弃开奖动态")
        },
        "subscriptions": {
            "users": ConfigField(list, [
                {"uid": "114514", "groups": ["1919810"]}
            ], "订阅列表")
        }
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        asyncio.create_task(self._auto_start())

    async def _auto_start(self):
        await asyncio.sleep(5)
        if self.get_config("plugin.enabled"):
            def getter(k): return self.get_config(k)
            await monitor_instance.start(getter)

    def get_plugin_components(self) -> List[Tuple[ComponentInfo, Type]]:
        return [
            (BiliCommand.get_command_info(), BiliCommand)
        ]
