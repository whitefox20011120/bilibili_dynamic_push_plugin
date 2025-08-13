import time
import json
import threading
import base64
import random
import re
from typing import Dict, List, Any, Optional, Set, Tuple, Callable
from pathlib import Path
import urllib.request
import urllib.error
import urllib.parse
from hashlib import md5
from functools import reduce

from src.plugin_system import BasePlugin, register_plugin
from src.plugin_system.apis import send_api
from src.common.logger import get_logger

logger = get_logger("plugins.bilibili_dynamic_push_plugin")


# ---------------- 基础 HTTP 工具 ----------------
def _http_build_request(url: str, params: dict | None, headers: dict | None):
    if params:
        q = urllib.parse.urlencode(params)
        url += ("&" if "?" in url else "?") + q
    req = urllib.request.Request(url)
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    return req


def _http_get(url: str, params: dict | None, headers: dict | None, timeout: int):
    req = _http_build_request(url, params, headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", 200)
            data = resp.read()
            text = None
            ctype = resp.headers.get_content_charset() or "utf-8"
            try:
                text = data.decode(ctype, errors="ignore")
            except Exception:
                pass
            return status, data, text
    except urllib.error.HTTPError as e:
        return e.code, b"", ""
    except Exception:
        return 0, b"", ""


def _unique(seq: List[str]) -> List[str]:
    seen: Set[str] = set()
    out: List[str] = []
    for s in seq:
        if s not in seen:
            out.append(s)
            seen.add(s)
    return out


# ---------------- 结构守护 & 文本清理 ----------------
def _ensure_dict(x: Any, *, pick_first: bool = True) -> dict:
    """若 x 为 list，则取第一个 dict；否则若为 dict 原样返回；否则 {}"""
    if isinstance(x, dict):
        return x
    if isinstance(x, list) and pick_first:
        for it in x:
            if isinstance(it, dict):
                return it
        return {}
    return {}


def _ensure_list(x: Any) -> list:
    if isinstance(x, list):
        return x
    if x is None:
        return []
    return [x]


def _sanitize_text(s: str) -> str:
    """清理常见零宽字符，避免看起来“空”的假阳性。"""
    if not s:
        return ""
    return re.sub(r"[\u200B\u200C\u200D\uFEFF]", "", s).strip()


# ---------------- 富文本节点拼接 ----------------
def _stringify_rich_nodes(nodes: List[Any], at_resolver: Optional[Callable[[str], Optional[str]]] = None) -> str:
    """
    将 B 站富文本节点列表拼接为纯文本。
    识别类型（常见）：TEXT / AT / TOPIC / EMOJI / URL / BR
    """
    out: List[str] = []
    for node in _ensure_list(nodes):
        n = _ensure_dict(node)
        t = (n.get("text") or "").strip()
        tp = n.get("type") or n.get("biz_type") or ""
        tp = str(tp)

        if tp.endswith("BR") or tp == "BR":
            out.append("\n")
            continue

        if "AT" in tp:
            if t:
                out.append(t)
            else:
                rid = str(n.get("rid") or n.get("mid") or "").strip()
                name = at_resolver(rid) if (at_resolver and rid) else None
                out.append(("@" + name) if name else "@")
            continue

        if "TOPIC" in tp:
            if t:
                t = t if t.startswith("#") else f"#{t}#"
                out.append(t)
            continue

        if "EMOJI" in tp:
            if t:
                out.append(t)
            else:
                emoji = _ensure_dict(n.get("emoji"))
                et = (emoji.get("text") or emoji.get("emoji_name") or "").strip()
                if et:
                    out.append(et)
            continue

        if "URL" in tp or "LINK" in tp:
            if t:
                out.append(t)
            else:
                url = (n.get("url") or n.get("jump_url") or "").strip()
                if url:
                    out.append(url)
            continue

        if t:
            out.append(t)

    s = "".join(out)
    s = re.sub(r"\n{3,}", "\n\n", s)
    s = _sanitize_text(s)
    return s


# ---------------- 文案增强工具 ----------------
def _extract_text_from_desc(desc: dict, at_resolver: Optional[Callable[[str], Optional[str]]] = None) -> str:
    d = _ensure_dict(desc)
    txt = _sanitize_text(str(d.get("text") or ""))
    if txt:
        return txt
    nodes = _ensure_list(d.get("rich_text_nodes"))
    if nodes:
        return _stringify_rich_nodes(nodes, at_resolver=at_resolver)
    return ""


def _extract_text_from_major(major: Any, at_resolver: Optional[Callable[[str], Optional[str]]] = None) -> str:
    parts: List[str] = []

    def pick_from_article(block: dict):
        title = block.get("title") or block.get("title_text") or ""  # 优化：添加更多可能的标题键
        summary = block.get("summary") or block.get("desc") or block.get("description") or ""  # 优化：添加更多描述键
        if title:
            parts.append(str(title))
        if summary and summary != title:
            parts.append(str(summary))

    def pick_from_archive(block: dict):
        # 视频动态：优先标题（优化：仅标题，不追加简介）
        title = block.get("title") or block.get("title_text") or block.get("name") or ""
        if title:
            parts.append(str(title))
        # 不追加 desc，以符合用户需求：仅发送标题

    def pick_from_opus(block: dict):
        title = block.get("title") or ""
        if title:
            parts.append(str(title))
        summary = _ensure_dict(block.get("summary"))
        stxt = _sanitize_text(summary.get("text") or "")
        if stxt:
            parts.append(stxt)
        s_nodes = _ensure_list(summary.get("rich_text_nodes"))
        if s_nodes:
            parts.append(_stringify_rich_nodes(s_nodes, at_resolver=at_resolver))
        o_nodes = _ensure_list(block.get("rich_text_nodes"))
        if o_nodes:
            parts.append(_stringify_rich_nodes(o_nodes, at_resolver=at_resolver))
        for k in ("content", "desc", "text", "description", "intro", "summary"):  # 优化：添加更多键
            v = _sanitize_text(block.get(k) or "") if isinstance(block.get(k), str) else ""
            if v:
                parts.append(v)

    # list 情况：逐项递归拼接
    if isinstance(major, list):
        for m in major:
            t = _extract_text_from_major(m, at_resolver=at_resolver)
            if t:
                parts.append(t)
        return "\n".join([p for p in parts if p]).strip()

    if not isinstance(major, dict):
        return ""

    # 支持 dyn_xxx 结构（优化：兼容 polymer 直接 dyn_archive 等）
    if "dyn_opus" in major:
        pick_from_opus(_ensure_dict(major.get("dyn_opus")))
    elif "opus" in major:
        pick_from_opus(_ensure_dict(major.get("opus")))

    if "dyn_article" in major:
        pick_from_article(_ensure_dict(major.get("dyn_article")))
    elif "article" in major:
        pick_from_article(_ensure_dict(major.get("article")))

    if "dyn_archive" in major:
        pick_from_archive(_ensure_dict(major.get("dyn_archive")))
    elif "archive" in major:
        pick_from_archive(_ensure_dict(major.get("archive")))

    if "live" in major or "dyn_live" in major:
        live = _ensure_dict(major.get("live") or major.get("dyn_live"))
        title = live.get("title") or ""
        room = live.get("room_id") or live.get("roomid") or ""
        if title:
            parts.append(str(title))
        if room:
            parts.append(f"直播间：{room}")

    if "pgc" in major or "dyn_pgc" in major:
        pgc = _ensure_dict(major.get("pgc") or major.get("dyn_pgc"))
        season = _ensure_dict(pgc.get("season"))
        ep = _ensure_dict(pgc.get("ep"))
        title = ep.get("title") or season.get("title") or ""
        subtitle = ep.get("long_title") or ep.get("pub_time") or ep.get("desc") or ep.get("description") or ""  # 优化：添加描述
        if title:
            parts.append(str(title))
        if subtitle and subtitle != title:
            parts.append(str(subtitle))

    if "ugc_season" in major or "dyn_ugc_season" in major:
        ugc = _ensure_dict(major.get("ugc_season") or major.get("dyn_ugc_season"))
        title = ugc.get("title") or ""
        if title:
            parts.append(str(title))

    text = "\n".join([p.strip() for p in parts if isinstance(p, str) and p.strip()])
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return _sanitize_text(text)


def _norm_author(module_author: dict) -> dict:
    """把 MODULE_TYPE_AUTHOR 的形态规范化为 {name, mid, ...}"""
    ma = _ensure_dict(module_author)
    name = ma.get("name")
    mid = ma.get("mid")
    if (not name) or (mid is None):
        user = _ensure_dict(ma.get("user"))
        name = name or user.get("name")
        mid = mid or user.get("mid")
    out = dict(ma)
    if name:
        out["name"] = name
    if mid is not None:
        out["mid"] = mid
    return out


def _normalize_modules(modules_raw: Any) -> dict:
    """
    兼容两种典型结构：
    1) dict: {"module_author": {...}, "module_desc": {...}, "module_dynamic": {...}, ...}
    2) list: [{"module_type":"MODULE_TYPE_AUTHOR","module_author":{...}}, {"module_type":"MODULE_TYPE_DESC","module_desc":{...}}, ...]
    返回统一的 dict 形式。
    """
    if isinstance(modules_raw, dict):
        d = dict(modules_raw)
        if "module_author" in d:
            d["module_author"] = _norm_author(_ensure_dict(d.get("module_author")))
        return d

    out: dict = {}
    for m in _ensure_list(modules_raw):
        m = _ensure_dict(m)
        t = str(m.get("module_type") or "").upper()
        if t == "MODULE_TYPE_AUTHOR" and "module_author" in m:
            out["module_author"] = _norm_author(_ensure_dict(m.get("module_author")))
        elif t == "MODULE_TYPE_DESC" and "module_desc" in m:
            out["module_desc"] = _ensure_dict(m.get("module_desc"))
        elif t == "MODULE_TYPE_DYNAMIC" and "module_dynamic" in m:
            out["module_dynamic"] = _ensure_dict(m.get("module_dynamic"))
        elif t == "MODULE_TYPE_STAT" and "module_stat" in m:
            out["module_stat"] = _ensure_dict(m.get("module_stat"))
        elif t == "MODULE_TYPE_TAG" and "module_tag" in m:
            out["module_tag"] = _ensure_dict(m.get("module_tag"))
    return out


def _pick_author_name(modules: dict, uid: str, resolver=None) -> str:
    ma = _ensure_dict(modules.get("module_author"))
    name = ma.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    user = _ensure_dict(ma.get("user"))
    uname = user.get("name")
    if isinstance(uname, str) and uname.strip():
        return uname.strip()
    mid = ma.get("mid") or user.get("mid") or uid
    if resolver:
        got = resolver(str(mid))
        if got:
            return got
    return f"UID:{uid}"


# ---------------- 图片提取（兼容 dyn_draw/major 等多形态） ----------------
def _collect_images_from_major(major: Any) -> List[str]:
    """
    统一收集图集/文章封面/视频封面等。
    对“视频动态”（major.archive/pgc/ugc_season）：
      - 优先取 cover/cover_url/pic/first_frame/dynamic_cover
      - 兼容 covers 数组
    """
    urls: List[str] = []
    if isinstance(major, list):
        for m in major:
            urls.extend(_collect_images_from_major(m))
        return _unique(urls)
    if not isinstance(major, dict):
        return urls

    # 图文（优化：支持 dyn_draw）
    if "dyn_draw" in major:
        for it in (_ensure_dict(major.get("dyn_draw")).get("items") or []):
            src = _ensure_dict(it).get("src")
            if src:
                urls.append(src)
    elif "draw" in major:
        for it in (_ensure_dict(major.get("draw")).get("items") or []):
            src = _ensure_dict(it).get("src")
            if src:
                urls.append(src)

    # OPUS（也可能带图，支持 dyn_opus）
    if "dyn_opus" in major:
        opus = _ensure_dict(major.get("dyn_opus"))
        for key in ("pics", "pictures", "images"):
            for pic in _ensure_list(opus.get(key)):
                src = _ensure_dict(pic).get("url") or _ensure_dict(pic).get("src")
                if src:
                    urls.append(src)
        cov = opus.get("cover")
        if isinstance(cov, str) and cov:
            urls.append(cov)
    elif "opus" in major:
        opus = _ensure_dict(major.get("opus"))
        for key in ("pics", "pictures", "images"):
            for pic in _ensure_list(opus.get(key)):
                src = _ensure_dict(pic).get("url") or _ensure_dict(pic).get("src")
                if src:
                    urls.append(src)
        cov = opus.get("cover")
        if isinstance(cov, str) and cov:
            urls.append(cov)

    # 文章（支持 dyn_article）
    if "dyn_article" in major:
        covs = _ensure_list(_ensure_dict(major.get("dyn_article")).get("covers"))
        for c in covs:
            if c:
                urls.append(str(c))
    elif "article" in major:
        covs = _ensure_list(_ensure_dict(major.get("article")).get("covers"))
        for c in covs:
            if c:
                urls.append(str(c))

    # 视频（UGC，支持 dyn_archive）
    if "dyn_archive" in major:
        arc = _ensure_dict(major.get("dyn_archive"))
        for key in ("cover", "cover_url", "pic", "dynamic_cover", "first_frame"):
            val = arc.get(key)
            if isinstance(val, str) and val.strip():
                urls.append(val.strip())
        for c in _ensure_list(arc.get("covers")):
            if c:
                urls.append(str(c))
        # 某些场景封面在 arc["bvid_cover"] 或 arc["pic_url"]
        for key in ("bvid_cover", "pic_url"):
            val = arc.get(key)
            if isinstance(val, str) and val.strip():
                urls.append(val.strip())
    elif "archive" in major:
        arc = _ensure_dict(major.get("archive"))
        for key in ("cover", "cover_url", "pic", "dynamic_cover", "first_frame"):
            val = arc.get(key)
            if isinstance(val, str) and val.strip():
                urls.append(val.strip())
        for c in _ensure_list(arc.get("covers")):
            if c:
                urls.append(str(c))
        # 某些场景封面在 arc["bvid_cover"] 或 arc["pic_url"]
        for key in ("bvid_cover", "pic_url"):
            val = arc.get(key)
            if isinstance(val, str) and val.strip():
                urls.append(val.strip())

    # 番剧/合集等也可能带封面（支持 dyn_pgc 等）
    for k in ("pgc", "dyn_pgc", "live", "dyn_live", "ugc_season", "dyn_ugc_season"):
        if k in major:
            blk = _ensure_dict(major.get(k))
            for key in ("cover", "cover_url", "pic", "dynamic_cover", "first_frame"):
                val = blk.get(key)
                if isinstance(val, str) and val.strip():
                    urls.append(val.strip())

    return _unique(urls)


def _collect_images_from_module_dynamic(md_block: dict) -> List[str]:
    urls: List[str] = []
    md_block = _ensure_dict(md_block)
    # 支持 dyn_draw 直接在 md_block
    dyn_draw = _ensure_dict(md_block.get("dyn_draw"))
    for it in _ensure_list(dyn_draw.get("items")):
        src = _ensure_dict(it).get("src")
        if src:
            urls.append(src)
    # major 或直接 md_block（优化：兼容无 major 的结构，如 dyn_archive 直接）
    major = md_block.get("major") or md_block
    urls.extend(_collect_images_from_major(major))
    return _unique(urls)


# ---------------- 注册插件 ----------------
@register_plugin
class BilibiliDynamicPushPlugin(BasePlugin):
    plugin_name = "bilibili_dynamic_push_plugin"
    plugin_description = (
        "定时检测B站UP主最新动态并推送到指定QQ群；支持多组合路由、转发动态；"
        "新/旧接口+HTML兜底；冷启动仅记不发；去重；"
        "静默模式（仅错误输出）；文案增强（富文本&转发解析）；昵称查补缓存；"
        "debug.output_dir 指定调试落盘；兼容 modules 列表结构与 dyn_forward；"
        "发图链路（Napcat 友好）：Base64 → URL → file:/// 兜底；视频动态自动携带封面图。"
    )
    plugin_version = "1.2.1"
    plugin_author = "白狐"
    enable_plugin = True

    config_file_name = "config.toml"
    config_section_descriptions = {}
    config_schema = {}

    dependencies: List[str] = []
    python_dependencies: List[str] = ["pillow"]  # 推荐安装，用于压缩/转 JPG

    # ---------------- 日志封装（silent 下屏蔽普通日志，错误始终输出） ----------------
    def _log(self, msg: str, *, flush: bool = True):
        if not getattr(self, "silent", False):
            print(msg, flush=flush)

    def _err(self, msg: str, *, flush: bool = True):
        print(msg, flush=flush)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        def get_conf(path: str, default=None):
            cur: Any = self.config or {}
            for key in path.split("."):
                if not isinstance(cur, dict) or key not in cur:
                    return default
                cur = cur[key]
            return cur

        def as_list(val: Any) -> List[str]:
            if isinstance(val, list):
                return [str(x) for x in val]
            if val is None:
                return []
            return [str(val)]

        # 基本配置
        self.enable = bool(get_conf("monitor.enable", True))
        self.interval_seconds = max(1, int(get_conf("monitor.interval_minutes", 3))) * 60
        self.jitter_seconds = float(get_conf("monitor.jitter_seconds", 15))
        self.silent = bool(get_conf("monitor.silent", False))  # ★ 静默开关

        # 发图控制
        self.send_images = bool(get_conf("image.send_images", True))        # ★ 可关图
        self.force_base64 = bool(get_conf("image.force_base64", True))      # Base64 优先
        self.base64_chunk_limit = int(get_conf("image.base64_chunk_limit", 5_500_000))
        self.downscale_width = int(get_conf("image.downscale_width", 720))
        self.jpeg_quality = int(get_conf("image.jpeg_quality", 85))
        self.per_image_delay_ms = int(get_conf("image.per_image_delay_ms", 1600))  # 每张图间隔

        # API/请求
        self.api_base = str(get_conf("api.base_url", "https://api.bilibili.com"))
        self.timeout = int(get_conf("api.timeout", 10))
        self.prefer_old = bool(get_conf("api.prefer_old", True))

        

        # 时效 & 回填策略
        self.max_push_age_hours = int(get_conf("monitor.max_push_age_hours", 48))
        self.startup_ts = int(time.time())
        self.push_on_first_fetch = bool(get_conf("monitor.push_on_first_fetch", False))
        self.allow_backfill_hours = int(get_conf("monitor.allow_backfill_hours", 0))
        self.cold_start_grace_hours = int(get_conf("monitor.cold_start_grace_hours", 0))
# 多组合路由：UID→群号并集
        self.uid_groups_map: Dict[str, List[str]] = {}
        routes = get_conf("bilibili.routes", None)
        legacy_uids = as_list(get_conf("bilibili.uids", []))
        legacy_groups = [str(g) for g in as_list(get_conf("bilibili.groups", []))]

        if isinstance(routes, list) and routes:
            for r in routes:
                try:
                    r_uids = as_list(_ensure_dict(r).get("uids") or _ensure_dict(r).get("uid"))
                    r_groups = [str(g) for g in as_list(_ensure_dict(r).get("groups") or _ensure_dict(r).get("group"))]
                except Exception:
                    r_uids, r_groups = [], []
                for uid in r_uids:
                    cur = self.uid_groups_map.get(uid, [])
                    cur.extend(r_groups)
                    self.uid_groups_map[uid] = _unique(cur)
        if legacy_uids:
            for uid in legacy_uids:
                cur = self.uid_groups_map.get(uid, [])
                cur.extend(legacy_groups)
                self.uid_groups_map[uid] = _unique(cur)
        if not self.uid_groups_map and legacy_uids:
            self.uid_groups_map = {uid: legacy_groups[:] for uid in legacy_uids}

        self.cookie: str = str((get_conf("bilibili.cookie", "") or "").strip())

        # 其它策略
        self.enable_html_fallback = bool(get_conf("fallback.enable_html", True))

        # 调试落盘
        self.debug_dump = bool(get_conf("debug.dump_json", False))
        self.debug_uid_whitelist = set(get_conf("debug.dump_uid", []) or [])
        out_dir_conf = str(get_conf("debug.output_dir", "") or "").strip()
        try:
            plugin_dir = Path(__file__).resolve().parent
        except Exception:
            plugin_dir = Path(".").resolve()
        self.debug_out_dir = Path(out_dir_conf) if out_dir_conf else (plugin_dir / "debug")
        self.debug_out_dir.mkdir(parents=True, exist_ok=True)

        # 请求头
        self._default_headers = {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/126.0.0.0 Safari/537.36"),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Connection": "keep-alive",
        }
        if self.cookie:
            self._default_headers["Cookie"] = self.cookie

        # 状态持久化
        try:
            data_dir = Path(self.get_data_dir())
        except Exception:
            data_dir = Path("./data/bilibili_dynamic_push_plugin")
        data_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = data_dir / "last_seen.json"
        self.last_seen: Dict[str, str] = self._load_state()
        self._stagnant: Dict[str, int] = {}

        self._wbi_cache: Optional[Tuple[str, str, float]] = None  # (img_key, sub_key, ts)
        self._stop_flag = False
        self._thread: Optional[threading.Thread] = None

        # 昵称缓存
        self._uname_cache: Dict[str, str] = {}

        # 启动自检 Cookie
        self._cookie_healthcheck()

        if self.enable:
            self._log("[BilibiliDynamicPush] 动态监控任务已启动")
            if not self.uid_groups_map:
                self._err("[BilibiliDynamicPush] ⚠ 未配置任何 UID/群号，请检查 config.toml")
            self._thread = threading.Thread(target=self._loop, name="bili-dyn-push", daemon=True)
            self._thread.start()
        else:
            self._log("[BilibiliDynamicPush] 插件未启用（monitor.enable=false）")

    async def on_unload(self):
        self._stop_flag = True
        time.sleep(0.2)
        self._save_state()
        self._log("[BilibiliDynamicPush] 监控任务已停止")

    # ---------------- Cookie 健康检查 ----------------
    def _cookie_healthcheck(self) -> bool:
        url = "https://api.bilibili.com/x/web-interface/nav"
        status, _, text = self._http_get_with_retry(url, headers=self._default_headers, max_retry=1)
        ok = False
        if status == 200 and text:
            try:
                j = json.loads(text)
            except Exception:
                j = {}
            data = j.get("data") or {}
            is_login = bool(_ensure_dict(data).get("isLogin")) or (j.get("code") == 0 and data != {})
            if is_login:
                tail = ""
                m = re.search(r"SESSDATA=([^;]+)", self.cookie or "")
                if m:
                    tail = m.group(1)[-6:]
                self._log(f"[BilibiliDynamicPush] ✅ Cookie 登录状态: 已登录 (SESSDATA…{tail})")
                ok = True
        if not ok:
            self._err("[BilibiliDynamicPush] ⚠ Cookie 未生效/未登录，可能出现 -352；请确认 [bilibili].cookie 为单行整条。")
        return ok

    # ---------------- 主循环 ----------------
    def _loop(self):
        while not self._stop_flag:
            try:
                self._log(f"[BilibiliDynamicPush] ⏱ 轮询开始")
                self._check_all_uids()
            except Exception as e:
                self._err(f"[BilibiliDynamicPush] 检测任务出错: {e}")
            sleep_for = max(5, self.interval_seconds + random.uniform(-self.jitter_seconds, self.jitter_seconds))
            self._log(f"[BilibiliDynamicPush] 😴 休眠 {sleep_for:.1f} 秒后再次检测")
            time.sleep(sleep_for)

    def _check_all_uids(self):
        for uid, groups in self.uid_groups_map.items():
            if not groups:
                self._err(f"[BilibiliDynamicPush] UID={uid} 未绑定群号，跳过")
                continue
            try:
                self._handle_uid(uid, groups)
            except Exception as e:
                self._err(f"[BilibiliDynamicPush] 处理 UID={uid} 出错: {e}")

    def _handle_uid(self, uid: str, groups: List[str]):
        item = self._fetch_latest_old(uid) if self.prefer_old else self._fetch_latest_new(uid)
        if not item:
            alt = self._fetch_latest_new(uid) if self.prefer_old else self._fetch_latest_old(uid)
            item = item or alt
        if not item and self.enable_html_fallback:
            item = self._fetch_space_html_latest(uid)
        if not item:
            self._log(f"[BilibiliDynamicPush] UID={uid} 无数据/全是置顶/请求失败")
            return

        if isinstance(item, list):
            self._log("[BilibiliDynamicPush] ⚠ STRUCT(list→dict) new item is list, choose first dict")
            item = _ensure_dict(item)

        cur_id = str(_ensure_dict(item).get("id_str") or "")
        if not cur_id.isdigit():
            self._err(f"[BilibiliDynamicPush] UID={uid} 缺少有效 id_str")
            return

        last_id = self.last_seen.get(uid) or ""
        if not last_id:
            self.last_seen[uid] = cur_id
            self._save_state()
            self._log(f"[BilibiliDynamicPush] 🔧 冷启动记忆 UID={uid} last_seen={cur_id}（不推送）")
            return

        if int(cur_id) <= int(last_id):
            cnt = self._stagnant.get(uid, 0) + 1
            self._stagnant[uid] = cnt
            self._log(f"[BilibiliDynamicPush] UID={uid} 动态未更新 (last={last_id}, cur={cur_id}) x{cnt}")
            return

        self._log(f"[BilibiliDynamicPush] ✅ UID={uid} 发现新动态 (ID: {cur_id})，准备推送")

        if self.debug_dump and (not self.debug_uid_whitelist or uid in self.debug_uid_whitelist):
            try:
                self._dump_module_json(uid, item, reason="before_push")
            except Exception:
                pass


        

        # —— 首次获取（冷启动/新加UID）基线保护 ——
        if not self.last_seen.get(uid):
            pub_ts = self._get_publish_ts(item)
            if not self.push_on_first_fetch:
                self._log(f"[BilibiliDynamicPush] 🧊 UID={uid} 首次获取，建立基线(不回填)，last_seen <- {cur_id}")
                self.last_seen[uid] = cur_id
                self._save_state()
                return
            else:
                # 允许首次回填，但仅限近 allow_backfill_hours 内
                allow_age = int(self.allow_backfill_hours * 3600)
                now = int(time.time())
                if (not pub_ts) or (now - pub_ts) >= allow_age:
                    self._log(f"[BilibiliDynamicPush] 🧊 UID={uid} 首次获取但过期(>{self.allow_backfill_hours}h)，仅建立基线，last_seen <- {cur_id}")
                    self.last_seen[uid] = cur_id
                    self._save_state()
                    return
                # 否则：pub_ts 在回填许可窗内，允许继续推送

        # —— 冷启动回填限制（旧动态一律不回填，窗口由 cold_start_grace_hours 控制） ——
        pub_ts = self._get_publish_ts(item)
        if pub_ts and self.cold_start_grace_hours >= 0:
            cutoff = self.startup_ts - int(self.cold_start_grace_hours * 3600)
            if pub_ts < cutoff:
                self._log(f"[BilibiliDynamicPush] 🧊 UID={uid} 冷启动回填拦截(pub<{cutoff})，仅更新last_seen <- {cur_id}")
                self.last_seen[uid] = cur_id
                self._save_state()
                return
# —— 时效阈值：避免回填过旧动态 ——
        pub_ts = self._get_publish_ts(item)
        now = int(time.time())
        max_age = int(self.max_push_age_hours * 3600)
        if pub_ts and (now - pub_ts) >= max_age:
            age_h = int((now - pub_ts) / 3600)
            self._log(f"[BilibiliDynamicPush] ⏩ UID={uid} 跳过过旧动态 (age={age_h}h ≥ {self.max_push_age_hours}h, id={cur_id})，仅更新last_seen")
            self.last_seen[uid] = cur_id
            self._save_state()
            return


        self._push_dynamic(uid, _ensure_dict(item), groups)
        self.last_seen[uid] = cur_id
        self._save_state()
        self._log(f"[BilibiliDynamicPush] ✅ UID={uid} 推送完成")

    # ---------------- HTTP 重试 ----------------
    def _http_get_with_retry(self, url: str, *, params: dict | None = None, headers: dict | None = None,
                             max_retry: int = 3, base_sleep: float = 0.8):
        last_status = 0
        for i in range(max_retry):
            status, data, text = _http_get(url, params, headers, self.timeout)
            last_status = status
            if status in (412, 429) or (500 <= status < 600) or status == 0:
                wait = (base_sleep * (2 ** i)) + random.uniform(0, 0.6)
                self._log(f"[BilibiliDynamicPush] 🛡 {status} 风控/限流/异常，{wait:.2f}s 后重试 ({i+1}/{max_retry})")
                time.sleep(wait)
                continue
            return status, data, text
        return last_status, b"", ""

    

    def _get_publish_ts(self, item: dict) -> int:
        it = _ensure_dict(item)
        basic = _ensure_dict(it.get("basic"))
        ts = basic.get("pub_ts") or basic.get("pub_time")
        if isinstance(ts, (int, float)) and ts > 0:
            return int(ts)
        modules = _ensure_dict(it.get("modules"))
        ma = _ensure_dict(modules.get("module_author"))
        ts = ma.get("pub_ts") or ma.get("ctime") or ma.get("timestamp")
        if isinstance(ts, (int, float)) and ts > 0:
            return int(ts)
        desc = _ensure_dict(it.get("desc"))
        ts = desc.get("timestamp") or desc.get("ctime")
        if isinstance(ts, (int, float)) and ts > 0:
            return int(ts)
        return 0
# ---------------- WBI 签名 ----------------
    MIXIN_KEY_ENC_TAB = [
        46,47,18,2,53,8,23,32,15,50,10,31,58,3,45,35,27,43,5,49,
        33,9,42,19,29,28,14,39,12,38,41,13,37,48,7,16,24,55,40,
        61,26,17,0,1,60,51,30,4,22,25,54,21,56,59,6,63,57,62,11,
        36,20,34,44,52
    ]

    def _wbi_get_mixin_key(self, raw: str) -> str:
        return reduce(lambda s, i: s + raw[i], self.MIXIN_KEY_ENC_TAB, '')[:32]

    def _wbi_refresh_keys(self) -> Optional[tuple[str, str]]:
        url = "https://api.bilibili.com/x/web-interface/nav"
        status, _, text = self._http_get_with_retry(url, headers=self._default_headers, max_retry=1)
        if status == 200 and text:
            try:
                j = json.loads(text)
                w = _ensure_dict((_ensure_dict(j.get("data")).get("wbi_img")))
                img_url = (w.get("img_url") or "")
                sub_url = (w.get("sub_url") or "")
                img_key = img_url.rsplit("/", 1)[-1].split(".")[0]
                sub_key = sub_url.rsplit("/", 1)[-1].split(".")[0]
                if img_key and sub_key:
                    self._wbi_cache = (img_key, sub_key, time.time())
                    return img_key, sub_key
            except Exception:
                pass
        return None

    def _wbi_get_keys(self) -> Optional[tuple[str, str]]:
        cache = getattr(self, "_wbi_cache", None)
        if cache and (time.time() - cache[2] < 3600):
            return cache[0], cache[1]
        return self._wbi_refresh_keys()

    def _wbi_sign_params(self, params: dict) -> dict:
        ks = self._wbi_get_keys()
        if not ks:
            ks = self._wbi_refresh_keys()
            if not ks:
                return params
        img_key, sub_key = ks
        mixin = self._wbi_get_mixin_key(img_key + sub_key)
        p = dict(params)
        p["wts"] = int(time.time())
        filtered = {k: "".join(ch for ch in str(v) if ch not in "!'()*") for k, v in p.items()}
        query = urllib.parse.urlencode(dict(sorted(filtered.items())))
        p["w_rid"] = md5((query + mixin).encode("utf-8")).hexdigest()
        return p

    # ---------------- 昵称解析：按 mid 远程获取并缓存 ----------------
    def _resolve_uname(self, mid: str) -> Optional[str]:
        if not mid:
            return None
        cached = self._uname_cache.get(str(mid))
        if isinstance(cached, str) and cached.strip():
            return cached
        url = f"{self.api_base}/x/space/wbi/acc/info"
        params = self._wbi_sign_params({"mid": str(mid)})
        status, _, text = self._http_get_with_retry(url, params=params, headers=self._default_headers, max_retry=1)
        if status == 200 and text:
            try:
                j = json.loads(text)
                if j.get("code") == 0:
                    data = _ensure_dict(j.get("data"))
                    name = data.get("name") or data.get("uname")
                    if isinstance(name, str) and name.strip():
                        self._uname_cache[str(mid)] = name.strip()
                        return name.strip()
            except Exception:
                pass
        return None

    # ---------------- 新接口（polymer/web/desktop） ----------------
    def _fetch_latest_new(self, uid: str) -> Optional[dict]:
        def is_pinned(it: dict) -> bool:
            it = _ensure_dict(it)
            basic = _ensure_dict(it.get("basic"))
            modules = _ensure_dict(it.get("modules"))
            author = _ensure_dict(modules.get("module_author"))
            tag = _ensure_dict(modules.get("module_tag"))
            if basic.get("is_top") is True:
                return True
            if author.get("top") is True:
                return True
            if isinstance(tag.get("text"), str) and "置顶" in tag.get("text"):
                return True
            return False

        endpoints = [
            f"{self.api_base}/x/polymer/web-dynamic/v1/feed/space",
            f"{self.api_base}/x/polymer/web-dynamic/desktop/v1/feed/space",
        ]
        base_params = {"host_mid": uid, "timezone_offset": "-480"}

        hdr = dict(self._default_headers)
        hdr["Referer"] = f"https://space.bilibili.com/{uid}/dynamic"
        hdr["Origin"] = "https://space.bilibili.com"

        for url in endpoints:
            params = self._wbi_sign_params(base_params)
            self._log(f"[BilibiliDynamicPush] 🌐 NEW GET {url} params={params}")
            status, _, text = self._http_get_with_retry(url, params=params, headers=hdr)
            self._log(f"[BilibiliDynamicPush] 📡 NEW 状态码: {status}")

            need_retry_with_fresh_wbi = False
            if status == 200 and text:
                try:
                    j = json.loads(text)
                except Exception:
                    j = {}
                code = j.get("code")
                data_block = _ensure_dict(j.get("data"))
                items = _ensure_list(data_block.get("items"))
                items = [x for x in items if isinstance(x, dict)]
                self._log(f"[BilibiliDynamicPush] NEW code={code} items={len(items)}")
                if code == -352:
                    self._log("[BilibiliDynamicPush] ❗ NEW -352，刷新 WBI 后重试")
                    need_retry_with_fresh_wbi = True
                elif code == 0 and items:
                    filtered = [it for it in items if not is_pinned(it)]
                    if filtered:
                        filtered.sort(key=lambda it: int(str(_ensure_dict(it).get("id_str") or "0")), reverse=True)
                        return filtered[0]

            if need_retry_with_fresh_wbi:
                self._wbi_refresh_keys()
                params2 = self._wbi_sign_params(base_params)
                status2, _, text2 = self._http_get_with_retry(url, params=params2, headers=hdr)
                self._log(f"[BilibiliDynamicPush] 🔁 NEW 重试 状态码: {status2}")
                if status2 == 200 and text2:
                    try:
                        j2 = json.loads(text2)
                    except Exception:
                        j2 = {}
                    code2 = j2.get("code")
                    data_block2 = _ensure_dict(j2.get("data"))
                    items2 = _ensure_list(data_block2.get("items"))
                    items2 = [x for x in items2 if isinstance(x, dict)]
                    self._log(f"[BilibiliDynamicPush] NEW(重试) code={code2} items={len(items2)}")
                    if code2 == 0 and items2:
                        filtered2 = [it for it in items2 if not is_pinned(it)]
                        if filtered2:
                            filtered2.sort(key=lambda it: int(str(_ensure_dict(it).get("id_str") or "0")), reverse=True)
                            return filtered2[0]
        return None

    # ---------------- 旧接口（space_history） ----------------
    def _fetch_latest_old(self, uid: str) -> Optional[dict]:
        url = "https://api.vc.bilibili.com/dynamic_svr/v1/dynamic_svr/space_history"
        params = {"host_uid": uid}
        hdr = dict(self._default_headers)
        hdr["Referer"] = f"https://space.bilibili.com/{uid}/dynamic"
        self._log(f"[BilibiliDynamicPush] 🌐 OLD GET {url} params={params}")
        status, _, text = self._http_get_with_retry(url, params=params, headers=hdr)
        self._log(f"[BilibiliDynamicPush] 📡 OLD 状态码: {status}")
        if status == 200 and text:
            try:
                j = json.loads(text)
            except Exception:
                j = {}
            code = j.get("code")
            if code == -352:
                self._err("[BilibiliDynamicPush] ❗ OLD 接口返回 -352（登录/风控）。")
            data = _ensure_dict(j.get("data"))
            cards = _ensure_list(data.get("cards"))
            if (code == 0) and cards:
                out: List[dict] = []
                for c in cards:
                    c = _ensure_dict(c)
                    desc = _ensure_dict(c.get("desc"))
                    dynamic_id_str = str(desc.get("dynamic_id_str") or "")
                    raw_card = c.get("card")
                    try:
                        card = json.loads(raw_card) if isinstance(raw_card, str) else (_ensure_dict(raw_card))
                    except Exception:
                        card = {}

                    uname = (
                        _ensure_dict(card.get("user")).get("name")
                        or _ensure_dict(card.get("origin_user")).get("info", {}).get("uname")
                        or f"UID:{uid}"
                    )
                    text_content = (
                        _ensure_dict(card.get("item")).get("description")
                        or _ensure_dict(card.get("item")).get("content")
                        or card.get("title")
                        or ""
                    )

                    # 图文
                    imgs = []
                    pics = _ensure_list(_ensure_dict(card.get("item")).get("pictures"))
                    if pics:
                        for p in pics:
                            p = _ensure_dict(p)
                            src = p.get("img_src") or p.get("img_url")
                            if src:
                                imgs.append({"src": src})

                    # 视频封面（旧接口常见字段：pic/cover）
                    video_cover = None
                    for key in ("pic", "cover", "dynamic_cover", "first_frame"):
                        v = card.get(key)
                        if isinstance(v, str) and v.strip():
                            video_cover = v.strip()
                            break

                    # 转发
                    forward_major = {}
                    if card.get("origin"):
                        try:
                            origin = json.loads(card.get("origin")) if isinstance(card.get("origin"), str) else (_ensure_dict(card.get("origin")))
                        except Exception:
                            origin = {}
                        otext = (
                            _ensure_dict(origin.get("item")).get("description")
                            or _ensure_dict(origin.get("item")).get("content")
                            or origin.get("title")
                            or ""
                        )
                        oimgs = []
                        opics = _ensure_list(_ensure_dict(origin.get("item")).get("pictures"))
                        if opics:
                            for p in opics:
                                p = _ensure_dict(p)
                                src = p.get("img_src") or p.get("img_url")
                                if src:
                                    oimgs.append({"src": src})

                        # 原动态视频封面兜底
                        ocover = None
                        for key in ("pic", "cover", "dynamic_cover", "first_frame"):
                            v = origin.get(key)
                            if isinstance(v, str) and v.strip():
                                ocover = v.strip()
                                break

                        ouname = (
                            _ensure_dict(origin.get("user")).get("name")
                            or _ensure_dict(card.get("origin_user")).get("info", {}).get("uname")
                            or "原动态"
                        )
                        if oimgs:
                            forward_major = {
                                "forward": {
                                    "orig": {
                                        "modules": {
                                            "module_author": {"name": ouname},
                                            "module_dynamic": {
                                                "desc": {"text": otext},
                                                "major": {"draw": {"items": oimgs}},
                                            },
                                        }
                                    }
                                }
                            }
                        elif ocover:
                            forward_major = {
                                "forward": {
                                    "orig": {
                                        "modules": {
                                            "module_author": {"name": ouname},
                                            "module_dynamic": {
                                                "desc": {"text": otext},
                                                "major": {"archive": {"cover": ocover}},
                                            },
                                        }
                                    }
                                }
                            }

                    # 组装
                    md_block: dict
                    if imgs:
                        md_block = {"major": {"draw": {"items": imgs}}}
                    elif video_cover:
                        md_block = {"major": {"archive": {"cover": video_cover}}}
                    else:
                        md_block = {}

                    built = {
                        "id_str": dynamic_id_str,
                        "modules": {
                            "module_author": {"name": uname},
                            "module_desc": {"text": text_content},
                            "module_dynamic": (forward_major if forward_major else md_block),
                        },
                    }
                    if dynamic_id_str:
                        out.append(built)
                if out:
                    out.sort(key=lambda it: int((it.get("id_str") or "0")), reverse=True)
                    return out[0]
        return None

    # ---------------- HTML 兜底 ----------------
    def _fetch_space_html_latest(self, uid: str) -> Optional[dict]:
        url = f"https://space.bilibili.com/{uid}/dynamic"
        hdr = dict(self._default_headers)
        hdr.update({
            "Referer": f"https://space.bilibili.com/{uid}/",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
        self._log(f"[BilibiliDynamicPush] 🌐 HTML GET {url}")
        status, _, text = self._http_get_with_retry(url, headers=hdr, max_retry=2)
        if status != 200 or not text:
            self._err(f"[BilibiliDynamicPush] HTML 获取失败: {status}")
            return None

        m = re.search(r"__INITIAL_STATE__\s*=\s*(\{.*?\});", text, re.S)
        raw_json = m.group(1) if m else None
        if not raw_json:
            m2 = re.search(r'__INITIAL_STATE__\s*=\s*decodeURIComponent\(\"(.*?)\"\)\s*;', text, re.S)
            if m2:
                try:
                    raw_json = urllib.parse.unquote(m2.group(1))
                except Exception:
                    raw_json = None
        if not raw_json:
            self._err("[BilibiliDynamicPush] HTML 未找到 __INITIAL_STATE__")
            return None

        try:
            j = json.loads(raw_json)
        except Exception:
            self._err("[BilibiliDynamicPush] HTML JSON 解析失败")
            return None

        cand = None
        try:
            arr = _ensure_list(_ensure_dict(_ensure_dict(j.get("dynAll")).get("list")).get("all"))
            cand = arr[0] if arr else None
        except Exception:
            pass
        if not cand:
            try:
                arr = _ensure_list(_ensure_dict(_ensure_dict(j.get("space")).get("res")).get("cardList"))
                cand = arr[0] if arr else None
            except Exception:
                pass
        if not cand:
            self._err("[BilibiliDynamicPush] HTML 结构未识别")
            return None

        dynamic_id = str(_ensure_dict(cand).get("id_str") or _ensure_dict(cand).get("id") or "")
        modules = _ensure_dict(_ensure_dict(cand).get("modules"))
        uname = _ensure_dict(modules.get("module_author")).get("name") \
                or _ensure_dict(cand.get("user")).get("name") or f"UID:{uid}"

        # 优化：使用统一的提取函数，确保视频标题被正确提取（即使在 HTML 结构中）
        text_content = _extract_text_from_desc(modules.get("module_desc"))
        if not text_content:
            md = _ensure_dict(modules.get("module_dynamic"))
            text_content = _extract_text_from_desc(md.get("desc"))
        if not text_content:
            # 兼容无 major 的结构
            md_major = md.get("major") or md
            text_content = _extract_text_from_major(md_major)
        # 额外兜底旧结构
        if not text_content:
            text_content = _ensure_dict(cand.get("item")).get("description") or cand.get("title") or ""

        imgs = []
        md = _ensure_dict(modules.get("module_dynamic"))
        # 兼容无 major
        md_major = md.get("major") or md
        # 图文
        if "draw" in md_major or "dyn_draw" in md_major:
            draw = _ensure_dict(md_major.get("draw") or md_major.get("dyn_draw"))
            for it in _ensure_list(draw.get("items")):
                it = _ensure_dict(it)
                if it.get("src"):
                    imgs.append({"src": it["src"]})
        # 视频封面（HTML 中常见：card.pic / major.archive.cover）
        video_cover = None
        arc = _ensure_dict(md_major.get("archive") or md_major.get("dyn_archive"))
        for key in ("cover", "cover_url", "pic", "dynamic_cover", "first_frame"):
            v = arc.get(key)
            if isinstance(v, str) and v.strip():
                video_cover = v.strip()
                break
        if not video_cover:
            v = _ensure_dict(cand.get("card")).get("pic")
            if isinstance(v, str) and v.strip():
                video_cover = v.strip()

        if not dynamic_id:
            return None

        if imgs:
            md_block = {"major": {"draw": {"items": imgs}}}
        elif video_cover:
            md_block = {"major": {"archive": {"cover": video_cover}}}
        else:
            md_block = {}

        return {
            "id_str": dynamic_id,
            "modules": {
                "module_author": {"name": uname},
                "module_desc": {"text": text_content},
                "module_dynamic": md_block,
            },
        }

    # ---------------- 公共抽取/发送 ----------------
    def _extract_for_display(self, uid: str, dynamic_data: dict):
        mdict = _ensure_dict(dynamic_data)
        modules_raw = mdict.get("modules")
        modules = _normalize_modules(modules_raw)

        author_name = _pick_author_name(modules, uid, resolver=self._resolve_uname)

        module_desc = _ensure_dict(modules.get("module_desc"))
        module_dynamic = _ensure_dict(modules.get("module_dynamic"))
        text_content = _extract_text_from_desc(module_desc, at_resolver=self._resolve_uname)

        if not text_content:
            # 兼容无 major 的结构（如 dyn_archive 直接在 module_dynamic）
            md_major = module_dynamic.get("major") or module_dynamic
            text_content = _extract_text_from_major(md_major, at_resolver=self._resolve_uname)

        # 转发识别：优先 polymer 的 dyn_forward，其次老的 orig
        forward_author, forward_text, forward_imgs = "", "", []
        is_forward = False

        dyn_forward = _ensure_dict(module_dynamic.get("dyn_forward"))
        if dyn_forward:
            item = _ensure_dict(dyn_forward.get("item"))
            if item:
                is_forward = True
                fmods = _normalize_modules(item.get("modules"))
                forward_author = _pick_author_name(fmods, uid, resolver=self._resolve_uname)
                forward_text = _extract_text_from_desc(_ensure_dict(fmods.get("module_desc")), at_resolver=self._resolve_uname)
                if not forward_text:
                    f_md = _ensure_dict(fmods.get("module_dynamic"))
                    f_md_major = f_md.get("major") or f_md
                    forward_text = _extract_text_from_major(f_md_major, at_resolver=self._resolve_uname)
                forward_imgs = _collect_images_from_module_dynamic(_ensure_dict(fmods.get("module_dynamic")))

        if not is_forward:
            orig = _ensure_dict(mdict.get("orig"))
            if orig:
                is_forward = True
                o_modules = _normalize_modules(_ensure_dict(orig.get("modules")))
                forward_author = _pick_author_name(o_modules, uid, resolver=self._resolve_uname)
                o_md = _ensure_dict(o_modules.get("module_dynamic"))
                forward_text = _extract_text_from_desc(_ensure_dict(o_md.get("desc")), at_resolver=self._resolve_uname)
                if not forward_text:
                    o_md_major = o_md.get("major") or o_md
                    forward_text = _extract_text_from_major(o_md_major, at_resolver=self._resolve_uname)
                forward_imgs = _collect_images_from_module_dynamic(o_md)

        # 当前动态的图片/封面（非转发）
        cur_imgs = [] if is_forward else _collect_images_from_module_dynamic(module_dynamic)

        # 如果没有任何文本，给一个合理的占位（图集/视频）
        if not text_content:
            md_major = module_dynamic.get("major") or module_dynamic
            if is_forward and forward_text:
                text_content = ""
            elif ("live" in md_major) or ("dyn_live" in md_major):
                lv = _ensure_dict(md_major.get("live") or md_major.get("dyn_live"))
                ltitle = _sanitize_text(lv.get("title") or "")
                lroom = str(lv.get("room_id") or lv.get("roomid") or "").strip()
                text_content = ("【直播】" + ltitle).strip() if ltitle else "【直播】"
                if lroom:
                    text_content += f"\n直播间：{lroom}"
            elif ("dyn_archive" in md_major) or ("archive" in md_major) or ("pgc" in md_major) or ("dyn_pgc" in md_major) or ("ugc_season" in md_major) or ("dyn_ugc_season" in md_major):
                text_content = "【视频】"
            elif cur_imgs:
                text_content = f"【图集】共 {len(cur_imgs)} 张"
            else:
                text_content = "（无文字内容）"

        MAX_LEN = 1200

        def _clip(t: str) -> str:
            t = _sanitize_text(t or "")
            return (t[:MAX_LEN] + "…") if len(t) > MAX_LEN else t

        dynamic_url = f"https://t.bilibili.com/{mdict.get('id_str', '')}"

        try:
            if _sanitize_text(text_content) in ("", "（无文字内容）"):
                self._dump_module_json(uid, mdict, reason="empty_text_fallback")
        except Exception:
            pass

        return {
            "author_name": author_name,
            "text": _clip(text_content),
            "url": dynamic_url,
            "is_forward": is_forward,
            "forward_author": forward_author,
            "forward_text": _clip(forward_text),
            "images": (forward_imgs if is_forward else cur_imgs),
        }

    # ---------------- 发送封装 ----------------
    def _send_text(self, group_id: str, text: str) -> bool:
        try:
            from asyncio import get_event_loop, new_event_loop, set_event_loop, iscoroutine
            try:
                loop = get_event_loop()
            except RuntimeError:
                loop = new_event_loop()
                set_event_loop(loop)
            coro = send_api.custom_message(
                message_type="text",
                content=text,
                target_id=group_id,
                is_group=True
            )
            ok = loop.run_until_complete(coro) if iscoroutine(coro) else bool(coro)
            return bool(ok)
        except Exception as e:
            self._err(f"[BilibiliDynamicPush] 文本发送失败: {e}")
            return False

    def _download_bytes(self, url: str) -> Optional[bytes]:
        if url.startswith("http://"):
            url = "https://" + url[len("http://"):]
        try:
            req = urllib.request.Request(url, headers=self._default_headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                if getattr(resp, "status", 200) != 200:
                    return None
                data = resp.read()
                return data if data else None
        except Exception:
            return None

    def _bili_url_variants(self, url: str) -> List[str]:
        if not url:
            return []
        if url.startswith("http://"):
            url = "https://" + url[len("http://"):]
        variants = [url]
        base = url.split("?")[0].split("@")[0]
        w = max(320, self.downscale_width)
        lower = base.lower()
        if lower.endswith((".jpg", ".jpeg", ".png", ".webp")):
            stem = base.rsplit(".", 1)[0]
            variants += [
                f"{stem}.jpg@{w}w_1e_1c.jpg",
                f"{stem}.jpg@{w}w_1e_1c.webp",
                f"{base}@{w}w_1e_1c.jpg",
                f"{base}@{w}w_1e_1c.webp",
            ]
        if "imageView2" not in url:
            variants.append(f"{base}?imageView2/2/w/{w}")
        return _unique(variants)

    def _prepare_image_base64(self, url: str) -> Optional[str]:
        """
        下载图片 → 限宽/转 JPG → Base64（不带 base64:// 前缀）
        """
        # 变体尝试
        def _download(u: str) -> Optional[bytes]:
            try:
                req = urllib.request.Request(u, headers=self._default_headers)
                with urllib.request.urlopen(req, timeout=10) as resp:
                    if getattr(resp, "status", 200) != 200:
                        return None
                    return resp.read()
            except Exception:
                return None

        try:
            from PIL import Image
            from io import BytesIO
            has_pillow = True
        except Exception:
            Image = None
            BytesIO = None
            has_pillow = False

        def _to_b64(raw: bytes) -> Optional[str]:
            if not raw:
                return None
            if not has_pillow:
                if len(raw) <= self.base64_chunk_limit:
                    try:
                        return base64.b64encode(raw).decode("utf-8")
                    except Exception:
                        return None
                return None
            try:
                im = Image.open(BytesIO(raw))
                if im.mode not in ("RGB", "L"):
                    im = im.convert("RGB")
                w, h = im.size
                max_w = max(320, self.downscale_width)
                if w > max_w:
                    nh = int(h * (max_w / float(w)))
                    im = im.resize((max_w, nh))
                buf = BytesIO()
                im.save(buf, format="JPEG", quality=self.jpeg_quality, optimize=True)
                data = buf.getvalue()
                if len(data) <= self.base64_chunk_limit:
                    return base64.b64encode(data).decode("utf-8")
            except Exception:
                return None
            return None

        for cu in self._bili_url_variants(url):
            raw = _download(cu)
            if not raw:
                continue
            b64 = _to_b64(raw)
            if b64:
                return b64

        raw = _download(url)
        if raw:
            return _to_b64(raw)
        return None

    def _send_image_with_fallbacks(self, group_id: str, img_url: str) -> bool:
        """按顺序：Base64 → URL → file:/// 兜底；纯 Base64 字符串，不卡前缀。"""
        from asyncio import get_event_loop, new_event_loop, set_event_loop

        # A) Base64 首选（Napcat 最稳）
        if self.force_base64:
            b64 = self._prepare_image_base64(img_url)
            if b64:
                try:
                    try:
                        loop = get_event_loop()
                    except RuntimeError:
                        loop = new_event_loop()
                        set_event_loop(loop)
                    ok = loop.run_until_complete(
                        send_api.custom_message(
                            message_type="image",
                            content=b64,          # ⚠️ 只放纯 Base64，不加 base64://
                            target_id=group_id,
                            is_group=True
                        )
                    )
                    if ok:
                        return True
                except Exception as e:
                    self._err(f"[BilibiliDynamicPush] Base64 发图失败: {e}")

        # B) URL 直发（适配器若支持）
        try:
            try:
                loop = get_event_loop()
            except RuntimeError:
                loop = new_event_loop()
                set_event_loop(loop)
            ok = loop.run_until_complete(
                send_api.custom_message(
                    message_type="image",
                    content=img_url,
                    target_id=group_id,
                    is_group=True
                )
            )
            if ok:
                return True
        except Exception:
            pass

        # C) 落盘 file:/// 再发
        try:
            raw = self._download_bytes(img_url)
            if raw:
                tmpdir = Path(self.get_data_dir()) / "tmp_images"
                tmpdir.mkdir(parents=True, exist_ok=True)
                fname = md5((img_url + str(time.time())).encode("utf-8")).hexdigest() + ".jpg"
                fpath = tmpdir / fname
                with open(fpath, "wb") as f:
                    f.write(raw)
                local_uri = "file:///" + fpath.as_posix()
                try:
                    loop = get_event_loop()
                except RuntimeError:
                    loop = new_event_loop()
                    set_event_loop(loop)
                ok = loop.run_until_complete(
                    send_api.custom_message(
                        message_type="image",
                        content=local_uri,
                        target_id=group_id,
                        is_group=True
                    )
                )
                if ok:
                    return True
        except Exception as e:
            self._err(f"[BilibiliDynamicPush] file:/// 兜底发图失败: {e}")

        return False

    # ---------------- 实际推送 ----------------
    def _push_dynamic(self, uid: str, dynamic_data: dict, groups: List[str]):
        info = self._extract_for_display(uid, dynamic_data)
        author_name = info["author_name"]
        text_content = info["text"]
        dynamic_url = info["url"]
        is_forward = info["is_forward"]
        forward_author = info["forward_author"]
        forward_text = info["forward_text"]
        img_urls: List[str] = info["images"]

        if is_forward:
            header_text = (
                f"🔁 {author_name} 转发了 {forward_author} 的动态：\n"
                f"{text_content}\n"
                f"——原文：{forward_text}\n"
                f"🔗 {dynamic_url}"
            ).strip()
        else:
            header_text = f"📢 {author_name} 发布了新动态：\n{text_content}\n🔗 {dynamic_url}"

        for group_id in groups:
            ok_text = self._send_text(group_id, header_text)
            self._log(f"[BilibiliDynamicPush] → 文本到群 {group_id}: {'OK' if ok_text else 'FAIL'}")

            # 可关图：只发文字与链接
            if not self.send_images or not img_urls:
                continue

            time.sleep(0.8)

            fail_urls = []
            for u in img_urls:
                sent = self._send_image_with_fallbacks(group_id, u)
                if not sent:
                    fail_urls.append(u)
                time.sleep(max(0.5, self.per_image_delay_ms / 1000.0))

            # 最后一层兜底：把失败的图片链接发出来
            if fail_urls:
                links_text = "⚠️ 以下图片发送失败，改为直链：\n" + "\n".join(fail_urls[:10])
                self._send_text(group_id, links_text)

    # ---------------- 调试落盘（支持 output_dir） ----------------
    def _dump_module_json(self, uid: str, item: dict, reason: str = ""):
        try:
            data_dir = getattr(self, "debug_out_dir", None)
            if not isinstance(data_dir, Path):
                try:
                    plugin_dir = Path(__file__).resolve().parent
                except Exception:
                    plugin_dir = Path(".").resolve()
                data_dir = plugin_dir / "debug"
                data_dir.mkdir(parents=True, exist_ok=True)

            dyn_id = str(_ensure_dict(item).get("id_str") or "unknown")
            modules_raw = item.get("modules")
            out = {
                "uid": uid,
                "dynamic_id": dyn_id,
                "reason": reason,
                "modules": modules_raw,
                "module_dynamic": _ensure_dict(_normalize_modules(modules_raw).get("module_dynamic")),
                "orig": _ensure_dict(item).get("orig", {}),
            }
            fp = data_dir / f"debug_module_{uid}_{dyn_id}.json"
            fp.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
            self._log(f"[DEBUG] dump -> {fp}")
        except Exception as e:
            self._err(f"[DEBUG] dump failed: {e}")

    # ---------------- 状态读写 ----------------
    def _load_state(self) -> Dict[str, str]:
        if not self.state_path.exists():
            return {}
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_state(self):
        try:
            self.state_path.write_text(
                json.dumps(self.last_seen, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
        except Exception as e:
            self._err(f"[BilibiliDynamicPush] 状态保存失败: {e}")

    def get_plugin_components(self):
        return []
