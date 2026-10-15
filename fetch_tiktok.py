#!/usr/bin/env python3
"""
用 Playwright 拦截 TikTok hashtag 页面的 API 请求，获取视频列表后用 yt-dlp 下载。

用法：
  python fetch_tiktok.py -n 30
  python fetch_tiktok.py -n 30 --min-views 500000
  python fetch_tiktok.py -n 30 --tags cravo espinhas peleooleosa
"""

import os
import argparse
import asyncio
import json
import subprocess
import time
from pathlib import Path

BASE_DIR = Path(__file__).parent
DEFAULT_OUTPUT = BASE_DIR / "别人的视频"
COOKIES_FILE = BASE_DIR / "tiktok_cookies.txt"
YT_DLP = os.environ.get("YT_DLP", "yt-dlp")

EDGE_PATH = "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"

# 描述/标签中含这些词的视频不适合做带货素材
BLACKLIST_KEYWORDS = [
    # 挤痘娱乐
    "squeeze", "squeezing", "squeezeyuk",
    "pimplepopper", "pimple popper",
    "extraction", "extractions",
    "satisfying", "oddly satisfying",
    "asmr",
    "blackhead removal", "blackheads removal",
    "espremendo", "espremer",
    "extracao", "extração",
    "positivity", "acnepositivity",
    # 美容院专业项目（非居家自用）
    "limpeza de pele", "limpezadepele",
    "peeling", "microagulhamento",
    "procedimento estetico", "procedimento estético",
    "protocolo estetico", "protocolo estético",
    "skincare profissional",
    "esfoliacao profissional", "esfoliação profissional",
]

# 账号名/昵称含这些词视为美容院/诊所账号，直接跳过
SALON_ACCOUNT_PATTERNS = [
    "estetica", "estética", "esteticista",
    "clinic", "clinica", "clínica",
    "aesthetic", "aesthetics",
    "spa", "salao", "salão",
    "dermato", "dermatolog",
    "dra.", "dr.",
]

# 明确拉黑的账号 uniqueId（之前人工审核确认不适合的）
ACCOUNT_BLACKLIST = {
    "sr.espinilla",
    "pimples.squeezee",
    "sacdepspa",
    "majidcosmetics",
    "maxutko.bankii",
}

DEFAULT_TAGS = [
    "peleooleosa",
    "controledeoleosidade",
    "espinhas",
    "acnetreatment",
    "serumacne",
    "tratamentodeacne",
    "acne",
]


def parse_cookies_file(path: Path) -> dict:
    """解析 Netscape 格式 cookies 文件，返回 tiktok.com 的 cookies"""
    cookies = {}
    try:
        for line in path.read_text().splitlines():
            if line.startswith("#") or not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) >= 7 and "tiktok.com" in parts[0]:
                name, value = parts[5], parts[6]
                cookies[name] = value
    except Exception:
        pass
    return cookies


async def fetch_tag_videos_playwright(tag: str, limit: int, min_views: int) -> list[dict]:
    from playwright.async_api import async_playwright

    collected = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path=EDGE_PATH,
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = await browser.new_context(locale="pt-BR")

        if COOKIES_FILE.exists():
            cookies = parse_cookies_file(COOKIES_FILE)
            await ctx.add_cookies([
                {"name": k, "value": v, "domain": ".tiktok.com", "path": "/"}
                for k, v in cookies.items()
            ])

        page = await ctx.new_page()

        # 拦截 challenge/item_list 响应体
        async def on_response(response):
            if "challenge/item_list" not in response.url:
                return
            try:
                data = await response.json()
                for item in data.get("itemList", []):
                    stats = item.get("stats", {})
                    views = stats.get("playCount", 0)
                    likes = stats.get("diggCount", 0)
                    duration = item.get("video", {}).get("duration", 0)
                    vid_id = item.get("id", "")
                    desc = item.get("desc", "")
                    author = item.get("author", {})
                    account_name = (author.get("uniqueId", "") + " " + author.get("nickname", "")).lower()

                    # 基础过滤
                    if not vid_id or views < min_views:
                        continue
                    # 时长 15-60s（最适合带货素材剪辑）
                    if not (15 <= duration <= 60):
                        continue
                    # 最低点赞数（过滤低互动视频）
                    if likes < 5000:
                        continue
                    # 点赞率 >= 1.5%（质量过滤）
                    if views > 0 and likes / views < 0.015:
                        continue
                    # 明确拉黑账号过滤
                    unique_id = author.get("uniqueId", "")
                    if unique_id in ACCOUNT_BLACKLIST:
                        continue
                    # 美容院/诊所账号过滤
                    if any(p in account_name for p in SALON_ACCOUNT_PATTERNS):
                        continue
                    # 黑名单关键词过滤（挤痘娱乐、美容院项目等）
                    desc_lower = desc.lower()
                    if any(kw in desc_lower for kw in BLACKLIST_KEYWORDS):
                        continue
                    # 去重
                    if any(v["id"] == vid_id for v in collected):
                        continue

                    collected.append({
                        "id": vid_id,
                        "views": views,
                        "likes": likes,
                        "like_rate": round(likes / views * 100, 1) if views else 0,
                        "duration": duration,
                        "desc": desc[:60],
                    })
            except Exception:
                pass

        page.on("response", on_response)

        url = f"https://www.tiktok.com/tag/{tag}"
        print(f"  打开 {url} ...", flush=True)
        try:
            await page.goto(url, timeout=30000, wait_until="domcontentloaded")
            await asyncio.sleep(3)
            for _ in range(8):
                await page.evaluate("window.scrollBy(0, 2500)")
                await asyncio.sleep(1.5)
                if len(collected) >= limit * 2:
                    break
        except Exception as e:
            print(f"  页面加载失败: {e}", flush=True)

        await browser.close()

    collected.sort(key=lambda x: x["views"], reverse=True)
    return collected[:limit]


def download_video(video_id: str, output_dir: Path, index: int, total: int) -> bool:
    output_dir.mkdir(exist_ok=True)
    url = f"https://www.tiktok.com/@placeholder/video/{video_id}"
    output_tmpl = str(output_dir / "%(uploader)s_%(id)s.%(ext)s")

    cmd = [
        YT_DLP,
        "--format", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "--merge-output-format", "mp4",
        "--output", output_tmpl,
        "--no-warnings",
        "--retries", "3",
        "--socket-timeout", "30",
        "--cookies", str(COOKIES_FILE),
        url,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode == 0:
        print(f"  [{index}/{total}] ✓ 下载成功", flush=True)
        return True
    else:
        err = (result.stderr or result.stdout)[-300:].strip()
        print(f"  [{index}/{total}] ✗ 失败: {err}", flush=True)
        return False


async def main_async(args):
    tags = args.tags or DEFAULT_TAGS
    per_tag = max(5, args.count // len(tags) + 3)

    all_videos: list[dict] = []
    seen_ids: set[str] = set()

    for tag in tags:
        print(f"\n搜索 #{tag} (目标 {per_tag} 个, 最低 {args.min_views:,} 播放)...", flush=True)
        videos = await fetch_tag_videos_playwright(tag, per_tag, args.min_views)
        added = 0
        for v in videos:
            if v["id"] not in seen_ids:
                seen_ids.add(v["id"])
                all_videos.append(v)
                added += 1
                print(f"    {v['views']:>10,} 播放  👍{v['like_rate']}%  {v['duration']}s  {v['desc']}", flush=True)
        if added == 0:
            print(f"    (无符合条件的视频)", flush=True)

    all_videos.sort(key=lambda x: x["views"], reverse=True)
    selected = all_videos[: args.count]

    print(f"\n共收集 {len(all_videos)} 个，取播放量最高的 {len(selected)} 个下载\n")
    if not selected:
        print("没有符合条件的视频，退出。")
        return

    output_dir = Path(args.output_dir)
    success = fail = 0
    for i, v in enumerate(selected, 1):
        print(f"  [{i}/{len(selected)}] {v['views']:>10,} 播放  👍{v['like_rate']}%  {v['desc']}", end=" ... ", flush=True)
        ok = download_video(v["id"], output_dir, i, len(selected))
        if ok:
            success += 1
        else:
            fail += 1

    print(f"\n=== 完成 === 成功: {success}  失败: {fail}  保存至: {output_dir}")


def main():
    parser = argparse.ArgumentParser(description="从 TikTok 下载巴西爆款视频")
    parser.add_argument("-n", "--count", type=int, default=20, help="下载数量（默认 20）")
    parser.add_argument("--min-views", type=int, default=100000, help="最低播放量（默认 100,000）")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT), help="下载目录")
    parser.add_argument("--tags", nargs="+", help="自定义 hashtag 列表（覆盖默认）")
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
