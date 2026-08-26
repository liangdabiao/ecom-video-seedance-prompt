"""analyze.py —— 带货视频镜头级拆解（每份视频一次，缓存复用）。

实现"反向拆解十招"的自动化：
1. 1fps 抽帧（>120s 自动降 0.5fps）+ ffmpeg 场景切换检测 → 切分镜头
2. 每镜头取首/中/尾关键帧，一次视觉调用产出「镜头卡」：
   景别/视角/景深/运镜/光影/色彩/主体(人+产品)/动作时序/物理交互/字幕/卖点用途
3. 全片一次综合调用（每镜头 1 帧）产出「带货全局分析」：
   产品与品类/人物设定/带货框架/自然流或付费流/钩子/卖点/口播摘要/节奏结构
4. 落盘 storyboard.json（标准分镜表），供 replicate.py 生成 Seedance 提示词

用法：
  python analyze.py <video> [--fps auto] [--min-shot 0.8] [--force] [--clean]
"""
import argparse
import base64
import hashlib
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from ds_client import DSClient, ChatError

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SKILL_DIR = Path(__file__).resolve().parents[1]
CACHE_ROOT = SKILL_DIR / ".cache"
MAX_SIDE_PX = 3600

SHOT_SYSTEM = """你是专业的AI编导，正在做带货视频的反向拆解（把看到的画面翻译成可复刻的编导指令）。
你会收到一个镜头内的若干帧图像（按时间顺序，每帧前有 [t=X.Xs] 标签）。

请按 S-A-C-S-C 框架输出这个镜头的结构化拆解，JSON 格式：
{"shot_type": "景别：极致特写/特写/近景/中景/全景/远景 之一",
 "angle": "视角：俯拍/平视/仰拍，附简短说明",
 "dof": "景深：浅(背景虚化)/深(前后清晰)",
 "camera_move": "运镜：固定镜头/缓慢推镜(dolly in)/平稳横移(pan)/环绕/跟拍/拉远(zoom out) 等，标注速度",
 "lighting": "光影：光源类型+光位+质感，如'暖光柔光，侧逆光，丁达尔效应'",
 "color": "色彩：色调+饱和度+对比度，如'暖色调，高饱和，对比适中'",
 "subject_person": "人物描述（四层：身份/外貌/服装含颜色/气质与表情的物理描述），无人出镜则为空字符串",
 "subject_product": "产品/商品描述：品类、外观、颜色、材质、包装、可见文字LOGO",
 "setting": "场景：场景类型+风格+环境细节",
 "action_steps": ["动作时序拆解：3-5个关键帧步骤，写慢写连续写过程"],
 "interaction": "物理交互：主体与产品/环境的接触细节（手指轻触/液体流动/布料摆动等），无则空字符串",
 "onscreen_text": "画面内可见文字（字幕/贴片/LOGO），无则空字符串",
 "audio_cues": "从画面推断的声音线索（口播主题/BGM情绪/产品音效），注明是推断",
 "selling_point": "本镜头承载的卖点或功能",
 "purpose": "在带货结构中的用途：黄金3秒钩子/卖点展示/效果对比/场景代入/信任建立/促单行动号召 之一或组合"}

只描述画面里可见的内容，不脑补看不到的信息。只输出 JSON。"""

OVERALL_SYSTEM = """你是电商带货视频的AI编导分析师。你会看到一个带货/商广视频每个镜头的中间时刻画面（按时间顺序，每帧前有 [镜头X t=X.Xs] 标签），以及各镜头的拆解卡。

请输出整条视频的全局分析，JSON 格式：
{"product": "带货产品判断：品名/品牌/品类",
 "category": "品类：美妆/食品/3C数码/家居/服饰/母婴/其他",
 "person_setting": "出镜人物设定总结（无人出镜则为空字符串）",
 "framework": "带货框架：口播介绍型/场景代入型/对比测评型/细节种草型/剧情植入型/产品展示型(无人出镜) 之一",
 "flow_type": "流量类型判断：自然流(真实感种草)/付费流(视觉冲击强转化)/通用，附判断理由",
 "hook": "开头钩子类型与内容（痛点提问/视觉奇观/结果前置/福利/悬念等）",
 "selling_points": ["全片核心卖点列表"],
 "script_summary": "口播/文案内容摘要（从字幕和画面推断，注明推断）",
 "rhythm": "节奏结构：每镜头时长分配+情绪曲线描述，如'0-3s平静引入，3-7s上升展示，7-10s高潮收尾'",
 "cta": "行动号召/转化引导内容，无则为空字符串",
 "replicate_difficulty": "复刻难度评估：哪些元素Seedance容易还原，哪些难（如特定品牌LOGO、多人交互）"}

只输出 JSON。"""


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ffprobe(path: Path) -> dict:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
         str(path)], capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        sys.exit(f"[error] ffprobe failed: {r.stderr[:300]}")
    data = json.loads(r.stdout)
    v = next(s for s in data.get("streams", []) if s.get("codec_type") == "video")
    return {"width": v["width"], "height": v["height"],
            "duration": float(data.get("format", {}).get("duration", 0) or 0)}


def extract_frames(video: Path, out_dir: Path, fps: float):
    out_dir.mkdir(parents=True, exist_ok=True)
    files = sorted(out_dir.glob("f_*.jpg"))
    if files:
        return files
    vf = f"fps={fps}"
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height", "-of", "csv=p=0", str(video)],
        capture_output=True, text=True, timeout=60)
    try:
        w, h = (int(x) for x in r.stdout.strip().split(","))
        if max(w, h) > MAX_SIDE_PX:
            vf += f",scale={MAX_SIDE_PX}:{MAX_SIDE_PX}:force_original_aspect_ratio=decrease"
    except ValueError:
        pass
    r = subprocess.run(["ffmpeg", "-y", "-i", str(video), "-vf", vf, "-q:v", "3",
                        "-start_number", "0", str(out_dir / "f_%05d.jpg")],
                       capture_output=True, text=True, timeout=1800)
    if r.returncode != 0:
        sys.exit(f"[error] ffmpeg extract failed: {r.stderr[-300:]}")
    return sorted(out_dir.glob("f_*.jpg"))


def detect_scene_cuts(video: Path) -> list:
    r = subprocess.run(
        ["ffmpeg", "-i", str(video), "-vf", "select='gt(scene,0.25)',showinfo", "-f", "null", "-"],
        capture_output=True, text=True, timeout=1800)
    cuts = []
    for line in r.stderr.splitlines():
        if "pts_time:" in line:
            try:
                cuts.append(round(float(line.split("pts_time:")[1].split()[0]), 2))
            except (IndexError, ValueError):
                pass
    return cuts


def build_shots(cuts, duration, min_shot):
    """场景切换点 → 镜头区间列表 [(start,end),...]；无切换则整片一个镜头。"""
    bounds = [0.0] + [c for c in cuts if min_shot < c < duration - min_shot] + [duration]
    shots, start = [], bounds[0]
    for b in bounds[1:]:
        if b - start >= min_shot:
            shots.append((round(start, 2), round(b, 2)))
            start = b
    if shots and start < duration:
        shots[-1] = (shots[-1][0], duration)
    return shots or [(0.0, duration)]


def b64_block(path, label):
    b64 = base64.b64encode(Path(path).read_bytes()).decode()
    return [{"type": "text", "text": label},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]


def frames_in_shot(frame_files, fps, start, end, max_n=4):
    """取镜头内首/中/尾关键帧（最多 max_n 张），返回 (帧路径, 时刻) 列表。"""
    idxs = [i for i in range(len(frame_files)) if start - 1e-6 <= i / fps <= end + 1e-6]
    if not idxs:
        i0 = min(range(len(frame_files)), key=lambda k: abs(k / fps - (start + end) / 2))
        idxs = [i0]
    if len(idxs) > max_n:
        pick = sorted(set([idxs[0], idxs[len(idxs) // 2], idxs[-1],
                           idxs[len(idxs) // 4], idxs[3 * len(idxs) // 4]]))[:max_n]
        idxs = sorted(pick)
    return [(frame_files[i], round(i / fps, 2)) for i in idxs]


def normalize_shot(rec: dict, start, end) -> dict:
    rec["start"], rec["end"] = start, end
    rec["duration"] = round(end - start, 2)
    for k in ("shot_type", "angle", "dof", "camera_move", "lighting", "color",
              "subject_person", "subject_product", "setting", "interaction",
              "onscreen_text", "audio_cues", "selling_point", "purpose"):
        rec[k] = str(rec.get(k, "")).strip()
    rec["action_steps"] = [str(s).strip() for s in (rec.get("action_steps") or []) if str(s).strip()][:6]
    return rec


def analyze_shot(client, shot_no, start, end, kf):
    blocks = []
    for p, t in kf:
        blocks += b64_block(p, f"[t={t:.1f}s]")
    blocks.append({"type": "text",
                   "text": f"这是镜头{shot_no}（{start:.1f}s-{end:.1f}s，共{end - start:.1f}秒）的{len(kf)}帧抽样。请输出该镜头的拆解 JSON。"})
    try:
        data, _ = client.chat_json(blocks, system=SHOT_SYSTEM, thinking=False, max_tokens=4096)
        return normalize_shot(data if isinstance(data, dict) else {}, start, end)
    except ChatError as e:
        print(f"[shot] 镜头{shot_no} analysis failed: {e}", flush=True)
        return normalize_shot({}, start, end)


def analyze_overall(client, shots):
    blocks = []
    for s in shots:
        kf = s["_keyframes"]
        blocks += b64_block(kf[len(kf) // 2][0], f"[镜头{s['id']} t={s['start']:.1f}s]")
    digest = "\n".join(
        f"镜头{s['id']}({s['start']:.1f}-{s['end']:.1f}s): {s['shot_type']}｜{s['camera_move']}｜"
        f"{s['lighting']}｜产品:{s['subject_product'][:40]}｜用途:{s['purpose']}"
        for s in shots)
    blocks.append({"type": "text", "text": f"各镜头拆解卡摘要：\n{digest}\n\n请输出整条视频的全局分析 JSON。"})
    data, _ = client.chat_json(blocks, system=OVERALL_SYSTEM, thinking=False, max_tokens=3000)
    return data if isinstance(data, dict) else {}


def main():
    ap = argparse.ArgumentParser(description="Shot-level decomposition of an e-commerce video")
    ap.add_argument("video", type=Path)
    ap.add_argument("--fps", type=float, default=0, help="抽帧率（默认：≤120s→1fps，更长→0.5fps）")
    ap.add_argument("--min-shot", type=float, default=0.8, help="最短镜头时长（秒）")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--clean", action="store_true")
    args = ap.parse_args()

    video = args.video.resolve()
    if not video.is_file():
        sys.exit(f"video not found: {video}")
    sha = sha256_file(video)
    cache_dir = CACHE_ROOT / sha[:16]
    if args.clean:
        if cache_dir.exists():
            for f in cache_dir.glob("*"):
                f.unlink()
            cache_dir.rmdir()
            print(f"[clean] removed {cache_dir}")
        return

    meta = ffprobe(video)
    duration = meta["duration"]
    fps = args.fps if args.fps > 0 else (1.0 if duration <= 120 else 0.5)
    sb_path = cache_dir / "storyboard.json"
    if sb_path.exists() and not args.force:
        print(f"[cache] storyboard exists: {sb_path}")
        return

    print(f"[analyze] {video.name}: {meta['width']}x{meta['height']}, {duration:.1f}s, fps={fps}", flush=True)
    t0 = time.time()
    frame_files = extract_frames(video, cache_dir / "frames", fps)
    cuts = detect_scene_cuts(video)
    shot_ranges = build_shots(cuts, duration, args.min_shot)
    print(f"[shots] {len(frame_files)} frames, {len(cuts)} scene cuts -> {len(shot_ranges)} shots: "
          f"{[f'{a:.1f}-{b:.1f}s' for a, b in shot_ranges]}", flush=True)

    client = DSClient()
    shots = []
    for i, (a, b) in enumerate(shot_ranges):
        kf = frames_in_shot(frame_files, fps, a, b)
        card = analyze_shot(client, chr(ord("A") + i), a, b, kf)
        card["id"] = chr(ord("A") + i)
        card["_keyframes"] = [(str(p), t) for p, t in kf]
        shots.append(card)
        print(f"[shot] 镜头{card['id']} done: {card['shot_type']}｜{card['camera_move']}｜"
              f"{(card['selling_point'] or card['purpose'])[:40]} ({time.time() - t0:.0f}s)", flush=True)

    overall = analyze_overall(client, shots)
    print(f"[overall] 产品: {overall.get('product', '?')}｜框架: {overall.get('framework', '?')}｜"
          f"{overall.get('flow_type', '?')}", flush=True)

    keyframes = [{k: v for k, v in s.items() if k != "_keyframes"} for s in shots]
    sb = {
        "video": str(video), "video_name": video.name, "sha256": sha,
        "model": client.model, "created": datetime.now().isoformat(timespec="seconds"),
        "duration": duration, "width": meta["width"], "height": meta["height"],
        "sample_fps": fps, "scene_cuts": cuts,
        "overall": overall, "shots": keyframes,
        "keyframes": {s["id"]: s["_keyframes"] for s in shots},
    }
    sb_path.write_text(json.dumps(sb, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[done] storyboard -> {sb_path} ({time.time() - t0:.0f}s)", flush=True)


if __name__ == "__main__":
    main()
