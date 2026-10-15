#!/usr/bin/env python3
"""
批量生成混剪电商带货视频 v4
核心思路：
- 基于 source_classify.json 精确区分 before/after 素材区间
- 叙事结构：before片段 → 产品 → after片段 → 产品收尾
- 音频连贯（取一条源视频音频贯穿）+ 画面独立切换
"""

import os
import sys
import json
import random
import subprocess
import tempfile
import shutil
from pathlib import Path

# === 配置 ===
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE", "ffprobe")

BASE_DIR = Path(__file__).parent
BGM_DIR = None  # 若指定，从该目录取音频源
OTHERS_DIR = BASE_DIR / "别人的视频"
PRODUCT_DIR = BASE_DIR / "我们的品"
OUTPUT_DIR = BASE_DIR / "output"
CLASSIFY_FILE = BASE_DIR / "source_classify.json"
BRAND_SEGMENTS_FILE = BASE_DIR / "brand_segments.json"
BLACK_SEGMENTS_FILE  = BASE_DIR / "black_segments.json"

AVOID_BRANDS = True  # 默认绕过竞品，可通过 --no-brand-filter 关闭

OUTPUT_W, OUTPUT_H = 1080, 1920
TARGET_DURATION = (15, 18)
CLIP_DURATION_OTHERS = (2.5, 5.0)
CLIP_DURATION_PRODUCT = (2.0, 4.0)
PRODUCT_IMAGE_DUR = (2.0, 3.5)

# 去重参数
SPEED_RANGE = (0.9, 1.1)
CROP_RANGE = (0.92, 0.95)

# 跨视频素材使用计数（同一进程内共享，确保均匀轮转）
_source_use_count = {}



def get_video_info(path):
    cmd = [FFPROBE, "-v", "quiet", "-print_format", "json",
           "-show_format", "-show_streams", str(path)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    data = json.loads(result.stdout)
    duration = float(data["format"]["duration"])
    has_audio = any(s.get("codec_type") == "audio" for s in data.get("streams", []))
    return duration, has_audio


def detect_black_segments(path, black_min_duration=0.3, pixel_threshold=0.10):
    """用 FFmpeg blackdetect 检测视频中的黑帧段，返回 [[start, end], ...]"""
    cmd = [
        FFMPEG, "-i", str(path),
        "-vf", f"blackdetect=d={black_min_duration}:pix_th={pixel_threshold}",
        "-an", "-f", "null", "-"
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    segs = []
    for line in result.stderr.splitlines():
        if "black_start" not in line:
            continue
        parts = {kv.split(":")[0]: float(kv.split(":")[1])
                 for kv in line.split() if ":" in kv and kv.split(":")[0] in
                 ("black_start", "black_end")}
        if "black_start" in parts and "black_end" in parts:
            segs.append([parts["black_start"], parts["black_end"]])
    return segs


def load_black_segments(video_paths):
    """加载或构建黑帧缓存，返回 {filename: [[s,e],...]}"""
    cache = {}
    if BLACK_SEGMENTS_FILE.exists():
        with open(BLACK_SEGMENTS_FILE, "r") as f:
            cache = json.load(f)

    updated = False
    for p in video_paths:
        name = Path(p).name
        if name not in cache:
            print(f"  检测黑帧: {name} ...", flush=True)
            cache[name] = detect_black_segments(p)
            updated = True

    if updated:
        with open(BLACK_SEGMENTS_FILE, "w") as f:
            json.dump(cache, f, indent=2)

    return cache


AUDIO_STATUS_FILE = BASE_DIR / "audio_status.json"
SILENT_THRESHOLD_DB = -60.0  # max_volume 低于此值视为静音


def check_audio_has_sound(path):
    """用 FFmpeg volumedetect 检测音频是否有实际声音，返回 True/False"""
    cmd = [
        FFMPEG, "-i", str(path),
        "-af", "volumedetect", "-f", "null", "-"
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    for line in result.stderr.splitlines():
        if "max_volume" in line:
            try:
                db = float(line.split("max_volume:")[1].strip().split()[0])
                return db > SILENT_THRESHOLD_DB
            except (IndexError, ValueError):
                pass
    return False  # 无法检测到音量信息时保守处理为无声


def load_audio_status(video_paths):
    """加载或构建音频状态缓存，返回 {filename: True/False}（True=有声）"""
    cache = {}
    if AUDIO_STATUS_FILE.exists():
        with open(AUDIO_STATUS_FILE, "r") as f:
            cache = json.load(f)

    updated = False
    for p in video_paths:
        name = Path(p).name
        if name not in cache:
            print(f"  检测音频: {name} ...", flush=True)
            cache[name] = check_audio_has_sound(p)
            updated = True

    if updated:
        with open(AUDIO_STATUS_FILE, "w") as f:
            json.dump(cache, f, indent=2)

    return cache


def get_all_sources():
    """加载素材并读取分类标注"""
    # 读取分类配置
    classify = {}
    if CLASSIFY_FILE.exists():
        with open(CLASSIFY_FILE, "r") as f:
            classify = json.load(f)

    # 读取竞品时间段缓存
    brand_segments = {}
    if BRAND_SEGMENTS_FILE.exists():
        with open(BRAND_SEGMENTS_FILE, "r") as f:
            brand_segments = json.load(f)

    before_sources = []  # 可用于 before 的素材+区间
    after_sources = []   # 可用于 after 的素材+区间
    all_others = []      # 所有别人的视频（用于音频源）

    # 收集视频路径，用于黑帧和音频检测
    video_files = [f for f in sorted(OTHERS_DIR.iterdir())
                   if f.suffix.lower() in ('.mp4', '.mov', '.avi', '.mkv')]
    black_segments = load_black_segments([str(f) for f in video_files])
    audio_status   = load_audio_status([str(f) for f in video_files])

    for f in video_files:
        dur, has_audio_stream = get_video_info(f)
        # has_audio = 有音频流 且 实际有声音（排除静音轨）
        has_audio = has_audio_stream and audio_status.get(f.name, False)
        # 竞品段和黑帧段分开存：竞品段受 AVOID_BRANDS 开关控制，黑帧段始终排除
        info = {
            "path": str(f), "duration": dur, "has_audio": has_audio,
            "brand_segs": brand_segments.get(f.name, []),
            "black_segs":  black_segments.get(f.name, []),
        }
        all_others.append(info)

        # 不区分 before/after，所有素材用完整区间
        before_sources.append({**info, "zone_start": 0, "zone_end": dur})
        after_sources.append({**info, "zone_start": 0, "zone_end": dur})

    products_video = []
    products_image = []
    for f in sorted(PRODUCT_DIR.iterdir()):
        if f.suffix.lower() in ('.mp4', '.mov'):
            dur, has_audio = get_video_info(f)
            products_video.append({"path": str(f), "duration": dur, "has_audio": has_audio})
        elif f.suffix.lower() in ('.png', '.jpg', '.jpeg'):
            products_image.append({"path": str(f)})

    # 没有分类标注时，把所有视频同时当 before 和 after 素材使用（full zone）
    if not before_sources and not after_sources and all_others:
        before_sources = [
            {**info, "zone_start": 0, "zone_end": info["duration"]}
            for info in all_others
        ]
        after_sources = list(before_sources)

    return all_others, before_sources, after_sources, products_video, products_image


def random_crop_filter():
    crop_ratio = random.uniform(*CROP_RANGE)
    offset_x = random.uniform(0, 1 - crop_ratio)
    offset_y = random.uniform(0, 1 - crop_ratio)
    return (
        f"crop=iw*{crop_ratio:.3f}:ih*{crop_ratio:.3f}"
        f":iw*{offset_x:.3f}:ih*{offset_y:.3f}"
    )


def extract_audio_track(source_path, target_dur, output_path):
    """从一个视频中提取连续音频轨，归一化音量"""
    src_dur, has_audio = get_video_info(source_path)
    if not has_audio:
        return None

    # 随机选一个起始点（确保有足够长度）
    max_start = max(0, src_dur - target_dur - 0.5)
    start = random.uniform(0, max_start) if max_start > 0 else 0

    cmd = [
        FFMPEG, "-y",
        "-ss", f"{start:.2f}",
        "-t", f"{target_dur + 1:.2f}",
        "-i", source_path,
        "-vn",
        "-af", "loudnorm=I=-14:TP=-1.5:LRA=11,aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo,apad=pad_dur=2,afade=t=in:st=0:d=0.3,afade=t=out:st={:.2f}:d=0.5".format(target_dur - 0.5),
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
        "-t", f"{target_dur:.2f}",
        output_path,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    return output_path if proc.returncode == 0 else None


def make_visual_segment(seg, tmp_dir, seg_idx):
    """渲染一个纯视频片段（无音频），统一分辨率"""
    out_path = os.path.join(tmp_dir, f"vseg_{seg_idx:03d}.mp4")

    speed = seg.get("speed", 1.0)
    crop = seg.get("crop", "")
    mirror = seg.get("mirror", False)

    is_image = seg["type"] == "image"

    vfilters = []
    if abs(speed - 1.0) > 0.01:
        vfilters.append(f"setpts={1/speed:.4f}*PTS")
    if crop:
        vfilters.append(crop)
    if mirror:
        vfilters.append("hflip")
    if is_image:
        # 图片：保持原始比例，四周填黑边
        vfilters.append(f"scale={OUTPUT_W}:{OUTPUT_H}:force_original_aspect_ratio=decrease")
        vfilters.append(f"pad={OUTPUT_W}:{OUTPUT_H}:(ow-iw)/2:(oh-ih)/2:color=black")
    else:
        vfilters.append(f"scale={OUTPUT_W}:{OUTPUT_H}:force_original_aspect_ratio=increase")
        vfilters.append(f"crop={OUTPUT_W}:{OUTPUT_H}")
    vfilters.append("setsar=1")
    vfilters.append("format=yuv420p")
    # 统一帧率，避免不同帧率片段拼接时画面冻结
    vfilters.append("fps=30")
    vfilter_str = ",".join(vfilters)

    if is_image:
        cmd = [
            FFMPEG, "-y",
            "-loop", "1", "-t", f"{seg['duration']:.2f}",
            "-i", seg["path"],
            "-vf", vfilter_str,
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
            "-r", "30",
            "-an",
            out_path,
        ]
    else:
        cmd = [
            FFMPEG, "-y",
            "-ss", f"{seg['start']:.2f}",
            "-t", f"{seg['duration']:.2f}",
            "-i", seg["path"],
            "-vf", vfilter_str,
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
            "-r", "30",
            "-an",
            out_path,
        ]

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        return None, proc.stderr
    return out_path, None


def concat_visual_with_xfade(segment_files, tmp_dir):
    """用 xfade 转场拼接所有视频片段"""
    if len(segment_files) < 2:
        return segment_files[0] if segment_files else None, None

    # 获取每个片段的时长
    durations = []
    for sf in segment_files:
        dur, _ = get_video_info(sf)
        durations.append(dur)

    # 构建 xfade 链式滤镜
    # 每两个片段之间加一个 xfade
    n = len(segment_files)
    inputs = []
    for sf in segment_files:
        inputs.extend(["-i", sf])

    filter_parts = []
    # 计算每个 xfade 的 offset（前一个片段结束前 XFADE_DURATION 秒开始过渡）
    offsets = []
    cumulative = 0
    for i in range(n - 1):
        offset = cumulative + durations[i] - XFADE_DURATION
        offsets.append(max(0, offset))
        cumulative = offset  # 下一段从这里继续

    # 链式 xfade
    if n == 2:
        filter_parts.append(
            f"[0:v][1:v]xfade=transition=fade:duration={XFADE_DURATION:.3f}"
            f":offset={offsets[0]:.3f}[outv]"
        )
    else:
        # 第一对
        filter_parts.append(
            f"[0:v][1:v]xfade=transition=fade:duration={XFADE_DURATION:.3f}"
            f":offset={offsets[0]:.3f}[xf0]"
        )
        # 中间对
        for i in range(1, n - 2):
            filter_parts.append(
                f"[xf{i-1}][{i+1}:v]xfade=transition=fade:duration={XFADE_DURATION:.3f}"
                f":offset={offsets[i]:.3f}[xf{i}]"
            )
        # 最后一对
        last = n - 2
        filter_parts.append(
            f"[xf{last-1}][{n-1}:v]xfade=transition=fade:duration={XFADE_DURATION:.3f}"
            f":offset={offsets[last]:.3f}[outv]"
        )

    filter_complex = ";".join(filter_parts)
    out_path = os.path.join(tmp_dir, "visual_combined.mp4")

    cmd = [FFMPEG, "-y"] + inputs + [
        "-filter_complex", filter_complex,
        "-map", "[outv]",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
        "-an",
        out_path,
    ]

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        return None, proc.stderr
    return out_path, None


def concat_visual_simple(segment_files, tmp_dir):
    """简单拼接（concat demuxer），作为 xfade 的备选"""
    concat_list = os.path.join(tmp_dir, "concat.txt")
    with open(concat_list, "w") as f:
        for sf in segment_files:
            f.write(f"file '{sf}'\n")

    out_path = os.path.join(tmp_dir, "visual_combined.mp4")
    cmd = [
        FFMPEG, "-y",
        "-f", "concat", "-safe", "0",
        "-i", concat_list,
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
        "-an",
        out_path,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        return None, proc.stderr
    return out_path, None


def merge_audio_video(video_path, audio_path, output_path):
    """合并视频轨和音频轨，音频裁剪到视频实际时长"""
    # 获取视频轨实际时长，用它来限制音频
    video_dur, _ = get_video_info(video_path)

    cmd = [
        FFMPEG, "-y",
        "-i", video_path,
        "-i", audio_path,
        "-t", f"{video_dur:.3f}",
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        "-af", f"afade=t=out:st={max(0, video_dur - 0.5):.3f}:d=0.5",
        "-map", "0:v:0", "-map", "1:a:0",
        "-movflags", "+faststart",
        str(output_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        return False, proc.stderr
    return True, None


def get_valid_zones(zone_start, zone_end, brand_segs, min_dur=1.0):
    """从 zone 中排除竞品时间段，返回可用的子区间列表 [(start, end), ...]"""
    # 先把竞品段按开始时间排序
    blocked = sorted(brand_segs, key=lambda s: s[0])
    valid = []
    cursor = zone_start
    for bs, be in blocked:
        if bs >= zone_end:
            break
        if be <= cursor:
            continue
        # cursor ~ bs 是安全区间
        seg_start = cursor
        seg_end = min(bs, zone_end)
        if seg_end - seg_start >= min_dur:
            valid.append((seg_start, seg_end))
        cursor = max(cursor, be)
    # 最后一段
    if zone_end - cursor >= min_dur:
        valid.append((cursor, zone_end))
    return valid


def make_clip_from_source(src, clip_dur):
    """
    从标注的 zone_start/zone_end 区间内随机取一段片段，
    自动跳过竞品品牌出现的时间段。
    """
    zone_start = src.get("zone_start", 0)
    zone_end = src.get("zone_end", src["duration"])
    brand_segs = src.get("brand_segs", [])
    black_segs  = src.get("black_segs", [])

    # 始终排除黑帧段；竞品段仅在 AVOID_BRANDS=True 时排除
    exclude_segs = black_segs[:]
    if AVOID_BRANDS:
        exclude_segs += brand_segs

    if exclude_segs:
        valid_zones = get_valid_zones(zone_start, zone_end, exclude_segs, min_dur=clip_dur * 0.8)
        if not valid_zones:
            valid_zones = [(zone_start, zone_end)]
    else:
        valid_zones = [(zone_start, zone_end)]

    # 随机选一个可用区间
    vz_start, vz_end = random.choice(valid_zones)
    zone_len = vz_end - vz_start
    clip_dur = min(clip_dur, zone_len)

    max_start = max(vz_start, vz_end - clip_dur - 0.2)
    start = random.uniform(vz_start, max_start) if max_start > vz_start else vz_start
    start = max(0, min(start, src["duration"] - clip_dur - 0.1))

    return {
        "type": "video",
        "path": src["path"],
        "start": start,
        "duration": clip_dur,
        "speed": random.uniform(*SPEED_RANGE),
        "crop": random_crop_filter(),
        "mirror": False,
    }


def make_product_segment(products_video, products_image, pv_idx, max_dur):
    """生成一个产品片段（视频或图片）"""
    # 没有图片时强制用视频，避免 30% 概率返回 None 导致产品段缺失
    use_video = (random.random() < 0.7 or not products_image) and bool(products_video)
    if use_video:
        pv = products_video[pv_idx % len(products_video)]
        clip_dur = random.uniform(*CLIP_DURATION_PRODUCT)
        clip_dur = min(clip_dur, pv["duration"], max_dur)
        if clip_dur < 0.5:
            return None, pv_idx + 1
        max_start = max(0, pv["duration"] - clip_dur - 0.3)
        start = random.uniform(0, max_start) if max_start > 0 else 0
        seg = {
            "type": "video",
            "path": pv["path"],
            "start": start,
            "duration": clip_dur,
            "speed": random.uniform(*SPEED_RANGE),
            "crop": random_crop_filter(),
            "mirror": False,
        }
        return seg, pv_idx + 1
    elif products_image:
        pi = random.choice(products_image)
        img_dur = random.uniform(*PRODUCT_IMAGE_DUR)
        img_dur = min(img_dur, max_dur)
        if img_dur < 0.5:
            return None, pv_idx
        seg = {
            "type": "image",
            "path": pi["path"],
            "duration": img_dur,
        }
        return seg, pv_idx
    return None, pv_idx


def fill_phase(sources, budget, last_path):
    """从 sources 中按顺序取片段填满 budget 秒，不重复相邻来源。
    使用全局计数器确保跨视频均匀轮转，使用次数少的素材优先被选到。"""
    segs = []
    # 按使用次数升序排列，同等次数内随机打散
    sorted_sources = sorted(sources, key=lambda s: (_source_use_count.get(s["path"], 0), random.random()))
    idx = 0
    accumulated = 0.0
    while accumulated < budget:
        if budget - accumulated < 0.5:
            break
        src = sorted_sources[idx % len(sorted_sources)]
        idx += 1
        if src["path"] == last_path and len(sources) > 1:
            src = sorted_sources[idx % len(sorted_sources)]
            idx += 1

        clip_dur = random.uniform(*CLIP_DURATION_OTHERS)
        clip_dur = min(clip_dur, budget - accumulated)
        if clip_dur < 0.5:
            break

        seg = make_clip_from_source(src, clip_dur)
        segs.append(seg)
        _source_use_count[src["path"]] = _source_use_count.get(src["path"], 0) + 1
        last_path = src["path"]
        accumulated += clip_dur / seg["speed"]

    return segs


def plan_segments(before_sources, after_sources, products_video, products_image, target_dur, seed):
    """
    叙事结构：before片段 → 产品 → after片段 → 产品收尾
    - before_sources: 标注为 before 区间的素材（展示皮肤问题）
    - after_sources:  标注为 after 区间的素材（展示改善效果）
    """
    random.seed(seed)
    segments = []

    shuffled_pv = list(products_video) if products_video else []
    random.shuffle(shuffled_pv)
    pv_idx = 0
    used_product_paths = set()  # 记录已用过的产品素材路径，同一视频内不重复

    # 结尾不能用"不要放在结尾"的视频
    shuffled_pv_ending = [p for p in shuffled_pv if "不要放在结尾" not in Path(p["path"]).name]
    if not shuffled_pv_ending:
        shuffled_pv_ending = shuffled_pv  # 没有其他选项时回退
    pv_idx_ending = 0

    # 时长分配
    has_after = len(after_sources) > 0
    dur_product1 = target_dur * random.uniform(0.08, 0.12)
    dur_after    = target_dur * random.uniform(0.25, 0.35) if has_after else 0
    # product2 封顶 25%，避免无 after 素材时产品段占满后半段
    dur_product2 = min(target_dur * random.uniform(0.15, 0.25),
                       target_dur - dur_product1 - dur_after - target_dur * 0.35)
    dur_before   = target_dur - dur_product1 - dur_after - dur_product2

    # === 第一段：before（皮肤问题） ===
    before_segs = fill_phase(before_sources, dur_before, last_path=None)
    segments.extend(before_segs)

    # === 第二段：产品展示 ===
    acc = 0.0
    while acc < dur_product1:
        if dur_product1 - acc < 0.5:
            break
        seg, pv_idx = make_product_segment(shuffled_pv, products_image, pv_idx, dur_product1 - acc)
        if seg is None:
            break
        segments.append(seg)
        used_product_paths.add(seg["path"])
        seg_dur = seg["duration"] / seg.get("speed", 1.0) if seg["type"] != "image" else seg["duration"]
        acc += seg_dur

    # === 第三段：after（效果展示），无 after 素材时用 before 补充 ===
    last = segments[-1]["path"] if segments else None
    if has_after:
        after_segs = fill_phase(after_sources, dur_after, last_path=last)
    else:
        after_segs = fill_phase(before_sources, target_dur * random.uniform(0.25, 0.35), last_path=last)
    segments.extend(after_segs)

    # === 第四段：产品收尾（视频永远以产品结束，不用"不要放在结尾"的素材，不重复已用素材） ===
    unused_pv_ending = [p for p in shuffled_pv_ending if p["path"] not in used_product_paths]
    if not unused_pv_ending:
        unused_pv_ending = shuffled_pv_ending  # 素材不足时回退
    unused_pi = [p for p in products_image if p["path"] not in used_product_paths] if products_image else []
    if not unused_pi:
        unused_pi = products_image  # 图片不足时回退
    acc = 0.0
    while acc < dur_product2:
        if dur_product2 - acc < 0.5:
            break
        seg, pv_idx_ending = make_product_segment(unused_pv_ending, unused_pi, pv_idx_ending, dur_product2 - acc)
        if seg is None:
            break
        segments.append(seg)
        used_product_paths.add(seg["path"])
        seg_dur = seg["duration"] / seg.get("speed", 1.0) if seg["type"] != "image" else seg["duration"]
        acc += seg_dur

    return segments


def build_video(video_id, all_others, before_sources, after_sources, products_video, products_image, seed):
    """构建一个混剪视频：连续音频 + 独立画面切换"""
    random.seed(seed)
    target_dur = random.uniform(*TARGET_DURATION)

    # 1. 选一个有音频的视频作为音频源
    if BGM_DIR and BGM_DIR.exists():
        bgm_files = list(BGM_DIR.iterdir())
        bgm_files = [f for f in bgm_files if f.suffix.lower() in ('.mp4', '.mov', '.avi', '.mkv', '.mp3', '.m4a', '.wav')]
        audio_candidates = [{"path": str(f), "duration": get_video_info(str(f))[0]} for f in bgm_files]
        audio_candidates = [c for c in audio_candidates if c["duration"] >= target_dur]
        if not audio_candidates:
            audio_candidates = [{"path": str(f), "duration": get_video_info(str(f))[0]} for f in bgm_files]
    else:
        audio_candidates = [o for o in all_others if o["has_audio"] and o["duration"] >= target_dur]
        if not audio_candidates:
            audio_candidates = [o for o in all_others if o["has_audio"] and o["duration"] >= target_dur * 0.8]
        if not audio_candidates:
            audio_candidates = [o for o in all_others if o["has_audio"]]
    if not audio_candidates:
        return False, "无可用音频源"
    audio_source = random.choice(audio_candidates)

    # 2. 规划视觉片段（before→产品→after→产品收尾）
    visual_seed = random.randint(0, 2**32)
    segments = plan_segments(before_sources, after_sources, products_video, products_image, target_dur, visual_seed)
    if not segments:
        return False, "片段不足"

    output_path = OUTPUT_DIR / f"mix_{video_id:03d}.mp4"
    tmp_dir = tempfile.mkdtemp(prefix="videomix_")

    try:
        # 3. 渲染每个视觉片段（纯视频无音频）
        seg_files = []
        for idx, seg in enumerate(segments):
            sf, err = make_visual_segment(seg, tmp_dir, idx)
            if sf is None:
                return False, f"片段{idx}失败: {err[:150]}"
            seg_files.append(sf)

        # 4. 拼接视觉片段
        visual_combined, err = concat_visual_simple(seg_files, tmp_dir)
        if visual_combined is None:
            return False, f"视觉拼接失败: {err[:150]}"

        # 5. 获取视频实际时长，按此提取音频
        actual_video_dur, _ = get_video_info(visual_combined)

        audio_file = os.path.join(tmp_dir, "audio.m4a")
        audio_result = extract_audio_track(audio_source["path"], actual_video_dur, audio_file)
        if audio_result is None:
            return False, "音频提取失败"

        # 6. 合并音频和视频
        ok, err = merge_audio_video(visual_combined, audio_file, output_path)
        if not ok:
            return False, f"合并失败: {err[:150]}"

        return True, str(output_path)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("count", nargs="?", type=int, default=50, help="生成数量（默认50）")
    parser.add_argument("output_dir", nargs="?", default=None, help="输出目录")
    parser.add_argument("--no-brand-filter", action="store_true", help="不绕过竞品画面（默认绕过）")
    parser.add_argument("--base-dir", default=None, help="项目根目录（含别人的视频/我们的品子目录），默认为脚本所在目录")
    parser.add_argument("--bgm-dir", default=None, help="BGM音频目录，指定后从该目录随机选取音频源")
    args = parser.parse_args()

    global AVOID_BRANDS, OUTPUT_DIR, OTHERS_DIR, PRODUCT_DIR, CLASSIFY_FILE, BRAND_SEGMENTS_FILE, BLACK_SEGMENTS_FILE, AUDIO_STATUS_FILE, BGM_DIR
    AVOID_BRANDS = not args.no_brand_filter

    if args.base_dir:
        base = Path(args.base_dir)
        OTHERS_DIR = base / "别人的视频"
        PRODUCT_DIR = base / "我们的品"
        CLASSIFY_FILE = base / "source_classify.json"
        BRAND_SEGMENTS_FILE = base / "brand_segments.json"
        BLACK_SEGMENTS_FILE  = base / "black_segments.json"
        AUDIO_STATUS_FILE    = base / "audio_status.json"
        if args.output_dir is None:
            OUTPUT_DIR = base / "output"

    if args.output_dir:
        OUTPUT_DIR = Path(args.output_dir)
        if not OUTPUT_DIR.is_absolute():
            OUTPUT_DIR = Path(__file__).parent / OUTPUT_DIR
    else:
        # 自动找下一个可用的 output_N 编号
        root = base if args.base_dir else Path(__file__).parent
        n = 1
        while (root / f"output_{n}").exists():
            n += 1
        OUTPUT_DIR = root / f"output_{n}"

    if args.bgm_dir:
        BGM_DIR = Path(args.bgm_dir)
        if not BGM_DIR.is_absolute():
            BGM_DIR = Path(__file__).parent / BGM_DIR

    OUTPUT_DIR.mkdir(exist_ok=True)

    print("正在扫描素材...", flush=True)
    all_others, before_sources, after_sources, products_video, products_image = get_all_sources()
    print(f"  别人的视频: {len(all_others)} 个  (before片段:{len(before_sources)} after片段:{len(after_sources)})", flush=True)
    print(f"  产品视频: {len(products_video)} 个  产品图片: {len(products_image)} 个", flush=True)
    print(f"  竞品过滤: {'开启' if AVOID_BRANDS else '关闭'}", flush=True)

    num_videos = args.count
    print(f"\n将生成 {num_videos} 个混剪视频（15-20秒，连续音频+快剪）...\n", flush=True)

    success = 0
    fail = 0

    for i in range(1, num_videos + 1):
        seed = random.randint(0, 2**32)
        print(f"  [{i}/{num_videos}] mix_{i:03d}.mp4 ...", end=" ", flush=True)

        ok, msg = build_video(i, all_others, before_sources, after_sources, products_video, products_image, seed)
        if ok:
            try:
                dur, _ = get_video_info(OUTPUT_DIR / f"mix_{i:03d}.mp4")
                print(f"OK ({dur:.1f}s)", flush=True)
            except Exception:
                print("OK", flush=True)
            success += 1
        else:
            print(f"FAIL: {msg[:80]}", flush=True)
            fail += 1

    print(f"\n=== 完成 ===", flush=True)
    print(f"成功: {success}  失败: {fail}", flush=True)
    print(f"输出: {OUTPUT_DIR}", flush=True)

    if _source_use_count:
        print(f"\n=== 素材使用次数 ===", flush=True)
        for name, cnt in sorted(_source_use_count.items(), key=lambda x: -x[1]):
            print(f"  {cnt:3d}x  {name}", flush=True)


if __name__ == "__main__":
    main()
