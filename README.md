# 智能视频混剪流水线

基于 Python、FFmpeg 和视觉模型的视频处理工具集，覆盖素材分析、片段筛选、批量混剪、背景音乐替换与感知哈希去重。

## 工作流程

1. 使用 CLIP 或视觉语言模型识别素材中的 before / after 区间。
2. 使用 OCR 或 CLIP 检测品牌画面，结合黑帧和音频检测筛选片段。
3. 按“before → 产品 → after → 产品收尾”组织画面，批量输出竖屏视频。
4. 使用帧感知哈希筛选相似结果，并按需替换背景音乐。

默认输出为 1080 × 1920，目标时长 15–18 秒，参数位于 `batch_mix.py` 配置区。自动识别和去重属于辅助筛选，结果需要人工复核。

## 安装

建议使用 Python 3.10 或更高版本的独立环境，并安装 FFmpeg，将 `ffmpeg` 和 `ffprobe` 加入 PATH。

```bash
python -m venv .venv
# Windows PowerShell: .venv\Scripts\Activate.ps1
# macOS/Linux: source .venv/bin/activate
python -m pip install -r requirements.txt
ffmpeg -version
ffprobe -version
```

可通过 `FFMPEG`、`FFPROBE`、`YT_DLP` 环境变量覆盖可执行文件路径。模型首次使用需下载权重，CLIP 脚本支持 Apple MPS 或 CPU。

## 准备素材与运行

准备自己的素材目录，例如：

```text
demo/
├── 别人的视频/          # 获得授权的参考视频
├── 我们的品/            # 自有产品图片和视频
└── bgm/                 # 获得授权的背景音乐
```

```bash
# 本地 CLIP 分类；模型产品类型选项见脚本 --help
python classify_zones.py --base-dir demo
# 批量生成 5 条视频
python batch_mix.py 5 --base-dir demo --bgm-dir demo/bgm
# 在已有结果基础上生成并筛选低相似度结果
python run_unique.py --base-dir demo -n 5 --threshold 0.9
# 单独替换背景音乐
python replace_bgm.py --input demo/output --bgm demo/bgm --output demo/output_bgm
```

`run_unique.py` 使用项目根目录的 `bgm/` 作为音频目录，使用前应将自己的音乐放到该位置，或调整脚本中的参数。品牌扫描脚本默认扫描仓库根目录的 `别人的视频/`，自定义目录需修改其 `BASE_DIR` 配置。

可选的 `classify_zones_vlm.py` 调用 OpenRouter，需设置 `OPENROUTER_API_KEY` 环境变量；所选模型见该脚本配置，使用时会向服务发送抽取的视频帧，并可能产生 API 费用。`.env.example` 仅作配置示例，脚本不会自动加载 `.env`。

## 脚本说明

| 文件 | 用途 |
| --- | --- |
| batch_mix.py | 片段调度、音画处理、批量混剪 |
| classify_zones.py | CLIP before / after 区间分类 |
| classify_zones_vlm.py | 通过 OpenRouter 调用视觉模型分类 |
| scan_brands.py | OCR 品牌文字扫描 |
| scan_brands_clip.py | CLIP 品牌画面和涂抹动作扫描 |
| run_unique.py | 基于感知哈希的结果筛选 |
| replace_bgm.py | 批量替换背景音乐 |
| make_contact_sheet.py | 抽帧并生成素材缩略图 |
| fetch_tiktok.py / fetch_bgm.py | 可选的素材采集辅助脚本 |

采集脚本需要额外执行 `python -m playwright install chromium`，并自行在本地配置 `tiktok_cookies.txt`。仅处理有权使用的素材，并遵守来源平台规则。

## 展示范围

仓库不包含登录 Cookie、API 密钥、业务文档、视频素材、音乐及生成结果。原机器的固定工具路径已改为读取环境变量或 PATH。发布前仅完成 Python 语法验证和配置变更检查，未执行视频渲染、模型推理或外部 API 调用。
