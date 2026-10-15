#!/usr/bin/env python3
"""
为指定目录下的所有视频生成缩略图联系表
每个视频抽3帧（开头/中间/结尾），拼成一张大图便于快速筛查
用法：
  python make_contact_sheet.py                          # 默认扫 别人的视频/
  python make_contact_sheet.py --dir 牙膏/别人的视频
  python make_contact_sheet.py --dir 别人的视频 --out review.png
"""

import os
import sys
import json
import subprocess
import tempfile
import argparse
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

FFMPEG  = os.environ.get("FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE", "ffprobe")

BASE_DIR = Path(__file__).parent

THUMB_W   = 320   # 每帧缩略图宽度
THUMB_H   = 568   # 9:16 高度
FRAMES_N  = 3     # 每个视频抽几帧
LABEL_H   = 36    # 文件名标签高度
ROW_PAD   = 8     # 行间距
COL_PAD   = 6     # 帧间距


def get_duration(path):
    cmd = [FFPROBE, "-v", "quiet", "-print_format", "json",
           "-show_format", str(path)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    try:
        return float(json.loads(r.stdout)["format"]["duration"])
    except Exception:
        return None


def extract_frame(path, ts, out_path):
    cmd = [
        FFMPEG, "-y",
        "-ss", f"{ts:.2f}",
        "-i", str(path),
        "-vframes", "1",
        "-vf", f"scale={THUMB_W}:{THUMB_H}:force_original_aspect_ratio=increase,crop={THUMB_W}:{THUMB_H}",
        "-q:v", "3",
        out_path,
    ]
    r = subprocess.run(cmd, capture_output=True, timeout=20)
    return r.returncode == 0


def make_row(video_path, tmp_dir):
    """返回一张横条图（label + N帧）"""
    dur = get_duration(video_path)
    if dur is None or dur < 0.5:
        return None

    # 抽帧时间点：10% / 50% / 85%
    ratios = [0.10, 0.50, 0.85] if FRAMES_N == 3 else \
             [i / (FRAMES_N - 1) for i in range(FRAMES_N)]
    timestamps = [max(0.0, min(r * dur, dur - 0.1)) for r in ratios]

    frames = []
    for i, ts in enumerate(timestamps):
        out = os.path.join(tmp_dir, f"frame_{i}.jpg")
        if extract_frame(video_path, ts, out):
            try:
                frames.append(Image.open(out).copy())
            except Exception:
                pass

    if not frames:
        return None

    # 拼这一行：label 在左，帧依次排列
    row_w = len(frames) * (THUMB_W + COL_PAD) - COL_PAD
    row_h = THUMB_H + LABEL_H

    row_img = Image.new("RGB", (row_w, row_h), (30, 30, 30))
    draw = ImageDraw.Draw(row_img)

    # 贴帧
    for i, frame in enumerate(frames):
        x = i * (THUMB_W + COL_PAD)
        row_img.paste(frame.resize((THUMB_W, THUMB_H)), (x, LABEL_H))
        # 时间戳水印
        ts_text = f"{timestamps[i]:.1f}s"
        draw.text((x + 4, LABEL_H + 4), ts_text, fill=(255, 255, 100))

    # 文件名标签
    name = Path(video_path).stem[:60]
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 20)
    except Exception:
        font = ImageFont.load_default()
    draw.text((6, 6), f"{name}  ({dur:.1f}s)", fill=(255, 255, 255), font=font)

    return row_img


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", default=None, help="视频目录（默认：别人的视频）")
    parser.add_argument("--out", default=None, help="输出图片路径（默认：contact_sheet.png）")
    args = parser.parse_args()

    video_dir = Path(args.dir) if args.dir else BASE_DIR / "别人的视频"
    if not video_dir.is_absolute():
        video_dir = BASE_DIR / video_dir

    out_path = Path(args.out) if args.out else BASE_DIR / "contact_sheet.png"

    video_files = sorted([
        f for f in video_dir.iterdir()
        if f.suffix.lower() in ('.mp4', '.mov', '.avi', '.mkv')
    ])

    if not video_files:
        print(f"没有找到视频文件：{video_dir}")
        sys.exit(1)

    print(f"共 {len(video_files)} 个视频，开始生成联系表...", flush=True)

    rows = []
    with tempfile.TemporaryDirectory(prefix="contact_") as tmp_dir:
        for i, vf in enumerate(video_files, 1):
            print(f"  [{i}/{len(video_files)}] {vf.name}", flush=True)
            row = make_row(str(vf), tmp_dir)
            if row:
                rows.append(row)

    if not rows:
        print("没有成功生成任何行，请检查视频文件。")
        sys.exit(1)

    # 拼接所有行
    total_w = max(r.width for r in rows)
    total_h = sum(r.height for r in rows) + ROW_PAD * (len(rows) - 1)
    sheet = Image.new("RGB", (total_w, total_h), (15, 15, 15))

    y = 0
    for row in rows:
        sheet.paste(row, (0, y))
        y += row.height + ROW_PAD

    sheet.save(str(out_path), quality=85)
    print(f"\n联系表已保存：{out_path}  ({sheet.width}x{sheet.height}px)")


if __name__ == "__main__":
    main()
