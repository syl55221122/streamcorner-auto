#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""StreamCorner 抓取 (Playwright).

策略 (按优先级):
  1. 拦截页面发往 data.*.workers.dev/corner 的 API 请求, 直接拿 JSON
     (比 DOM 抓取更快更稳, 数据结构干净)。
  2. 拦截失败时退化为 DOM 抓取 /stream/<provider>/<id> 卡片。

输出:
  data/items.json  全量条目
  all.m3u          合并订阅 (live 地址指向 M3U_HOST)

环境变量:
  M3U_HOST   默认 127.0.0.1:19926
  OUT_DIR    默认 .
"""
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta

BASE = "https://streamcorner.st"
PROVIDERS = ["paramount", "peacock", "slingtv"]
API_P = {
    "paramount": ["paramount_schedule", "paramount_hero"],
    "peacock": ["peacock_schedule", "peacock_channels"],
    "slingtv": ["slingtv_channels", "slingtv_sports"],
}
M3U_HOST = os.environ.get("M3U_HOST", "127.0.0.1:19926")
OUT_DIR = os.environ.get("OUT_DIR", ".")


def clean(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", s).strip()


def build_m3u(items, host):
    lines = ["#EXTM3U"]
    for it in items:
        name = it["title"].replace(",", "，")
        if it["time"]:
            name = f"{name} ({it['time']})"
            if "LIVE" in it["time"].upper():
                name = "[LIVE] " + name
        url = f"http://{host}/live/{it['provider']}/{it['id']}.m3u8"
        lines.append(f'#EXTINF:-1 group-title="StreamCorner {it["provider"]}",{name}')
        lines.append(url)
    return "\n".join(lines) + "\n"


def main():
    from playwright.sync_api import sync_playwright

    api_data = {}   # p -> json
    captured_payloads = {}  # p -> {"headers":..., "body":...}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"),
        )

        # 诊断: 记录所有请求
        all_reqs = []
        def on_all_req(req):
            u = req.url
            if any(x in u for x in ("workers.dev", "/corner", "api", "schedule", "channel")):
                all_reqs.append(f"{req.method} {u[:150]}")
                print(f"[req] {req.method} {u[:150]}")
        page.on("request", on_all_req)
        console_msgs = []
        page.on("console", lambda m: console_msgs.append(f"{m.type}: {m.text[:200]}"))
        page.on("pageerror", lambda e: console_msgs.append(f"PAGEERROR: {str(e)[:200]}"))

        # 拦截发往 Worker 的请求体 (直调用)
        def on_request(req):
            try:
                if "workers.dev/corner" in req.url:
                    m = re.search(r"[?&]p=([a-z_]+)", req.url)
                    if m and m.group(1) not in captured_payloads:
                        captured_payloads[m.group(1)] = {
                            "method": req.method,
                            "url": req.url,
                            "headers": dict(req.headers),
                            "body": req.post_data or "",
                        }
                        print(f"[payload] captured p={m.group(1)} body={len(req.post_data or '')}B")
            except Exception:
                pass

        page.on("request", on_request)

        # 拦截 API 响应
        def on_response(resp):
            try:
                url = resp.url
                if "workers.dev/corner" in url and resp.status == 200:
                    m = re.search(r"[?&]p=([a-z_]+)", url)
                    if m:
                        api_data[m.group(1)] = resp.json()
                        print(f"[api] captured p={m.group(1)}")
            except Exception:
                pass

        page.on("response", on_response)

        for prov in PROVIDERS:
            try:
                page.goto(f"{BASE}/{prov}", wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(12000)  # 等 Worker 请求完成
                print(f"[{prov}] page loaded")
            except Exception as e:
                print(f"[{prov}] load failed: {e}", file=sys.stderr)

        # 把拦截到的 POST 请求体也记下来 (用于后续直调优化)
        print(f"[diag] interesting requests: {len(all_reqs)}")
        _diag_reqs = list(all_reqs)
        _diag_console = list(console_msgs)
        try:
            _diag_html = page.content()[:50000]
        except Exception:
            _diag_html = ""
        browser.close()

    # 从 API 数据组装条目
    items, seen = [], set()
    for prov in PROVIDERS:
        for p in API_P[prov]:
            data = api_data.get(p)
            if not data:
                continue
            for ch in parse_api_channels(data, prov):
                key = (prov, ch["id"])
                if key in seen:
                    continue
                seen.add(key)
                items.append(ch)

    # API 拦截失败时退化提示 (DOM 抓取需另跑带渲染的版本)
    if not items:
        print("WARNING: no api data captured, items empty", file=sys.stderr)

    data_dir = os.path.join(OUT_DIR, "data")
    os.makedirs(data_dir, exist_ok=True)
    with open(os.path.join(data_dir, "debug_requests.json"), "w", encoding="utf-8") as f:
        json.dump(_diag_reqs, f, ensure_ascii=False, indent=1)
    with open(os.path.join(data_dir, "debug_console.json"), "w", encoding="utf-8") as f:
        json.dump(_diag_console, f, ensure_ascii=False, indent=1)
    with open(os.path.join(data_dir, "debug_html.txt"), "w", encoding="utf-8") as f:
        f.write(_diag_html)
    with open(os.path.join(data_dir, "payload.json"), "w", encoding="utf-8") as f:
        json.dump(captured_payloads, f, ensure_ascii=False, indent=1)
    ts = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    with open(os.path.join(data_dir, "items.json"), "w", encoding="utf-8") as f:
        json.dump({"updated": ts, "count": len(items), "items": items},
                  f, ensure_ascii=False, indent=1)
    with open(os.path.join(OUT_DIR, "all.m3u"), "w", encoding="utf-8") as f:
        f.write(build_m3u(items, M3U_HOST))
    print(f"done: {len(items)} items at {ts}")


def parse_api_channels(data, provider):
    """从 Worker API 的 JSON 里提频道条目, 兼容几种可能的包法."""
    out = []
    if isinstance(data, dict):
        pools = []
        for k in ("channels", "streams", "data", "items", "list"):
            v = data.get(k)
            if isinstance(v, list):
                pools.extend(v)
        if not pools and data:
            pools = [data]
    elif isinstance(data, list):
        pools = data
    else:
        return out
    for c in pools:
        if not isinstance(c, dict):
            continue
        cid = str(c.get("id") or c.get("stream_id") or c.get("channel_id") or "")
        name = clean(str(c.get("name") or c.get("title") or cid))
        if not cid:
            continue
        out.append({
            "provider": provider,
            "id": cid,
            "title": name or cid,
            "time": clean(str(c.get("time") or c.get("start") or "")),
            "page": f"{BASE}/stream/{provider}/{cid}",
        })
    return out


if __name__ == "__main__":
    main()
