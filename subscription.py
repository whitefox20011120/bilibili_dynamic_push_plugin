"""
订阅管理器
用于指令增删订阅
"""
import os
import json
import asyncio


class SubscriptionManager:
    def __init__(self):
        self.file_path = os.path.join(os.path.dirname(__file__), "subscriptions.json")
        self.lock = asyncio.Lock()
        self.data = {"static": {}, "custom": {}, "names": {}}
        self._load_sync()

    def _load_sync(self):
        if os.path.exists(self.file_path):
            try:
                with open(self.file_path, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
            except Exception:
                self.data = {"static": {}, "custom": {}, "names": {}}
        else:
            self.data = {"static": {}, "custom": {}, "names": {}}
        self.data.setdefault("static", {})
        self.data.setdefault("custom", {})
        self.data.setdefault("names", {})

    async def save(self):
        async with self.lock:
            def _write():
                with open(self.file_path, "w", encoding="utf-8") as f:
                    json.dump(self.data, f, indent=2, ensure_ascii=False)
            await asyncio.to_thread(_write)

    async def sync_static(self, config_users: list):
        new_static = {}
        for sub in config_users:
            raw_groups = sub.get("groups", [])
            uids = []
            if sub.get("uid"):
                uids.append(str(sub["uid"]))
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


# 全局单例
sub_manager = SubscriptionManager()
