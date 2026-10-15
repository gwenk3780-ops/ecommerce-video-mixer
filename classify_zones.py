#!/usr/bin/env python3
"""
用 CLIP 检测别人的视频中的 before/after 区间，输出 source_classify.json。

用法：
  python classify_zones.py --base-dir 脱毛膏          # 扫描脱毛膏/别人的视频/
  python classify_zones.py --base-dir 脱毛膏 --force  # 强制重新扫描
  python classify_zones.py --base-dir 脱毛膏 --test   # 只扫描第一个视频（调试）

输出：{base-dir}/source_classify.json
  格式：{ "video.mp4": { "before": [0.0, 0.4], "after": [0.6, 1.0] }, ... }
  比例范围 0~1，基于视频时长。
"""

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path

import torch
from PIL import Image

FFMPEG  = os.environ.get("FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE", "ffprobe")

SAMPLE_INTERVAL = 1.0   # 每隔多少秒抽一帧
MERGE_GAP       = 2.0   # 合并相邻段的间隔(秒)
EXPAND          = 0.5   # 检测到的帧前后扩展(秒)
MIN_SEG_DUR     = 2.0   # 最短保留段长度(秒)


# ── 产品类型 prompts ──────────────────────────────────────────────
PRODUCT_PROMPTS = {
    "depilatory": {   # 脱毛膏
        "before": [
            "hairy legs or arms with visible body hair before hair removal",
            "unshaved skin with dark hair before depilation",
            "leg hair or arm hair clearly visible on skin",
        ],
        "after": [
            "smooth hairless legs or skin after hair removal",
            "clean hairless skin after depilation, no hair visible",
            "perfectly smooth legs after using hair removal cream",
        ],
        "neutral": [
            "applying white cream or lotion on skin",
            "a person talking to camera",
            "no skin or hair visible",
            "person's face close-up",
        ],
    },
}
# ─────────────────────────────────────────────────────────────────


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
    return frames, duration


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


def classify_video(video_path, before_emb, after_emb, neutral_emb, model, processor, device):
    before_times = []
    after_times  = []

    with tempfile.TemporaryDirectory() as tmp:
        frames, duration = extract_frames(video_path, SAMPLE_INTERVAL, tmp)
        for t, fp in frames:
            img     = Image.open(fp).convert("RGB")
            img_emb = get_image_embedding(img, model, processor, device)

            before_sim  = torch.cosine_similarity(img_emb.unsqueeze(0), before_emb,  dim=-1).mean().item()
            after_sim   = torch.cosine_similarity(img_emb.unsqueeze(0), after_emb,   dim=-1).mean().item()
            neutral_sim = torch.cosine_similarity(img_emb.unsqueeze(0), neutral_emb, dim=-1).mean().item()

            best = max(before_sim, after_sim, neutral_sim)
            if best == before_sim:
                before_times.append(t)
            elif best == after_sim:
                after_times.append(t)
            # neutral → 跳过，不归入任何区间

    before_segs = merge_segments(before_times, EXPAND, MERGE_GAP, duration, MIN_SEG_DUR)
    after_segs  = merge_segments(after_times,  EXPAND, MERGE_GAP, duration, MIN_SEG_DUR)

    # 转换为比例
    def to_ratio(segs):
        return [[round(s[0] / duration, 3), round(s[1] / duration, 3)] for s in segs]

    return to_ratio(before_segs), to_ratio(after_segs), duration


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-dir", required=True, help="产品目录，例如：脱毛膏")
    parser.add_argument("--product-type", default="depilatory",
                        choices=list(PRODUCT_PROMPTS.keys()),
                        help="产品类型，决定使用哪组 prompts")
    parser.add_argument("--force", action="store_true", help="强制重新扫描（忽略缓存）")
    parser.add_argument("--test",  action="store_true", help="只扫描第一个视频")
    args = parser.parse_args()

    base = Path(args.base_dir)
    if not base.is_absolute():
        base = Path(__file__).parent / base

    others_dir    = base / "别人的视频"
    classify_file = base / "source_classify.json"

    if not others_dir.exists():
        print(f"目录不存在: {others_dir}"); return

    # 加载缓存
    cache = {}
    if classify_file.exists() and not args.force:
        raw = json.load(open(classify_file))
        cache = {k: v for k, v in raw.items() if not k.startswith("_")}

    print(f"加载 CLIP 模型...", flush=True)
    model, processor, device = load_clip()
    print(f"  使用设备: {device}", flush=True)

    prompts = PRODUCT_PROMPTS[args.product_type]
    before_emb  = get_text_embeddings(prompts["before"],  model, processor, device)
    after_emb   = get_text_embeddings(prompts["after"],   model, processor, device)
    neutral_emb = get_text_embeddings(prompts["neutral"], model, processor, device)

    videos = sorted(others_dir.iterdir())
    videos = [v for v in videos if v.suffix.lower() in ('.mp4', '.mov', '.avi', '.mkv')]
    if args.test:
        videos = videos[:1]

    for v in videos:
        if v.name in cache and not args.force:
            print(f"  跳过(已缓存): {v.name}", flush=True)
            continue

        print(f"  分类: {v.name} ...", end=" ", flush=True)
        try:
            before_segs, after_segs, dur = classify_video(
                str(v), before_emb, after_emb, neutral_emb, model, processor, device
            )
        except Exception as e:
            print(f"ERROR: {e}", flush=True)
            continue

        entry = {}
        if before_segs:
            # 取第一段 before（通常是视频开头展示问题的部分）
            entry["before"] = before_segs[0]
        if after_segs:
            # 取最后一段 after（通常是视频结尾展示效果的部分）
            entry["after"] = after_segs[-1]
        if not entry:
            # 没检测到有效区间 → 整个视频作为 before
            entry["before"] = [0.0, 1.0]
            entry["note"] = "CLIP未检测到明显before/after，全段作为before"

        cache[v.name] = entry

        # 实时保存
        out = {"_说明": "before/after 是视频时长的比例范围 [起始%, 结束%]，由 CLIP 自动标注"}
        out.update(cache)
        with open(classify_file, "w") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)

        parts = []
        if "before" in entry:
            parts.append(f"before={entry['before']}")
        if "after" in entry:
            parts.append(f"after={entry['after']}")
        print("  ".join(parts) or entry.get("note", ""), flush=True)

    print(f"\n完成 → {classify_file}", flush=True)


if __name__ == "__main__":
    main()
