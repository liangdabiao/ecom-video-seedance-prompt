"""replicate.py —— 由分镜表生成 Seedance 复刻提示词包。

输入 analyze.py 产出的 storyboard.json + 用户意图（复刻风格/产品补充信息），
一次生成调用（开推理，max_tokens=32768），产出：

  · 每镜头提示词（镜头A/B/C…，各≤200字，含时间锚点与@图占位）
  · 整片基础参数段（可直接拼接）+ 完整可粘贴整段提示词（≤800字）
  · 负面提示词（中英双语模板）
  · @参考图清单建议（产品图/人物图怎么拍）
  · 复刻差异说明 + 升级建议

渲染为 markdown + 自包含 HTML 预览（每镜头提示词旁配该镜头关键帧原图）。

用法：
  python replicate.py <video> [--style faithful|organic|paid] [--product "产品信息"]
                       [--duration 15] [--shot a,b,c] [--out seedance-out]
"""
import argparse
import base64
import hashlib
import html
import json
import re
import sys
import time
from pathlib import Path

from ds_client import DSClient, ChatError, parse_json_lenient

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SKILL_DIR = Path(__file__).resolve().parents[1]
CACHE_ROOT = SKILL_DIR / ".cache"
OUT_DIR = Path("seedance-out")

STYLE_DESC = {
    "faithful": "忠实复刻：尽最大程度还原原视频的画面、节奏与风格（仅把不可控元素替换为可控描述）",
    "organic": "自然流改版：在原视频结构基础上，按抖音自然流种草逻辑改写（真实场景、自然光、中景跟拍、痛点/种草叙事、无过度营销元素）",
    "paid": "付费流改版：在原视频结构基础上，按千川付费流逻辑改写（黄金3秒视觉奇观钩子、极致特写、硬光侧逆光、快节奏、结尾行动号召）",
}

SYSTEM = """你是资深AI编导，任务是把带货视频的分镜拆解表转写为 Seedance（即梦2.0）可用的复刻提示词。
严格遵循以下编导规则：

【结构】每条镜头提示词 = 主体 + 动作 + 场景 + 光影 + 镜头语言 + 色彩情绪 + 画质约束，五大要素(S-A-C-S-C)缺一不可。
【字数】单镜头≤200字（建议120）；整片完整版≤800字；宁精勿滥。
【动作】写慢、写连续、写过程：拆成时序步骤（A→B→C），用"缓慢/轻柔/连贯/自然/流畅"修饰；严禁剧烈动作、多人复杂交互。
【镜头】一条视频只用一种主运镜；5秒内不切换光影；运镜用标准术语（固定镜头/缓慢推镜dolly in/平稳横移pan/环绕/跟拍）。
【主体】人物用四层结构（身份→外貌→服装含具体颜色→气质表情物理化）；产品写"材质+纹理"双重描述和色号。
【表情】物理描述替代抽象词（"嘴角微微上扬"而非"开心"）。
【物理交互】加入贴合产品的交互细节（头发随风飘动/手指轻触/液体缓慢流动/布料垂坠）。
【约束尾】每条镜头提示词尾部带稳定性约束：人脸自然五官对称无脸崩无变形，人体结构正常，同一角色服装发型一致，画面无抖动无模糊。
【@语法】涉及产品/人物时用 @产品参考图1、@人物参考图1 占位（不发明不存在的素材）。
【带货逻辑】镜头用途对应结构：黄金3秒钩子→卖点展示→场景代入/效果对比→促单。
【克制原则】单镜头单重点，拒绝信息堆砌，剔除"好看/高级/有质感"等模糊词。

输出 JSON：
{"replication_strategy": "复刻策略说明（2-3句：怎么理解原片、本版提示词的取舍）",
 "base_params": "整片基础参数段（直接抄用：15秒，9:16竖屏，1080P高清，30fps帧率，电影质感，画面丝滑无卡顿，无抖动无闪烁，细节丰富，锐度清晰，色彩自然还原，主体结构正常比例自然——按原片实际比例/时长调整）",
 "shots": [{"id": "A", "time": "0-3s", "purpose": "该镜头用途", "prompt": "该镜头完整提示词（≤200字）"}],
 "full_prompt": "把所有镜头按时间锚点拼成的完整可粘贴提示词（≤800字，镜头间用'镜头A(0-3s)：…'分隔）",
 "negative_prompt_cn": "中文约束（负面）",
 "negative_prompt_en": "英文负面提示词",
 "reference_assets": [{"name": "@产品参考图1", "how": "拍摄/准备建议，如：正面纯色背景自然光，抠图锐化"}],
 "fidelity_notes": "与原片的差异说明：哪些元素被替换/省略，为什么（如品牌LOGO不可控、AI生成水印不复刻）",
 "upgrades": ["在原片基础上的1-3条升级建议，如增强钩子/补充促单/优化光影"]}

只输出 JSON。"""


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_storyboard(video: Path):
    sha = sha256_file(video)
    sb_path = CACHE_ROOT / sha[:16] / "storyboard.json"
    if not sb_path.exists():
        sys.exit(f"[error] 未找到分镜表，请先运行: python analyze.py \"{video}\"")
    return json.loads(sb_path.read_text("utf-8")), sb_path.parent


def storyboard_digest(sb, shot_ids=None):
    o = sb["overall"]
    lines = [
        f"原片：{sb['video_name']}，{sb['duration']:.1f}秒，{sb['width']}x{sb['height']}（比例≈{sb['width'] / sb['height']:.2f}）",
        f"全局：产品={o.get('product', '')}｜品类={o.get('category', '')}｜框架={o.get('framework', '')}｜"
        f"流量类型={o.get('flow_type', '')}｜钩子={o.get('hook', '')}",
        f"卖点：{'；'.join(o.get('selling_points', []) or [])}",
        f"人物设定：{o.get('person_setting', '')}",
        f"节奏：{o.get('rhythm', '')}",
        f"口播/文案摘要：{o.get('script_summary', '')}",
        f"行动号召：{o.get('cta', '')}",
        f"复刻难度：{o.get('replicate_difficulty', '')}",
        "",
        "分镜表：",
    ]
    for s in sb["shots"]:
        if shot_ids and s["id"] not in shot_ids:
            continue
        lines.append(
            f"镜头{s['id']}({s['start']:.1f}-{s['end']:.1f}s, {s['duration']:.1f}秒)｜{s['shot_type']}｜{s['angle']}｜"
            f"景深:{s['dof']}｜运镜:{s['camera_move']}｜光影:{s['lighting']}｜色彩:{s['color']}")
        lines.append(f"  人物：{s['subject_person']}")
        lines.append(f"  产品：{s['subject_product']}")
        lines.append(f"  场景：{s['setting']}")
        lines.append(f"  动作时序：{' → '.join(s['action_steps'])}")
        if s["interaction"]:
            lines.append(f"  物理交互：{s['interaction']}")
        if s["onscreen_text"]:
            lines.append(f"  画面文字：{s['onscreen_text']}")
        lines.append(f"  卖点：{s['selling_point']}｜用途：{s['purpose']}")
    return "\n".join(lines)


def generate(client, sb, style, product_info, duration):
    user_parts = [f"【分镜拆解表】\n{storyboard_digest(sb)}",
                  f"\n【生成要求】风格：{STYLE_DESC[style]}"]
    if product_info:
        user_parts.append(f"产品补充信息（以此为准）：{product_info}")
    user_parts.append(f"目标成片时长：{duration}秒（Seedance 支持4-15秒；镜头时长可按比例缩放）")
    user_parts.append("请生成本风格的完整复刻提示词包 JSON。")
    text, finish = client.chat([{"type": "text", "text": "\n".join(user_parts)}],
                               system=SYSTEM, thinking=True, max_tokens=32768, retries=3)
    if finish == "length":
        print("[warn] 生成被截断，尝试补救解析", flush=True)
    try:
        return parse_json_lenient(text)
    except ChatError:
        # 截断的 JSON：截到最后一个完整对象再试一次
        t = text[: text.rfind("}") + 1]
        return parse_json_lenient(t)


def md_inline(text: str) -> str:
    return html.escape(text)


def render_markdown(sb, data, style, video_name) -> str:
    o = sb["overall"]
    L = []
    L.append(f"# Seedance 复刻提示词 · {video_name}")
    L.append(f"\n**风格**：{STYLE_DESC[style]}  \n**原片判断**：{o.get('product', '')}｜"
             f"{o.get('framework', '')}｜{o.get('flow_type', '')}\n")
    L.append(f"\n## 复刻策略\n\n{data.get('replication_strategy', '')}\n")
    L.append(f"\n## 基础参数（拼在提示词开头）\n\n```\n{data.get('base_params', '')}\n```\n")
    L.append("\n## 分镜头提示词\n")
    for s in data.get("shots") or []:
        L.append(f"### 镜头{s.get('id', '?')}（{s.get('time', '')}）· {s.get('purpose', '')}\n")
        L.append(f"```\n{s.get('prompt', '')}\n```\n")
    L.append("\n## 完整版（直接粘贴即梦）\n")
    L.append(f"```\n{data.get('full_prompt', '')}\n```\n")
    L.append("\n## 负面提示词\n")
    L.append(f"- 中文约束：`{data.get('negative_prompt_cn', '')}`\n")
    L.append(f"- 英文：`{data.get('negative_prompt_en', '')}`\n")
    L.append("\n## @参考图清单\n")
    for r in data.get("reference_assets") or []:
        L.append(f"- **{r.get('name', '')}**：{r.get('how', '')}")
    L.append(f"\n## 与原片的差异说明\n\n{data.get('fidelity_notes', '')}\n")
    L.append("\n## 升级建议\n")
    for u in data.get("upgrades") or []:
        L.append(f"- {u}")
    return "\n".join(L)


def render_html(sb, data, style, cache_dir, md_text):
    kf_map = sb.get("keyframes", {})
    parts = [f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><title>Seedance 复刻提示词</title>
<style>
body{{font-family:system-ui,"Microsoft YaHei",sans-serif;max-width:960px;margin:0 auto;padding:24px;color:#222}}
.meta{{color:#666;font-size:14px}}
h2{{margin-top:32px;border-bottom:2px solid #e8e8e8;padding-bottom:6px}}
.shot{{margin:18px 0;padding:14px 18px;background:#f6f7f9;border-radius:10px}}
.prompt{{white-space:pre-wrap;background:#fff;border:1px solid #e3e3e8;padding:12px 16px;border-radius:8px;font-size:14px;line-height:1.7}}
img{{width:100%;border:1px solid #ddd;border-radius:6px;margin:4px 0}}
.kfs{{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:8px}}
code{{background:#e8e8ec;padding:1px 5px;border-radius:3px}}
.full{{white-space:pre-wrap;background:#fff;border:1px solid #e3e3e8;padding:14px 18px;border-radius:8px;font-size:14px;line-height:1.7}}
</style></head><body>
<h1>Seedance 复刻提示词</h1>
<div class="meta">{html.escape(sb['video_name'])} · {STYLE_DESC[style]}</div>
<h2>复刻策略</h2><div class="prompt">{html.escape(str(data.get('replication_strategy', '')))}</div>
<h2>基础参数</h2><div class="prompt">{html.escape(str(data.get('base_params', '')))}</div>
<h2>分镜头提示词（配原片关键帧）</h2>"""]
    for s in data.get("shots") or []:
        sid = str(s.get("id", ""))
        parts.append(f'<div class="shot"><h3>镜头{html.escape(sid)}（{html.escape(str(s.get("time", "")))}）· '
                     f'{html.escape(str(s.get("purpose", "")))}</h3>')
        parts.append(f'<div class="prompt">{html.escape(str(s.get("prompt", "")))}</div>')
        kfs = kf_map.get(sid) or []
        if kfs:
            parts.append('<div class="kfs">')
            for p, t in kfs:
                fp = Path(p)
                if not fp.is_file():
                    fp = cache_dir / "frames" / fp.name
                if fp.is_file():
                    b64 = base64.b64encode(fp.read_bytes()).decode()
                    parts.append(f'<div><img alt="t={t}s" src="data:image/jpeg;base64,{b64}">'
                                 f'<div class="meta">t={t}s</div></div>')
            parts.append("</div>")
        parts.append("</div>")
    parts.append(f'<h2>完整版（直接粘贴即梦）</h2><div class="full">{html.escape(str(data.get("full_prompt", "")))}</div>')
    parts.append(f'<h2>负面提示词</h2><div class="prompt">中文约束：{html.escape(str(data.get("negative_prompt_cn", "")))}'
                 f'\n\n英文：{html.escape(str(data.get("negative_prompt_en", "")))}</div>')
    parts.append("<h2>@参考图清单</h2><ul>")
    for r in data.get("reference_assets") or []:
        parts.append(f"<li><b>{html.escape(str(r.get('name', '')))}</b>：{html.escape(str(r.get('how', '')))}</li>")
    parts.append("</ul>")
    parts.append(f'<h2>与原片的差异说明</h2><div class="prompt">{html.escape(str(data.get("fidelity_notes", "")))}</div>')
    parts.append("<h2>升级建议</h2><ul>")
    for u in data.get("upgrades") or []:
        parts.append(f"<li>{html.escape(str(u))}</li>")
    parts.append("</ul></body></html>")
    return "".join(parts)


def main():
    ap = argparse.ArgumentParser(description="Generate Seedance replication prompts")
    ap.add_argument("video", type=Path)
    ap.add_argument("--style", choices=list(STYLE_DESC), default="faithful",
                    help="faithful=忠实复刻 / organic=自然流改版 / paid=付费流改版")
    ap.add_argument("--product", default="", help="产品补充信息（品牌/卖点/色号等，以此为准）")
    ap.add_argument("--duration", type=int, default=15, help="目标成片时长（4-15秒）")
    ap.add_argument("--shot", default="", help="只生成指定镜头，如 A,B")
    ap.add_argument("--out", type=Path, default=OUT_DIR)
    args = ap.parse_args()

    video = args.video.resolve()
    sb, cache_dir = load_storyboard(video)
    shot_ids = {x.strip().upper() for x in args.shot.split(",")} if args.shot else None
    print(f"[replicate] {sb['video_name']}: {len(sb['shots'])} shots, style={args.style}, "
          f"duration={args.duration}s", flush=True)

    client = DSClient()
    t0 = time.time()
    data = generate(client, sb, args.style, args.product, args.duration)
    print(f"[replicate] generated in {time.time() - t0:.0f}s: "
          f"{len(data.get('shots') or [])} shot prompts", flush=True)

    md = render_markdown(sb, data, args.style, sb["video_name"])
    args.out.mkdir(exist_ok=True)
    stem = sb["video_name"].rsplit(".", 1)[0][:40]
    style_tag = {"faithful": "忠实复刻", "organic": "自然流", "paid": "付费流"}[args.style]
    md_path = args.out / f"{stem}_{style_tag}_提示词.md"
    md_path.write_text(md, encoding="utf-8")
    html_text = render_html(sb, data, args.style, cache_dir, md)
    html_path = args.out / f"{stem}_{style_tag}_提示词.html"
    html_path.write_text(html_text, encoding="utf-8")

    print("\n=== 复刻策略 ===")
    print(data.get("replication_strategy", ""))
    print("\n=== 分镜头提示词 ===")
    for s in data.get("shots") or []:
        print(f"[镜头{s.get('id')} {s.get('time')} · {s.get('purpose')}]")
        print(s.get("prompt", ""))
        print()
    print("=== 完整版提示词 ===")
    print(data.get("full_prompt", ""))
    print("\n=== 负面提示词 ===")
    print(data.get("negative_prompt_cn", ""))
    print(data.get("negative_prompt_en", ""))
    print("\n=== 产出文件 ===")
    print(md_path.resolve())
    print(html_path.resolve())


if __name__ == "__main__":
    main()
