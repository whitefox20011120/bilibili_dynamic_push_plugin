import asyncio
import json
import base64
import aiohttp
import os
import time
import random  # [新增] 用于计算随机抖动
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
from bilibili_api import user, Credential, live

logger = get_logger("bilibili_monitor")

# ====================
# 1. 辅助工具类
# ====================
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

# ====================
# 2. 核心监控逻辑
# ====================
class BiliMonitor:
    def __init__(self):
        self.running = False
        self.history = BiliUtils.load_history()
        self.credential = None
        self._tasks = []

    async def start(self, config_getter):
        if self.running: return
        self.running = True
        logger.info("启动 Bilibili 监控任务...")
        
        cred_dict = config_getter("settings.credential")
        if cred_dict and isinstance(cred_dict, dict):
            valid_cred = {k: v for k, v in cred_dict.items() if v}
            if valid_cred:
                try:
                    self.credential = Credential(**valid_cred)
                    logger.info("✅ B站凭证加载成功")
                except: pass
        
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
                jitter = config_getter("settings.poll_jitter") or 0  # [新增] 获取抖动配置
                max_imgs = config_getter("settings.max_images") or 3

                if not subs:
                    await asyncio.sleep(base_interval)
                    continue

                # [新增] 计算包含抖动的实际休眠时间
                actual_interval = base_interval
                if jitter > 0:
                    # 确保最小时间不小于5秒，防止负数或过频
                    min_time = max(5, base_interval - jitter)
                    max_time = base_interval + jitter
                    actual_interval = random.randint(min_time, max_time)

                logger.info(f"🔄 检测中... (下次检测将在 {actual_interval}s 后, 基准{base_interval}±{jitter}s)")

                for sub in subs:
                    raw_groups = sub.get("groups", [])
                    if not raw_groups: continue
                    
                    target_stream_ids = []
                    for gid in raw_groups:
                        gid_str = str(gid)
                        stream_obj = chat_api.get_stream_by_group_id(gid_str, platform="qq")
                        if stream_obj: target_stream_ids.append(stream_obj.stream_id)
                        else: target_stream_ids.append(gid_str)

                    if not target_stream_ids: continue

                    target_uids = []
                    if "uid" in sub and sub["uid"]: target_uids.append(str(sub["uid"]))
                    if "uids" in sub and isinstance(sub["uids"], list):
                        target_uids.extend([str(x) for x in sub["uids"]])
                    
                    for uid in set(target_uids):
                        if not uid: continue
                        await self.check_dynamic(uid, target_stream_ids, max_imgs)
                        await self.check_live(uid, target_stream_ids)

                # [修改] 使用计算后的随机时间进行休眠
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

            # 获取该UID的历史记录
            user_hist = self.history.get(uid, {})
            if isinstance(user_hist, str): user_hist = {'dyn_id': user_hist}
            
            last_saved_id = user_hist.get('dyn_id')
            
            # === 场景1：全新订阅（history无记录） ===
            if not last_saved_id:
                latest_id = str(items[0]['id_str']) 
                for item in items:
                    if int(item['id_str']) > int(latest_id):
                        latest_id = str(item['id_str'])
                        
                logger.info(f"UID {uid} 首次初始化，基准ID: {latest_id}")
                user_hist['dyn_id'] = latest_id
                self.history[uid] = user_hist
                BiliUtils.save_history(self.history)
                return

            # === 场景2：已有记录 ===
            # 筛选出所有比历史记录新的动态
            new_items = []
            for item in items:
                curr_id = str(item['id_str'])
                
                # 跳过置顶判断 (可选)
                is_top = False
                try:
                    if item.get('modules', {}).get('module_tag', {}).get('text') == '置顶': is_top = True
                except: pass
                
                if int(curr_id) > int(last_saved_id):
                    new_items.append(item)
                else:
                    # 如果遇到旧动态且不是置顶，说明后面都是旧的了，停止遍历
                    if not is_top: break
            
            if not new_items: return

            # === 核心修改：只推送最新的一条，但更新到最新ID ===
            # new_items[0] 就是最新的一条（API返回顺序通常是新->旧）
            latest_item_to_push = new_items[0]
            latest_id_str = str(latest_item_to_push['id_str'])

            logger.info(f"🎉 UID {uid} 发现 {len(new_items)} 条新动态，仅推送最新一条: {latest_id_str}")
            
            await self.process_and_push(latest_item_to_push, stream_ids, max_imgs)
            
            # 更新历史记录为最新那条的ID
            user_hist['dyn_id'] = latest_id_str
            self.history[uid] = user_hist
            BiliUtils.save_history(self.history)

        except Exception as e:
            logger.error(f"UID {uid} 动态检查失败: {e}")

    async def check_live(self, uid: str, stream_ids: List[str]):
        try:
            u = user.User(int(uid), credential=self.credential)
            live_info = await u.get_live_info()
            
            current_status = live_info.get('liveStatus', 0)
            room_title = live_info.get('title', '直播间')
            url = live_info.get('url', '')
            cover = live_info.get('cover', '')
            uname = live_info.get('username', 'UP主')

            user_hist = self.history.get(uid, {})
            if isinstance(user_hist, str): user_hist = {'dyn_id': user_hist}
            last_status = user_hist.get('live_status', 0)

            if 'live_status' not in user_hist:
                user_hist['live_status'] = current_status
                self.history[uid] = user_hist
                BiliUtils.save_history(self.history)
                return

            if current_status == 1 and last_status == 0:
                logger.info(f"UID {uid} 开播")
                msg = f"🔴 【{uname}】开播了！\n\n📺 {room_title}\n🔗 {url}"
                await self.push_simple(msg, cover, stream_ids)
            
            elif current_status == 0 and last_status == 1:
                logger.info(f"UID {uid} 下播")
                msg = f"🏁 【{uname}】下播了。"
                for sid in stream_ids: 
                    await send_api.text_to_stream(text=msg, stream_id=sid)

            if current_status != last_status:
                user_hist['live_status'] = current_status
                self.history[uid] = user_hist
                BiliUtils.save_history(self.history)

        except Exception: pass

    async def push_simple(self, text: str, image_url: str, stream_ids: List[str]):
        for sid in stream_ids:
            await send_api.text_to_stream(text=text, stream_id=sid)
        if image_url:
            b64 = await BiliUtils.url_to_base64(image_url)
            if b64:
                for sid in stream_ids:
                    await send_api.image_to_stream(image_base64=b64, stream_id=sid)

    async def process_and_push(self, item: Dict, stream_ids: List[str], max_imgs: int):
        parsed = self.parse_dynamic(item)
        if not parsed: return

        author = parsed.get('author', 'UP主')
        text = f"📢 【{author}】发布了新动态！\n\n{parsed['text']}\n🔗 链接: {parsed['url']}"

        images = parsed['images']
        
        # === 核心修改：将“图片过多”提示合并进文本，且不发图片 ===
        if len(images) > max_imgs:
            # 追加提示文本
            text += f"\n\n⚠️ 动态图片过多，共【{len(images)}】张，请点击链接去原动态查看图片。"
            # 清空图片列表，防止后续发送
            images = []
        
        # 先发送文本（可能包含警告）
        for sid in stream_ids:
            await send_api.text_to_stream(text=text, stream_id=sid)

        # 再发送图片（如果有）
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
            
            result = {
                "type": "unknown", "text": "", "images": [], 
                "url": f"https://t.bilibili.com/{id_str}",
                "author": module_author.get('name', 'UP主')
            }

            main_text, main_images = self._extract_major_data(module_dynamic)
            desc_text = (module_dynamic.get('desc') or {}).get('text', '')
            
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

# ====================
# 3. 交互指令
# ====================
class BiliCommand(BaseCommand):
    command_name = "bili_control"
    command_description = "B站订阅控制"
    command_pattern = r"^/bili_control\s+(?P<action>start|stop|status|test)(?:\s+(?P<arg>\S+))?$"

    async def execute(self) -> Tuple[bool, str, bool]:
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
            cnt = len(self.get_config("subscriptions.users"))
            await self.send_text(f"📊 状态:{st} | 订阅数:{cnt}")
        elif action == "test":
            if not arg:
                await self.send_text("❌ 用法: /bili_control test <uid>")
                return False, "", True
            await self.send_text(f"🧪 测试 UID {arg}...")
            try:
                u = user.User(int(arg), credential=monitor_instance.credential)
                dyn = await u.get_dynamics_new()
                items = dyn.get('items', [])
                if not items: 
                    await self.send_text("无动态")
                else:
                    # 测试指令逻辑不变：找最新非置顶
                    item_to_push = items[0]
                    for item in items:
                        is_top = False
                        try:
                            if item.get('modules', {}).get('module_tag', {}).get('text') == '置顶': is_top = True
                        except: pass
                        if not is_top: 
                            item_to_push = item
                            break
                    
                    sid = None
                    try: sid = self.message.chat_stream.stream_id
                    except: pass
                    if not sid: 
                        await self.send_text("❌ 无法获取当前ID")
                        return True, "err", True

                    await monitor_instance.process_and_push(item_to_push, [sid], 9)
                    await self.send_text("✅ 测试完成")
            except Exception as e: await self.send_text(f"❌ 错误: {e}")

        return True, "done", True

@register_plugin
class BiliPlugin(BasePlugin):
    plugin_name = "bilibili_dynamic_subscription"
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
            "poll_jitter": ConfigField(int, 10, "轮询抖动秒数(实际=基准±抖动)"), # [新增] 抖动配置
            "credential": ConfigField(dict, {}, "Cookie"),
            "max_images": ConfigField(int, 3, "最大图片数")
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
