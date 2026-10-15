#!/usr/bin/env python3
"""
用 Gemini 2.0 Flash（via OpenRouter）检测别人视频的 before/after 区间。

用法：
  python classify_zones_vlm.py --base-dir 脱毛膏
  python classify_zones_vlm.py --base-dir 脱毛膏 --force
  python classify_zones_vlm.py --base-dir 脱毛膏 --test   # 只扫第一个视频

输出：{base-dir}/source_classify.json
  格式：{ "video.mp4": { "before": [0.0, 0.4], "after": [0.6, 1.0] }, ... }
  比例范围 0~1，基于视频时长。
"""

import argparse
import base64
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

from openai import OpenAI

FFMPEG  = os.environ.get("FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE", "ffprobe")

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
MODEL = "meta-llama/llama-3.2-11b-vision-instruct"

SAMPLE_INTERVAL = 2.0   # 每隔多少秒抽一帧
MERGE_GAP       = 3.0   # 合并相邻段的间隔(秒)
EXPAND          = 1.0   # 检测到的帧前后扩展(秒)
MIN_SEG_DUR     = 2.0   # 最短保留段长度(秒)

SYSTEM_PROMPTS = {
    "toothpaste": """你是一个视频帧分类助手，专门分析牙膏/美白牙齿相关视频。

对于每一帧图片，判断它属于以下哪个类别：
- before：画面中可以看到牙齿发黄、有污渍、不白净（使用牙膏前的状态）
- after：画面中可以看到牙齿洁白、明亮、干净（使用后的效果）
- neutral：刷牙过程、漱口、说话、人脸特写、产品展示等其他画面

只回复一个单词：before、after 或 neutral。不要解释。""",

    "depilatory": """你是一个视频帧分类助手，专门分析脱毛相关视频。

对于每一帧图片，判断它属于以下哪个类别：
- before：画面中可以明显看到腿部、手臂或其他身体部位有毛发（脱毛前的状态）
- after：画面中可以看到光滑无毛的皮肤（脱毛后的效果）
- neutral：涂抹脱毛膏的过程、等待、清洗、人脸特写、说话、产品展示等其他画面

只回复一个单词：before、after 或 neutral。不要解释。""",
}

SYSTEM_PROMPT = SYSTEM_PROMPTS["depilatory"]  # 默认，运行时会被覆盖


client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
)


def get_video_duration(video_path):
    cmd = [FFPROBE, "-v", "quiet", "-print_format", "json", "-show_format", str(video_path)]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    return float(json.loads(result.stdout)["format"]["duration"])


def detect_black_segments(video_path, duration):
    """返回黑帧时间段列表 [(start, end), ...]"""
    cmd = [
        FFMPEG, "-i", str(video_path),
        "-vf", "blackdetect=d=0.3:pix_th=0.10",
        "-an", "-f", "null", "-"
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    segs = []
    for line in result.stderr.splitlines():
        if "black_start" in line:
            try:
                bs = float(line.split("black_start:")[1].split()[0])
                be = float(line.split("black_end:")[1].split()[0])
                segs.append((bs, be))
            except Exception:
                pass
    return segs


def is_black(t, black_segs):
    for bs, be in black_segs:
        if bs <= t <= be:
            return True
    return False


def extract_frame(video_path, t, tmp_dir):
    fp = os.path.join(tmp_dir, f"frame_{t:.1f}.jpg")
    subprocess.run([
        FFMPEG, "-y", "-ss", f"{t:.2f}", "-i", str(video_path),
        "-vframes", "1", "-q:v", "4",
        "-vf", "scale=512:512:force_original_aspect_ratio=decrease,pad=512:512:(ow-iw)/2:(oh-ih)/2",
        fp
    ], capture_output=True, timeout=10)
    return fp if os.path.exists(fp) else None


def classify_frame(image_path, retries=3):
    with open(image_path, "rb") as f:
        b64 = base64.standard_b64encode(f.read()).decode("utf-8")

    for attempt in range(retries):
        try:
            resp = client.chat.completions.create(
                model=MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                        {"type": "text", "text": "这一帧属于哪个类别？"}
                    ]}
                ],
                max_tokens=10,
                temperature=0,
            )
            label = resp.choices[0].message.content.strip().lower()
            if "before" in label:
                return "before"
            elif "after" in label:
                return "after"
            else:
                return "neutral"
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2)
            else:
                print(f"[API错误] {e}", flush=True)
                return "neutral"


def merge_segments(timestamps, expand, merge_gap, duration, min_dur):
    if not timestamps:
        return []
    segs = []
    cur_start = max(0.0, timestamps[0] - expand)
    cur_end   = min(duration, timestamps[0] + expand)
    for t in timestamps[1:]:
        if t - expand <= cur_end + merge_gap:
            cur_end = min(duration, max(cur_end, t + expand))
        else:
            if cur_end - cur_start >= min_dur:
                segs.append([round(cur_start, 1), round(cur_end, 1)])
            cur_start = max(0.0, t - expand)
            cur_end   = min(duration, t + expand)
    if cur_end - cur_start >= min_dur:
        segs.append([round(cur_start, 1), round(cur_end, 1)])
    return segs


def classify_video(video_path):
    duration = get_video_duration(video_path)
    black_segs = detect_black_segments(video_path, duration)
    before_times = []
    after_times  = []

    with tempfile.TemporaryDirectory() as tmp:
        t = 0.0
        while t < duration:
            if is_black(t, black_segs):
                t += SAMPLE_INTERVAL
                continue
            fp = extract_frame(video_path, t, tmp)
            if fp:
                label = classify_frame(fp)
                if label == "before":
                    before_times.append(t)
                elif label == "after":
                    after_times.append(t)
            t += SAMPLE_INTERVAL

    before_segs = merge_segments(before_times, EXPAND, MERGE_GAP, duration, MIN_SEG_DUR)
    after_segs  = merge_segments(after_times,  EXPAND, MERGE_GAP, duration, MIN_SEG_DUR)

    def to_ratio(segs):
        return [[round(s[0] / duration, 3), round(s[1] / duration, 3)] for s in segs]

    return to_ratio(before_segs), to_ratio(after_segs), duration


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-dir", required=True)
    parser.add_argument("--product-type", default="depilatory",
                        choices=list(SYSTEM_PROMPTS.keys()),
                        help="产品类型，决定使用哪组提示词")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--test",  action="store_true", help="只扫第一个视频")
    args = parser.parse_args()

    global SYSTEM_PROMPT
    SYSTEM_PROMPT = SYSTEM_PROMPTS[args.product_type]

    base = Path(args.base_dir)
    if not base.is_absolute():
        base = Path(__file__).parent / base

    others_dir    = base / "别人的视频"
    classify_file = base / "source_classify.json"

    # 加载缓存
    cache = {}
    if classify_file.exists() and not args.force:
        raw = json.load(open(classify_file))
        cache = {k: v for k, v in raw.items() if not k.startswith("_")}

    videos = sorted(others_dir.iterdir())
    videos = [v for v in videos if v.suffix.lower() in ('.mp4', '.mov', '.avi', '.mkv')]
    if args.test:
        videos = videos[:1]

    print(f"共 {len(videos)} 个视频，使用 {MODEL}", flush=True)

    for i, v in enumerate(videos, 1):
        if v.name in cache and not args.force:
            print(f"  [{i}/{len(videos)}] 跳过(已缓存): {v.name}", flush=True)
            continue

        duration = get_video_duration(v)
        n_frames = int(duration / SAMPLE_INTERVAL) + 1
        print(f"  [{i}/{len(videos)}] {v.name} ({duration:.1f}s, ~{n_frames}帧) ...", flush=True)

        try:
            before_segs, after_segs, dur = classify_video(str(v))
        except Exception as e:
            print(f"    ERROR: {e}", flush=True)
            continue

        entry = {}
        if before_segs:
            entry["before"] = before_segs[0]
        if after_segs:
            entry["after"] = after_segs[-1]
        if not entry:
            entry["before"] = [0.0, 1.0]
            entry["note"] = "未检测到明显before/after，全段作为before"

        cache[v.name] = entry

        out = {"_说明": "before/after 是视频时长的比例范围，由 Gemini 2.0 Flash 自动标注"}
        out.update(cache)
        with open(classify_file, "w") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)

        parts = []
        if "before" in entry:
            parts.append(f"before={entry['before']}")
        if "after" in entry:
            parts.append(f"after={entry['after']}")
        print(f"    {'  '.join(parts) or entry.get('note','')}", flush=True)

    print(f"\n完成 → {classify_file}", flush=True)


if __name__ == "__main__":
    main()
