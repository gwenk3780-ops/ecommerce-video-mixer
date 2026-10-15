#!/usr/bin/env python3
"""
生成视频并与 selected 文件夹做重复率检测，重复率 > 阈值的丢弃重试。

用法：
  python run_unique.py --base-dir 脱毛膏 -n 20
  python run_unique.py --base-dir 脱毛膏 -n 20 --threshold 0.9
"""

import argparse
import subprocess
import sys
import tempfile
import json
import os
from pathlib import Path

import imagehash
from PIL import Image

FFMPEG  = os.environ.get("FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE", "ffprobe")
PYTHON  = sys.executable
BATCH_SCRIPT = Path(__file__).parent / "batch_mix.py"

SAMPLE_INTERVAL = 1.0   # 每隔多少秒抽一帧做比对
HASH_THRESHOLD  = 10    # pHash 距离 <= 此值认为两帧相似


def get_duration(path):
    cmd = [FFPROBE, "-v", "quiet", "-print_format", "json", "-show_format", str(path)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    return float(json.loads(r.stdout)["format"]["duration"])


def extract_hashes(video_path, interval=SAMPLE_INTERVAL):
    duration = get_duration(video_path)
    hashes = []
    t = 0.0
    while t < duration:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            fp = tmp.name
        try:
            subprocess.run([
                FFMPEG, "-y", "-ss", f"{t:.2f}", "-i", str(video_path),
                "-vframes", "1", "-q:v", "5",
                "-vf", "scale=64:64",
                fp
            ], capture_output=True, timeout=10)
            if os.path.exists(fp) and os.path.getsize(fp) > 0:
                img = Image.open(fp).convert("RGB")
                hashes.append(imagehash.phash(img))
        except Exception:
            pass
        finally:
            try: os.unlink(fp)
            except: pass
        t += interval
    return hashes


def similarity(hashes_new, hashes_ref):
    """新视频有多少帧在 ref 中找到了相似帧（pHash距离<=阈值）"""
    if not hashes_new or not hashes_ref:
        return 0.0
    matches = 0
    for h in hashes_new:
        if any(abs(h - r) <= HASH_THRESHOLD for r in hashes_ref):
            matches += 1
    return matches / len(hashes_new)


def max_similarity_vs_selected(video_path, selected_hashes):
    """与 selected 里所有视频的最大相似度"""
    if not selected_hashes:
        return 0.0
    new_h = extract_hashes(video_path)
    max_sim = max(similarity(new_h, ref_h) for ref_h in selected_hashes)
    return max_sim


def next_output_dir(base):
    i = 2
    while True:
        d = base / f"output_{i}"
        if not d.exists():
            return d
        i += 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-dir", required=True)
    parser.add_argument("-n", type=int, default=20, help="目标生成数量")
    parser.add_argument("--threshold", type=float, default=0.9, help="重复率阈值，超过则丢弃")
    parser.add_argument("--no-brand-filter", action="store_true")
    args = parser.parse_args()

    base = Path(args.base_dir)
    if not base.is_absolute():
        base = Path(__file__).parent / base

    ref_dirs = [base / "output_1", base / "output_2"]
    output_dir = next_output_dir(base)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"输出目录: {output_dir}", flush=True)
    print(f"重复率阈值: {args.threshold:.0%}", flush=True)

    # 预计算 output_1 + output_2 里所有视频的帧哈希
    selected_hashes = []
    for ref_dir in ref_dirs:
        if ref_dir.exists():
            ref_videos = sorted(ref_dir.glob("*.mp4"))
            print(f"预计算 {ref_dir.name}/{len(ref_videos)} 个视频的帧哈希...", flush=True)
            for v in ref_videos:
                try:
                    selected_hashes.append(extract_hashes(v))
                except Exception as e:
                    print(f"  跳过 {v.name}: {e}", flush=True)
    print(f"  完成，共 {len(selected_hashes)} 个参考视频", flush=True)

    accepted = 0
    attempts = 0
    max_attempts = args.n * 5  # 最多尝试5倍数量

    with tempfile.TemporaryDirectory() as tmp_out:
        while accepted < args.n and attempts < max_attempts:
            attempts += 1

            # 生成1个视频到临时目录
            cmd = [PYTHON, str(BATCH_SCRIPT), "--base-dir", str(base), "1", tmp_out, "--bgm-dir", "bgm"]
            if args.no_brand_filter:
                cmd.append("--no-brand-filter")

            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode != 0:
                print(f"  [尝试{attempts}] batch_mix 失败，跳过", flush=True)
                continue

            # 找到刚生成的临时文件
            tmp_files = sorted(Path(tmp_out).glob("*.mp4"))
            if not tmp_files:
                print(f"  [尝试{attempts}] 未找到生成的视频，跳过", flush=True)
                continue

            tmp_video = tmp_files[0]

            # 重复率检测
            if selected_hashes:
                sim = max_similarity_vs_selected(tmp_video, selected_hashes)
                if sim > args.threshold:
                    print(f"  [尝试{attempts}] 重复率 {sim:.1%} > {args.threshold:.0%}，丢弃", flush=True)
                    tmp_video.unlink(missing_ok=True)
                    continue
                print(f"  [尝试{attempts}] 重复率 {sim:.1%} ✓", flush=True)
            else:
                print(f"  [尝试{attempts}] 无 selected 参照，直接接受", flush=True)

            # 移动到 output 目录
            accepted += 1
            dest = output_dir / f"mix_{accepted:03d}.mp4"
            tmp_video.rename(dest)
            print(f"  → 已接受 {accepted}/{args.n}: {dest.name}", flush=True)

            # 更新 selected_hashes（把新接受的也加进去，避免批次内自己互相重复）
            try:
                selected_hashes.append(extract_hashes(dest))
            except Exception:
                pass

    print(f"\n完成：接受 {accepted} 个，尝试 {attempts} 次，输出在 {output_dir}", flush=True)


if __name__ == "__main__":
    main()
