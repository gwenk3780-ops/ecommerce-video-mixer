#!/usr/bin/env python3
"""
将指定文件夹里的视频音轨替换为 bgm 文件夹里随机选取的音轨。
BGM 时长必须 >= 视频时长，替换后另存到新文件夹。

用法：
  python replace_bgm.py --input 脱毛膏/output_1 --bgm bgm --output 脱毛膏/output_1_bgm
"""

import os
import argparse
import json
import random
import subprocess
from pathlib import Path

BASE_DIR = Path(__file__).parent
FFMPEG  = os.environ.get("FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE", "ffprobe")


def get_duration(path: Path) -> float:
    cmd = [FFPROBE, "-v", "quiet", "-print_format", "json", "-show_format", str(path)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return float(json.loads(r.stdout)["format"]["duration"])
    except Exception:
        return 0.0


def replace_audio(video: Path, bgm: Path, output: Path, bgm_start: float = 0.0):
    """用 bgm 替换 video 的音轨，从 bgm_start 秒开始截取，输出到 output。"""
    output.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        FFMPEG, "-y",
        "-i", str(video),
        "-ss", f"{bgm_start:.2f}", "-i", str(bgm),
        "-map", "0:v",
        "-map", "1:a",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        str(output),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    return r.returncode == 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input",  default="脱毛膏/output_1", help="视频源文件夹")
    parser.add_argument("--bgm",    default="bgm",             help="BGM 文件夹")
    parser.add_argument("--output", default="脱毛膏/output_1_bgm", help="输出文件夹")
    parser.add_argument("--min-bgm-dur", type=float, default=0.0,
                        help="BGM 最短时长限制（秒），0=不限制，只要>=视频时长即可")
    args = parser.parse_args()

    input_dir  = Path(args.input)  if Path(args.input).is_absolute()  else BASE_DIR / args.input
    bgm_dir    = Path(args.bgm)    if Path(args.bgm).is_absolute()    else BASE_DIR / args.bgm
    output_dir = Path(args.output) if Path(args.output).is_absolute() else BASE_DIR / args.output

    # 收集所有 BGM 文件及时长
    bgm_files = sorted(bgm_dir.glob("*.mp4")) + sorted(bgm_dir.glob("*.mp3"))
    bgm_info = []
    print(f"扫描 BGM 文件夹: {bgm_dir} ({len(bgm_files)} 个文件)...", flush=True)
    for f in bgm_files:
        dur = get_duration(f)
        if dur > 0:
            bgm_info.append({"path": f, "dur": dur})
    print(f"有效 BGM: {len(bgm_info)} 个", flush=True)

    # 收集视频文件
    videos = sorted(input_dir.glob("*.mp4"))
    print(f"\n处理 {len(videos)} 个视频 → {output_dir}\n", flush=True)

    ok = 0
    for i, v in enumerate(videos, 1):
        v_dur = get_duration(v)
        if v_dur <= 0:
            print(f"  [{i}/{len(videos)}] ✗ {v.name}: 无法读取时长", flush=True)
            continue

        # 找时长 >= 视频时长的 BGM
        candidates = [b for b in bgm_info if b["dur"] >= v_dur]
        if not candidates:
            print(f"  [{i}/{len(videos)}] ✗ {v.name} ({v_dur:.1f}s): 没有足够长的 BGM", flush=True)
            continue

        chosen = random.choice(candidates)
        # 随机起始点，保证剩余时长 >= 视频时长
        max_start = chosen["dur"] - v_dur
        bgm_start = random.uniform(0, max_start) if max_start > 0 else 0.0

        out_path = output_dir / v.name
        success = replace_audio(v, chosen["path"], out_path, bgm_start)

        if success:
            print(f"  [{i}/{len(videos)}] ✓ {v.name} ({v_dur:.1f}s) ← {chosen['path'].name} (从{bgm_start:.1f}s)", flush=True)
            ok += 1
        else:
            print(f"  [{i}/{len(videos)}] ✗ {v.name}: ffmpeg 失败", flush=True)

    print(f"\n完成：{ok}/{len(videos)} 成功 → {output_dir}", flush=True)


if __name__ == "__main__":
    main()
