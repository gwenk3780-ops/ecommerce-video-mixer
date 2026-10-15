#!/usr/bin/env python3
"""
用 CLIP 检测别人的视频中的两类画面：
1. 竞品包装出现 → brand_segments.json（用于混剪时过滤）
2. 涂抹产品动作 → apply_segments.json（仅记录，供人工复查）

用法：
  python scan_brands_clip.py              # 扫描全部（跳过已缓存）
  python scan_brands_clip.py --force      # 强制重新扫描全部
  python scan_brands_clip.py --test       # 只扫描第一个视频
"""

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path

import torch
from PIL import Image

BASE_DIR = Path(__file__).parent
OTHERS_DIR = BASE_DIR / "别人的视频"
BRAND_CACHE = BASE_DIR / "brand_segments.json"
APPLY_CACHE = BASE_DIR / "apply_segments.json"
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE", "ffprobe")

SAMPLE_INTERVAL = 1.0   # 每隔多少秒抽一帧
MERGE_GAP = 2.0         # 合并相邻检测段的间隔(秒)
BRAND_EXPAND = 1.0      # 竞品包装段前后扩展(秒)
APPLY_EXPAND = 0.5      # 涂抹段前后扩展(秒)
PRODUCT_MARGIN = 0.0    # 产品检测最低优势
BRAND_ABS_THRESHOLD = 0.22  # brand 分数超过此值直接归竞品（不管 apply 是否更高）


def load_clip():
    from transformers import CLIPModel, CLIPProcessor
    print("  加载 CLIP 模型...", flush=True)
    model = CLIPModel.from_pretrained("openai/clip-vit-base-patch32")
    processor = CLIPProcessor.from_pretrained("openai/clip-vit-base-patch32")
    device = "mps" if torch.backends.mps.is_available() else "cpu"
    model = model.to(device).eval()
    return model, processor, device


def _to_tensor(emb):
    if isinstance(emb, torch.Tensor):
        return emb
    if hasattr(emb, "image_embeds"):
        return emb.image_embeds
    if hasattr(emb, "text_embeds"):
        return emb.text_embeds
    if hasattr(emb, "pooler_output"):
        return emb.pooler_output
    return emb.last_hidden_state[:, 0]


def get_image_embedding(image, model, processor, device):
    inputs = processor(images=image, return_tensors="pt").to(device)
    with torch.no_grad():
        emb = _to_tensor(model.get_image_features(**inputs))
    emb = emb / emb.norm(dim=-1, keepdim=True)
    return emb.squeeze(0).cpu()


def get_text_embeddings(texts, model, processor, device):
    inputs = processor(text=texts, return_tensors="pt", padding=True).to(device)
    with torch.no_grad():
        emb = _to_tensor(model.get_text_features(**inputs))
    emb = emb / emb.norm(dim=-1, keepdim=True)
    return emb.cpu()


def get_video_duration(video_path):
    cmd = [FFPROBE, "-v", "quiet", "-print_format", "json", "-show_format", video_path]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    return float(json.loads(result.stdout)["format"]["duration"])


def extract_frames(video_path, interval, tmp_dir):
    duration = get_video_duration(video_path)
    frames = []
    t = 0.0
    while t < duration:
        fp = os.path.join(tmp_dir, f"frame_{t:.1f}.jpg")
        subprocess.run([
            FFMPEG, "-y", "-ss", f"{t:.2f}", "-i", video_path,
            "-vframes", "1", "-q:v", "3",
            "-vf", "scale=224:224:force_original_aspect_ratio=increase,crop=224:224", fp
        ], capture_output=True, timeout=10)
        if os.path.exists(fp):
            frames.append((t, fp))
        t += interval
    return frames


def merge_segments(timestamps, expand, merge_gap):
    if not timestamps:
        return []
    segs = []
    cur_start = max(0.0, timestamps[0] - expand)
    cur_end = timestamps[0] + expand
    for t in timestamps[1:]:
        if t - expand <= cur_end + merge_gap:
            cur_end = max(cur_end, t + expand)
        else:
            segs.append([round(cur_start, 1), round(cur_end, 1)])
            cur_start = max(0.0, t - expand)
            cur_end = t + expand
    segs.append([round(cur_start, 1), round(cur_end, 1)])
    return segs


def scan_video(video_path, brand_emb, apply_emb, neutral_emb, model, processor, device):
    """
    同时检测竞品包装和涂抹动作，返回 (brand_segs, apply_segs)
    """
    brand_times = []
    apply_times = []

    with tempfile.TemporaryDirectory() as tmp:
        frames = extract_frames(video_path, SAMPLE_INTERVAL, tmp)
        for t, fp in frames:
            img = Image.open(fp).convert("RGB")
            img_emb = get_image_embedding(img, model, processor, device)

            brand_sim = torch.cosine_similarity(img_emb.unsqueeze(0), brand_emb, dim=-1).mean().item()
            apply_sim = torch.cosine_similarity(img_emb.unsqueeze(0), apply_emb, dim=-1).mean().item()
            neutral_sim = torch.cosine_similarity(img_emb.unsqueeze(0), neutral_emb, dim=-1).mean().item()

            # brand 分数超过绝对阈值 → 直接归竞品（涵盖"边涂抹边能看到包装"的情况）
            if brand_sim >= BRAND_ABS_THRESHOLD:
                brand_times.append(t)
            else:
                best = max(brand_sim, apply_sim, neutral_sim)
                if best == brand_sim and brand_sim > neutral_sim + PRODUCT_MARGIN:
                    brand_times.append(t)
                elif best == apply_sim and apply_sim > neutral_sim + PRODUCT_MARGIN:
                    apply_times.append(t)

    return (
        merge_segments(brand_times, BRAND_EXPAND, MERGE_GAP),
        merge_segments(apply_times, APPLY_EXPAND, MERGE_GAP),
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--test", action="store_true")
    parser.add_argument("--videos", nargs="+", help="只扫描指定文件名（不含路径）")
    args = parser.parse_args()

    print("加载 CLIP 模型...", flush=True)
    model, processor, device = load_clip()
    print(f"  使用设备: {device}", flush=True)

    # 三类文字 embedding：竞品包装 / 涂抹动作 / 中性画面
    brand_emb = get_text_embeddings([
        "a product bottle or tube with brand label being held or displayed",
        "cosmetic product packaging with brand name clearly visible",
        "skincare product container with visible logo held in hand",
        "a labeled cream or lotion bottle shown during application",
    ], model, processor, device)

    apply_emb = get_text_embeddings([
        "applying cream or lotion onto face or skin with no packaging visible",
        "rubbing skincare product into skin with fingers, no bottle in frame",
        "finger spreading product on face, no product container visible",
    ], model, processor, device)

    neutral_emb = get_text_embeddings([
        "a person's face or skin close-up",
        "a person talking to camera",
        "no product visible",
    ], model, processor, device)

    # 读取缓存
    brand_cache = json.load(open(BRAND_CACHE)) if BRAND_CACHE.exists() and not args.force else {}
    apply_cache = json.load(open(APPLY_CACHE)) if APPLY_CACHE.exists() and not args.force else {}

    videos = sorted(OTHERS_DIR.iterdir())
    videos = [v for v in videos if v.suffix.lower() in ('.mp4', '.mov', '.avi', '.mkv')]
    if args.test:
        videos = videos[:1]
    if args.videos:
        videos = [v for v in videos if v.name in args.videos]

    for v in videos:
        if v.name in brand_cache and not args.force:
            print(f"  跳过(已缓存): {v.name}", flush=True)
            continue

        print(f"  扫描: {v.name} ...", end=" ", flush=True)
        brand_segs, apply_segs = scan_video(str(v), brand_emb, apply_emb, neutral_emb,
                                            model, processor, device)
        brand_cache[v.name] = brand_segs
        apply_cache[v.name] = apply_segs

        with open(BRAND_CACHE, "w") as f:
            json.dump(brand_cache, f, indent=2)
        with open(APPLY_CACHE, "w") as f:
            json.dump(apply_cache, f, indent=2)

        parts = []
        if brand_segs:
            parts.append(f"竞品包装 {len(brand_segs)}段: {[f'{s[0]}-{s[1]}s' for s in brand_segs]}")
        if apply_segs:
            parts.append(f"涂抹 {len(apply_segs)}段: {[f'{s[0]}-{s[1]}s' for s in apply_segs]}")
        print(", ".join(parts) if parts else "干净", flush=True)

    print(f"\n完成  竞品包装→{BRAND_CACHE.name}  涂抹记录→{APPLY_CACHE.name}", flush=True)


if __name__ == "__main__":
    main()
