#!/usr/bin/env python3
"""
从 TikTok 美妆/护肤热门视频中提取 BGM 音轨。

用法：
  python fetch_bgm.py -n 30 --output-dir 脱毛膏/bgm
  python fetch_bgm.py -n 20 --tags beautycare skincare grwm --output-dir 脱毛膏/bgm
"""

import os
import argparse
import asyncio
import json
import subprocess
import tempfile
import time
from pathlib import Path

BASE_DIR = Path(__file__).parent
COOKIES_FILE = BASE_DIR / "tiktok_cookies.txt"
YT_DLP  = os.environ.get("YT_DLP", "yt-dlp")
FFMPEG  = os.environ.get("FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE", "ffprobe")

EDGE_PATH = "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"

MIN_DURATION = 20   # 音频最短时长(秒)
MAX_DURATION = 120  # 音频最长时长(秒)

# 美妆/护肤相关 hashtag，这类视频通常配有流行 BGM
DEFAULT_TAGS = [
    "grwm",
    "skincareroutine",
    "beautycare",
    "makeuptransformation",
    "glowup",
    "hairremoval",
    "depilation",
    "waxing",
]

# 以下关键词说明视频以说话/教程为主，BGM 不突出，跳过
SKIP_KEYWORDS = [
    "tutorial", "how to", "tips", "diy",
    "voiceover", "storytime",
    "asmr",
]


def parse_cookies_file(path: Path) -> dict:
    cookies = {}
    try:
        for line in path.read_text().splitlines():
            if line.startswith("#") or not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) >= 7 and "tiktok.com" in parts[0]:
                cookies[parts[5]] = parts[6]
    except Exception:
        pass
    return cookies


async def fetch_tag_videos(tag: str, limit: int, min_views: int) -> list[dict]:
    from playwright.async_api import async_playwright

    collected = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path=EDGE_PATH,
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = await browser.new_context(locale="en-US")

        if COOKIES_FILE.exists():
            cookies = parse_cookies_file(COOKIES_FILE)
            await ctx.add_cookies([
                {"name": k, "value": v, "domain": ".tiktok.com", "path": "/"}
                for k, v in cookies.items()
            ])

        page = await ctx.new_page()

        async def on_response(response):
            if "challenge/item_list" not in response.url:
                return
            try:
                data = await response.json()
                for item in data.get("itemList", []):
                    stats    = item.get("stats", {})
                    views    = stats.get("playCount", 0)
                    likes    = stats.get("diggCount", 0)
                    duration = item.get("video", {}).get("duration", 0)
                    vid_id   = item.get("id", "")
                    desc     = item.get("desc", "").lower()

                    if not vid_id or views < min_views:
                        continue
                    if not (MIN_DURATION <= duration <= MAX_DURATION):
                        continue
                    if likes < 3000:
                        continue
                    if any(kw in desc for kw in SKIP_KEYWORDS):
                        continue
                    if any(v["id"] == vid_id for v in collected):
                        continue

                    collected.append({
                        "id": vid_id,
                        "views": views,
                        "likes": likes,
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
            for _ in range(10):
                await page.evaluate("window.scrollBy(0, 2500)")
                await asyncio.sleep(1.5)
                if len(collected) >= limit * 2:
                    break
        except Exception as e:
            print(f"  页面加载失败: {e}", flush=True)

        await browser.close()

    collected.sort(key=lambda x: x["views"], reverse=True)
    return collected[:limit]


def get_audio_duration(path: str) -> float:
    cmd = [FFPROBE, "-v", "quiet", "-print_format", "json",
           "-show_format", path]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return float(json.loads(r.stdout)["format"]["duration"])
    except Exception:
        return 0.0


def download_and_extract_audio(video_id: str, output_dir: Path, index: int, total: int) -> bool:
    output_dir.mkdir(parents=True, exist_ok=True)
    url = f"https://www.tiktok.com/@placeholder/video/{video_id}"

    with tempfile.TemporaryDirectory() as tmp:
        tmp_tmpl = str(Path(tmp) / "%(uploader)s_%(id)s.%(ext)s")
        cmd = [
            YT_DLP,
            "--format", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "--merge-output-format", "mp4",
            "--output", tmp_tmpl,
            "--no-warnings",
            "--retries", "3",
            "--socket-timeout", "30",
            "--cookies", str(COOKIES_FILE),
            url,
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            print(f"  [{index}/{total}] ✗ 下载失败", flush=True)
            return False

        # 找到下载的 mp4
        mp4_files = list(Path(tmp).glob("*.mp4"))
        if not mp4_files:
            print(f"  [{index}/{total}] ✗ 找不到 mp4", flush=True)
            return False

        src = mp4_files[0]
        stem = src.stem
        out_mp4 = output_dir / f"{stem}.mp4"

        # 直接复制完整视频（batch_mix 会提取音轨）
        import shutil
        shutil.copy2(str(src), str(out_mp4))

        dur = get_audio_duration(str(out_mp4))
        if dur < MIN_DURATION:
            out_mp4.unlink()
            print(f"  [{index}/{total}] ✗ 时长太短 ({dur:.1f}s)", flush=True)
            return False

        print(f"  [{index}/{total}] ✓ {stem} ({dur:.1f}s)", flush=True)
        return True


async def main_async(args):
    output_dir = Path(args.output_dir)
    if not output_dir.is_absolute():
        output_dir = BASE_DIR / output_dir

    tags = args.tags or DEFAULT_TAGS
    per_tag = max(5, args.count // len(tags) + 3)

    all_videos: list[dict] = []
    seen_ids: set[str] = set()

    for tag in tags:
        print(f"\n搜索 #{tag} ...", flush=True)
        videos = await fetch_tag_videos(tag, per_tag, args.min_views)
        for v in videos:
            if v["id"] not in seen_ids:
                seen_ids.add(v["id"])
                all_videos.append(v)
        print(f"  累计 {len(all_videos)} 个候选", flush=True)

    all_videos.sort(key=lambda x: x["views"], reverse=True)
    to_download = all_videos[:args.count]

    print(f"\n开始下载 {len(to_download)} 个视频并提取音轨...", flush=True)
    ok = 0
    for i, v in enumerate(to_download, 1):
        success = download_and_extract_audio(v["id"], output_dir, i, len(to_download))
        if success:
            ok += 1
        time.sleep(1)

    print(f"\n完成：{ok}/{len(to_download)} 成功，保存到 {output_dir}", flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-n", "--count", type=int, default=30, help="目标数量")
    parser.add_argument("--min-views", type=int, default=200000, help="最低播放量")
    parser.add_argument("--tags", nargs="+", help="搜索标签（不含#）")
    parser.add_argument("--output-dir", default="脱毛膏/bgm", help="输出目录")
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
