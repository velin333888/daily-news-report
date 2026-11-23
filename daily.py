#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""云端 AI 日报：抓取 → 筛选 → 摘要 → 推送飞书

设计要点
- 只用标准库，无需 pip install
- 无 LLM Key 时自动降级为「标题 + 原文摘要」模式，不会整体失败
- 推送失败不影响退出码统计，但会打印原因
- 去重历史存 state/history.json，随仓库一起提交

环境变量
    FEISHU_WEBHOOK   必填，飞书群机器人 Webhook
    FEISHU_SECRET    选填，开启签名校验时填
    LLM_API_KEY      选填，OpenAI 兼容接口的 Key
    LLM_BASE_URL     选填，默认 https://api.deepseek.com/v1
    LLM_MODEL        选填，默认 deepseek-chat
    MAX_ITEMS        选填，默认 5
    DEDUP_DAYS       选填，默认 10
    DRY_RUN          选填，设为 1 则只打印不发送
"""
import base64
import hashlib
import hmac
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))
ROOT = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(ROOT, "state", "history.json")

SOURCES = [
    ("量子位", "https://www.qbitai.com/feed/", 1.0),
    ("IT之家", "https://www.ithome.com/rss/", 1.0),
    ("钛媒体", "https://www.tmtpost.com/rss", 0.5),
    ("Google AI", "https://blog.google/technology/ai/rss/", 1.0),
    ("arXiv cs.RO", "http://export.arxiv.org/rss/cs.RO", 1.0),
    ("arXiv cs.SE", "http://export.arxiv.org/rss/cs.SE", 0.8),
    ("OpenAI", "https://openai.com/news/rss.xml", 1.0),
    ("Hacker News", "https://news.ycombinator.com/rss", 0.6),
    ("MIT Tech Review", "https://www.technologyreview.com/feed/", 0.8),
    ("Simon Willison", "https://simonwillison.net/atom/everything/", 0.7),
]

# 关键词权重：命中即加分，负数用于降权
WEIGHTS = {
    # 具身智能
    "具身智能": 6, "人形机器人": 6, "机器人": 3, "灵巧手": 4, "VLA": 5,
    "世界模型": 5, "遥操作": 3, "宇树": 4, "智元": 4, "Optimus": 4,
    "humanoid": 4, "embodied": 5, "manipulation": 3, "robot": 2,
    "locomotion": 3, "teleoperation": 3, "dexterous": 4,
    # AI coding
    "AI 编程": 6, "编码智能体": 6, "Claude Code": 6, "Cursor": 5,
    "Copilot": 5, "Codex": 5, "编程": 3, "代码": 2, "IDE": 3,
    "coding agent": 6, "code generation": 4, "SWE-bench": 5,
    "agents.md": 4, "MCP": 4,
    # 通用但重要
    "大模型": 3, "开源": 2, "智能体": 3, "Agent": 2, "发布会": -1,
    "融资": 2, "开源模型": 3, "基准": 2, "benchmark": 2,
}

UA = "Mozilla/5.0 (compatible; AIDailyBot/1.0)"


def log(msg):
    print(msg, flush=True)


def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def http_post_json(url, payload, timeout=25):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json; charset=utf-8"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def strip_tags(s):
    s = re.sub(r"<[^>]+>", "", s or "")
    return html.unescape(s).strip()


def parse_feed(raw):
    """兼容 RSS 2.0 / Atom，返回 [{title, link, summary, ts}]"""
    out = []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return out

    def txt(node, *names):
        for name in names:
            el = node.find(name)
            if el is not None and (el.text or "").strip():
                return strip_tags(el.text)
            # Atom: <link href="..."/>
            if el is not None and el.get("href"):
                return el.get("href")
        return ""

    for node in root.iter():
        tag = node.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        title = txt(node, "title")
        link = txt(node, "link")
        summary = txt(node, "description", "summary", "content")
        date_s = txt(node, "pubDate", "published", "updated", "date")
        ts = None
        for fmt in (
            "%a, %d %b %Y %H:%M:%S %z",
            "%a, %d %b %Y %H:%M:%S %Z",
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%d %H:%M:%S",
        ):
            try:
                dt = datetime.strptime(date_s, fmt)
                ts = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
                break
            except (ValueError, TypeError):
                continue
        if title:
            out.append({"title": title, "link": link, "summary": summary[:600], "ts": ts})
    return out


def clean_title(title):
    # arXiv 的标题尾部噪音
    title = re.sub(r"\.?\s*\(arXiv:[\d.]+v?\d*\s*\[[^\]]+\]\)\s*$", "", title)
    return re.sub(r"\s+", " ", title).strip()


def clean_summary(text):
    """去掉 arXiv 的 'arXiv:xxxx Announce Type: new Abstract:' 前缀等噪音。"""
    if not text:
        return ""
    # arXiv: 只保留 Abstract 之后的部分
    m = re.search(r"Abstract:\s*", text)
    if m:
        text = text[m.end():]
    text = re.sub(r"^arXiv:[\d.]+v?\d*\s*", "", text)
    text = re.sub(r"Announce Type:\s*\S+\s*", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def brief(text, limit=150):
    """按字符截断，英文尽量断在空格处。"""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if " " in cut[-12:]:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip("，,、;；:：") + "…"


def score(item):
    text = (item["title"] + " " + item.get("summary", "")).lower()
    total = 0
    for kw, w in WEIGHTS.items():
        if kw.lower() in text:
            total += w
    if item.get("ts"):
        age_h = (datetime.now(timezone.utc) - item["ts"]).total_seconds() / 3600
        total += 3 if age_h <= 12 else (1.5 if age_h <= 36 else 0)
    return total * item.get("_src_weight", 1.0)


def load_history():
    if not os.path.exists(STATE):
        return {}
    try:
        with open(STATE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_history(hist, days):
    cutoff = (datetime.now(CST) - timedelta(days=days)).strftime("%Y-%m-%d")
    hist = {k: v for k, v in hist.items() if v >= cutoff}
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False, indent=2)


def hkey(title):
    norm = re.sub(r"[^\w\u4e00-\u9fff]+", "", title.lower())
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]


def collect():
    items = []
    for name, url, weight in SOURCES:
        try:
            raw = http_get(url)
            parsed = parse_feed(raw)
            log(f"[fetch] {name}: {len(parsed)} 条")
            for it in parsed:
                it["source"] = name
                it["_src_weight"] = weight
                it["title"] = clean_title(it["title"])
                it["summary"] = clean_summary(it.get("summary", ""))
                items.append(it)
        except Exception as exc:
            log(f"[fetch] {name}: 失败 {exc}")
    return items


def env_str(name, default=""):
    """GitHub Actions 里未定义的 vars 会注入空字符串，必须用 or 兜底。"""
    return (os.environ.get(name) or "").strip() or default


def env_int(name, default):
    try:
        return int(env_str(name, str(default)))
    except ValueError:
        return default


def llm_summarize(candidates, max_items):
    base = env_str("LLM_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
    model = env_str("LLM_MODEL", "deepseek-chat")
    key = env_str("LLM_API_KEY")
    if not key:
        return None

    today = datetime.now(CST).strftime("%Y-%m-%d")
    listing = "\n".join(
        f"{i+1}. [{c['source']}] {c['title']} :: {c.get('summary','')[:220]}"
        for i, c in enumerate(candidates)
    )
    prompt = f"""你是 AI 领域的技术编辑。今天是 {today}。

下面是过去 36 小时抓到的候选新闻（含来源与摘要）：
{listing}

请从中筛选 {max_items} 条最有价值的信息，要求：
1. 优先 AI coding（编码智能体、编程工具、模型编码能力）与具身智能（人形机器人、VLA、世界模型、训练方法）两个方向
2. 必须是实质性进展（发布、论文、量产、政策、标准、重大融资），不要产品营销稿、不要纯概念讨论
3. 同一主题只选一条
4. 优先选择"有具体数字或可核事实"的条目

只输出 JSON，不要任何解释文字，格式：
{{"items":[{{"title":"不超过20字的中文标题","event":"2-3句话说明发生了什么，中文","why":"1-2句话说明为什么值得关注，中文，要有判断而不是复述"}}]}}"""

    def _call(use_json_mode):
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
        }
        if use_json_mode:
            payload["response_format"] = {"type": "json_object"}
        req = urllib.request.Request(
            f"{base}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
            },
        )
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode("utf-8"))

    def _extract(text):
        text = re.sub(r"```(?:json)?", "", text).strip()
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            text = text[start : end + 1]
        return json.loads(text).get("items", [])[:max_items]

    last_err = None
    # 先带 response_format 试，多数 OpenAI 兼容接口支持；不支持则退回纯文本 JSON
    for use_json_mode in (True, False):
        try:
            body = _call(use_json_mode)
            items = _extract(body["choices"][0]["message"]["content"].strip())
            if items:
                usage = body.get("usage") or {}
                log(
                    f"[llm] {model} 返回 {len(items)} 条 (json_mode={use_json_mode}, "
                    f"in={usage.get('prompt_tokens')} out={usage.get('completion_tokens')})"
                )
                return items
            last_err = "模型返回空列表"
        except Exception as exc:
            last_err = exc
            log(f"[llm] 尝试失败 (json_mode={use_json_mode}): {exc}")

    log(f"[llm] 全部失败，降级为标题模式: {last_err}")
    return None


def fallback_items(candidates, max_items):
    # 降级时优先保证来源多样：先每源取 1 条，不够再补
    picked, used = [], set()
    for c in candidates:
        if c["source"] in used:
            continue
        used.add(c["source"])
        picked.append(c)
        if len(picked) >= max_items:
            break
    if len(picked) < max_items:
        for c in candidates:
            if c in picked:
                continue
            picked.append(c)
            if len(picked) >= max_items:
                break

    out = []
    for c in picked:
        summary = c.get("summary", "").strip()
        out.append(
            {
                "title": brief(c["title"], 34),
                "event": brief(summary, 150) if summary else f"来源：{c['source']}",
                "why": "",
                "_link": c.get("link", ""),
            }
        )
    return out


def render(items, degraded):
    today = datetime.now(CST)
    lines = [f"**AI 日报 · {today.strftime('%m/%d')}**", ""]
    if degraded:
        lines.append("_（未配置模型 Key，降级为标题速览）_")
        lines.append("")
    for i, it in enumerate(items, 1):
        lines.append(f"**{i}. {it['title']}**")
        lines.append(it.get("event", ""))
        if it.get("why"):
            lines.append(f"→ {it['why']}")
        lines.append("")
    return "\n".join(lines).strip()


def feishu_sign(secret, ts):
    key = f"{ts}\n{secret}".encode("utf-8")
    return base64.b64encode(hmac.new(key, digestmod=hashlib.sha256).digest()).decode()


def push_feishu(markdown, title):
    if os.environ.get("DRY_RUN") == "1":
        log("[push] DRY_RUN=1，不发送。内容预览：")
        log("-" * 40)
        log(markdown)
        log("-" * 40)
        return True

    url = os.environ.get("FEISHU_WEBHOOK", "").strip()
    if not url:
        log("[push] FEISHU_WEBHOOK 未配置，跳过发送")
        return False

    payload = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "blue",
            },
            "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": markdown}}],
        },
    }
    secret = os.environ.get("FEISHU_SECRET", "").strip()
    if secret:
        ts = str(int(time.time()))
        payload["timestamp"] = ts
        payload["sign"] = feishu_sign(secret, ts)

    try:
        resp = http_post_json(url, payload)
        log(f"[push] {resp}")
        return json.loads(resp).get("code", -1) == 0
    except urllib.error.HTTPError as exc:
        log(f"[push] HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')}")
    except Exception as exc:
        log(f"[push] 失败: {exc}")
    return False


def preflight():
    """开机自检：只打印是否配置，不泄露任何密钥。"""
    def state(name):
        return "✅ 已配置" if os.environ.get(name, "").strip() else "❌ 未配置"

    log(f"[config] FEISHU_WEBHOOK {state('FEISHU_WEBHOOK')}")
    log(f"[config] FEISHU_SECRET {state('FEISHU_SECRET')}")
    log(f"[config] LLM_API_KEY  {state('LLM_API_KEY')}")
    base = os.environ.get("LLM_BASE_URL", "").strip() or "https://api.deepseek.com/v1（默认）"
    model = os.environ.get("LLM_MODEL", "").strip() or "deepseek-chat（默认）"
    log(f"[config] base={base} model={model}")


def main():
    max_items = env_int("MAX_ITEMS", 5)
    dedup_days = env_int("DEDUP_DAYS", 10)

    preflight()

    items = collect()
    if not items:
        log("[error] 没有抓到任何条目，检查网络或源地址")
        return 1

    hist = load_history()
    fresh, seen = [], set()
    for it in items:
        k = hkey(it["title"])
        if k in hist or k in seen:
            continue
        seen.add(k)
        if it.get("ts"):
            age_h = (datetime.now(timezone.utc) - it["ts"]).total_seconds() / 3600
            if age_h > 36:
                continue
        fresh.append(it)

    log(f"[filter] 去重后 {len(fresh)} 条候选")
    if not fresh:
        log("[warn] 全部命中去重或超时，今日无新内容")
        return 0

    fresh.sort(key=score, reverse=True)

    # 每源最多 4 条进候选池，避免 arXiv 这类高产源把版面吃光
    capped, counts = [], {}
    for it in fresh:
        c = counts.get(it["source"], 0)
        if c >= 4:
            continue
        counts[it["source"]] = c + 1
        capped.append(it)
    candidates = capped[:40]

    picked = llm_summarize(candidates, max_items)
    degraded = picked is None
    if degraded:
        picked = fallback_items(candidates, max_items)

    md = render(picked, degraded)
    title = f"AI 日报 · {datetime.now(CST).strftime('%m/%d')}"

    today = datetime.now(CST).strftime("%Y-%m-%d")
    for c in candidates[: max_items * 3]:
        hist[hkey(c["title"])] = today
    save_history(hist, dedup_days)

    ok = push_feishu(md, title)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
