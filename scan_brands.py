#!/usr/bin/env python3
"""
扫描"别人的视频"目录中所有视频，用 OCR 检测竞品品牌文字出现的时间段。
结果缓存到 brand_segments.json，供 batch_mix.py 使用。

用法：
  python scan_brands.py              # 扫描全部（跳过已缓存的）
  python scan_brands.py --force      # 强制重新扫描全部
  python scan_brands.py --test       # 只扫描第一个视频，打印检测结果
"""

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path

import easyocr

BASE_DIR = Path(__file__).parent
OTHERS_DIR = BASE_DIR / "别人的视频"
CACHE_FILE = BASE_DIR / "brand_segments.json"
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")

# 竞品品牌关键词（全部小写，做模糊匹配）
BRAND_KEYWORDS = [
    "acnezil", "althea", "anua", "purito", "cerave", "la roche",
    "de la cruz", "neutrogena", "vichy", "bioderma", "eucerin",
    "clinique", "estee lauder", "loreal", "l'oreal", "garnier",
    "nivea", "dove", "pond", "olay", "aveeno", "dermalogica",
    "paula's choice", "the ordinary", "drunk elephant",
    "ebla", "principia", "profuse", "acnezap",
    "ln skin", "lnskin", "acnezil", "acnecide",
    "needs", "needs face",
]

# 每隔多少秒抽一帧（越小越精准但越慢）
SAMPLE_INTERVAL = 1.0
# OCR 置信度阈值
CONF_THRESHOLD = 0.5
# 相邻检测帧合并的间隔（秒）
MERGE_GAP = 2.0
# 检测到竞品后，在该段前后各扩展多少秒（避免边界遗漏）
EXPAND_SEC = 0.5


def extract_frames(video_path: str, interval: float, tmp_dir: str) -> list[tuple[float, str]]:
    """每隔 interval 秒提取一帧，返回 [(timestamp, frame_path), ...]"""
    # 获取视频时长
    r = subprocess.run(
        [os.environ.get("FFPROBE", "ffprobe"),
         "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=duration",
         "-of", "default=noprint_wrappers=1:nokey=1",
         video_path],
        capture_output=True, text=True
    )
    try:
        duration = float(r.stdout.strip())
    except Exception:
        return []

    frames = []
    t = 0.0
    while t < duration:
        frame_path = os.path.join(tmp_dir, f"frame_{t:.2f}.jpg")
        cmd = [
            FFMPEG, "-y", "-ss", f"{t:.2f}", "-i", video_path,
            "-vframes", "1", "-q:v", "3",
            "-vf", "scale=480:-1",  # 缩小提高 OCR 速度
            frame_path
        ]
        subprocess.run(cmd, capture_output=True, timeout=10)
        if os.path.exists(frame_path):
            frames.append((t, frame_path))
        t += interval

    return frames


def detect_brands_in_frame(reader, frame_path: str) -> bool:
    """返回该帧是否含竞品品牌文字"""
    try:
        results = reader.readtext(frame_path, detail=1)
        for (_, text, conf) in results:
            if conf < CONF_THRESHOLD:
                continue
            text_lower = text.lower().strip()
            for kw in BRAND_KEYWORDS:
                if kw in text_lower:
                    return True, text
    except Exception:
        pass
    return False, None


def merge_segments(timestamps: list[float], expand: float, merge_gap: float) -> list[list[float]]:
    """把离散的时间点合并成 [start, end] 区间列表"""
    if not timestamps:
        return []
    segs = []
    cur_start = timestamps[0] - expand
    cur_end = timestamps[0] + expand
    for t in timestamps[1:]:
        t_start = t - expand
        t_end = t + expand
        if t_start <= cur_end + merge_gap:
            cur_end = max(cur_end, t_end)
        else:
            segs.append([max(0, cur_start), cur_end])
            cur_start, cur_end = t_start, t_end
    segs.append([max(0, cur_start), cur_end])
    return segs


def scan_video(reader, video_path: str, verbose: bool = False) -> list[list[float]]:
    """扫描单个视频，返回竞品出现的时间段列表"""
    with tempfile.TemporaryDirectory(prefix="scan_") as tmp_dir:
        frames = extract_frames(video_path, SAMPLE_INTERVAL, tmp_dir)
        if not frames:
            return []

        hit_times = []
        for t, fp in frames:
            found, text = detect_brands_in_frame(reader, fp)
            if found:
                hit_times.append(t)
                if verbose:
                    print(f"    t={t:.1f}s  检测到: {text}", flush=True)

        return merge_segments(hit_times, EXPAND_SEC, MERGE_GAP)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="强制重新扫描")
    parser.add_argument("--test", action="store_true", help="只扫描第一个视频")
    args = parser.parse_args()

    # 加载已有缓存
    cache = {}
    if CACHE_FILE.exists() and not args.force:
        with open(CACHE_FILE) as f:
            cache = json.load(f)

    # 初始化 OCR（支持葡语+英语）
    print("初始化 EasyOCR...", flush=True)
    reader = easyocr.Reader(["pt", "en"], gpu=False)

    videos = sorted([
        f for f in OTHERS_DIR.iterdir()
        if f.suffix.lower() in (".mp4", ".mov", ".avi", ".mkv")
    ])

    if args.test:
        videos = videos[:1]

    for video in videos:
        name = video.name
        if name in cache and not args.force:
            print(f"  [跳过] {name}  (已缓存: {len(cache[name])} 段)", flush=True)
            continue

        print(f"  扫描: {name} ...", end=" ", flush=True)
        segs = scan_video(reader, str(video), verbose=args.test)
        cache[name] = segs

        if segs:
            seg_str = ", ".join(f"{s[0]:.1f}-{s[1]:.1f}s" for s in segs)
            print(f"发现 {len(segs)} 段竞品: [{seg_str}]", flush=True)
        else:
            print("干净", flush=True)

        # 每扫完一个就保存，避免中途中断丢失
        with open(CACHE_FILE, "w") as f:
            json.dump(cache, f, indent=2, ensure_ascii=False)

    print(f"\n完成，结果保存至 {CACHE_FILE}")


if __name__ == "__main__":
    main()
