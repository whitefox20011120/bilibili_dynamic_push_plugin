import asyncio
import time

from bilibili_api import user

from .utils import BiliUtils, fetch_uname
from .subscription import sub_manager
from .monitor import monitor_instance


async def handle_command(plugin, action, arg, group_id, reply_group):
    """
    处理 /B动态 系列指令。
    plugin: BiliPlugin 实例（用于读取 ctx/config）
    """
    ctx = plugin.ctx
    config = plugin.config

    # start
    if action == "start":
        if monitor_instance.running:
            await reply_group("⚠️ B站监控已在运行中，无需重复启动。")
        else:
            await monitor_instance.start(ctx, config)
            await reply_group("✅ B站监控已成功启动。")
        return True, None, True

    # stop
    if action == "stop":
        await monitor_instance.stop()
        await reply_group("🛑 B站监控已停止运行。")
        return True, None, True

    # status
    if action == "status":
        st = "🟢 运行中" if monitor_instance.running else "🔴 已停止"
        cnt = len(monitor_instance.uid_to_stream_ids)
        await reply_group(f"📊 B站监控状态: {st}\n当前共监控 {cnt} 个 B站 UID。")
        return True, None, True

    # info
    if action == "info":
        if not arg:
            await reply_group("❌ 用法错误: /B动态 info <uid>")
            return True, None, True
        try:
            u = user.User(int(arg), credential=monitor_instance.credential)
            raw_info = await u.get_live_info()
            live_room = raw_info.get("live_room", {})
            status = live_room.get("liveStatus", 0)
            uname = raw_info.get("name", "未知")

            if status == 1:
                user_hist = monitor_instance.history.get(arg, {})
                start_time = user_hist.get("live_start_time", 0) if isinstance(user_hist, dict) else 0
                duration_text = ""
                if start_time:
                    duration_text = f"\n⏱️ 已直播: {BiliUtils.format_duration(time.time() - start_time)}"

                msg = (
                    f"🟢 【{uname}】正在直播中！\n"
                    f"📺 {live_room.get('title')}\n"
                    f"🔗 {live_room.get('url')}"
                    f"{duration_text}"
                )
                await monitor_instance.push_simple(msg, live_room.get("cover", ""), [int(group_id)])
                return True, "✅ 直播状态已推送到当前群聊。", True
            else:
                return True, f"⚪ 状态查询结果：【{uname}】未开播。", True
        except Exception as e:
            return True, f"❌ 查询失败: {e}", True

    # test
    if action == "test":
        if not arg:
            return True, "❌ 用法错误: /B动态 test <uid>", True
        try:
            u = user.User(int(arg), credential=monitor_instance.credential)
            dyn = await u.get_dynamics_new()
            items = dyn.get("items", [])
            if not items:
                return True, "⚠️ 该 UID 暂无动态", True

            item_to_push = None
            for it in items:
                if it.get("type") == "DYNAMIC_TYPE_LIVE_RCMD":
                    continue
                try:
                    major_type = (
                        it.get("modules", {}).get("module_dynamic", {}).get("major", {}) or {}
                    ).get("type")
                    if major_type == "MAJOR_TYPE_LIVE_RCMD":
                        continue
                except Exception:
                    pass
                if monitor_instance._is_top_dynamic(it):
                    continue
                item_to_push = it
                break

            if not item_to_push:
                return True, "⚠️ 该 UID 除置顶外暂无可推送的普通动态", True

            await monitor_instance.process_and_push(
                item_to_push, [int(group_id)], config.settings.max_images
            )
            return True, "✅ 测试推送已成功发送到群聊", True
        except Exception as e:
            return True, f"❌ 推送错误: {e}", True

    # add
    if action == "add":
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

    # remove
    if action == "remove":
        if not arg or not arg.isdigit():
            await reply_group("❌ 参数错误！请提供正确的数字UID。\n用法: /B动态 remove <UID>")
            return True, None, True

        uid = str(arg)
        gid = int(group_id)

        if gid in sub_manager.data["static"].get(uid, []):
            await reply_group("⚠️ 无法移除！\n该UID是在 config 配置文件中固定订阅的。")
            return True, None, True

        async with sub_manager.lock:
            custom_groups = sub_manager.data["custom"].get(uid, [])
            if gid in custom_groups:
                sub_manager.data["custom"][uid].remove(gid)
                if not sub_manager.data["custom"][uid]:
                    del sub_manager.data["custom"][uid]
            else:
                await reply_group("⚪ 当前群聊并没有通过指令订阅过此UID，无需移除。")
                return True, None, True

        await sub_manager.save()
        await monitor_instance.update_subscription_map()
        await reply_group("🗑️ 已成功将此UID 从当前群聊的动态订阅中移除。")
        return True, None, True

    # list
    if action == "list":
        gid = int(group_id)
        static_list, custom_list = [], []

        for uid, groups in sub_manager.data["static"].items():
            if gid in groups:
                static_list.append(uid)
        for uid, groups in sub_manager.data["custom"].items():
            if gid in groups:
                custom_list.append(uid)

        if not static_list and not custom_list:
            await reply_group("📭 当前群聊暂无任何B站订阅。")
            return True, None, True

        missing = [u for u in static_list + custom_list if not sub_manager.get_name(u)]
        for uid in missing:
            uname = await fetch_uname(uid, monitor_instance.credential)
            if uname:
                await sub_manager.set_name(uid, uname)
            await asyncio.sleep(0.3)

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

    # help
    if action == "help":
        help_text = (
            "🛠️ Bilibili 订阅管理指令\n"
            "------------------\n"
            "➕ /B动态 add [UID]\n   添加当前群聊对该 UID 的订阅\n"
            "➖ /B动态 remove [UID]\n   移除当前群聊的动态订阅\n"
            "📋 /B动态 list\n   列出当前群组的所有订阅源\n"
            "🔍 /B动态 info [UID]\n   查询该 UID 实时直播状态\n"
            "🧪 /B动态 test [UID]\n   触发一次动态推送测试\n"
            "📈 /B粉丝 [UID]\n   触发一次UP主粉丝数查询\n"
            "------------------\n"
            "⚠️ 仅管理员可用，固定订阅需改后台 Config"
        )
        await reply_group(help_text)
        return True, None, True

    return True, f"❌ 未知指令: {action}。发送 /B动态 help 查看帮助。", True
