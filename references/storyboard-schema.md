# 分镜表结构与提示词包 Schema

## storyboard.json（.cache/<视频sha前16位>/storyboard.json）

```jsonc
{
  "video": "绝对路径", "video_name": "xxx.mp4", "sha256": "缓存键",
  "model": "deepseek-v4-flash-vision-exp", "created": "ISO时间",
  "duration": 10.1, "width": 834, "height": 1112,
  "sample_fps": 1.0,           // 抽帧密度（≤120s→1，更长→0.5）
  "scene_cuts": [2.58, 4.58, 7.42],   // ffmpeg scene>0.25 检测
  "overall": {                 // 全片带货全局分析（每镜头中间帧 1 次调用）
    "product": "产品判断", "category": "美妆/食品/3C/家居/服饰/母婴/其他",
    "person_setting": "人物设定总结",
    "framework": "口播介绍型/场景代入型/对比测评型/细节种草型/剧情植入型/产品展示型",
    "flow_type": "自然流/付费流/通用 + 理由",
    "hook": "钩子类型与内容",
    "selling_points": ["卖点"],
    "script_summary": "口播/文案摘要（推断）",
    "rhythm": "节奏结构（镜头时长分配+情绪曲线）",
    "cta": "行动号召",
    "replicate_difficulty": "易还原/难还原元素"
  },
  "shots": [                   // 每镜头一张拆解卡（S-A-C-S-C，关推理+JSON）
    {"id": "A", "start": 0.0, "end": 2.58, "duration": 2.58,
     "shot_type": "景别", "angle": "视角", "dof": "景深",
     "camera_move": "运镜+速度", "lighting": "光源+光位+质感",
     "color": "色调+饱和+对比",
     "subject_person": "四层人物描述", "subject_product": "产品描述",
     "setting": "场景三层", "action_steps": ["时序步骤→…"],
     "interaction": "物理交互", "onscreen_text": "画面文字",
     "audio_cues": "声音线索（推断）",
     "selling_point": "本镜头卖点", "purpose": "钩子/展示/对比/代入/促单"}
  ],
  "keyframes": {"A": [["frames/f00000.jpg 绝对路径", 0.0], ...]}
}
```

同目录 `frames/f00000.jpg…` 为抽帧（q3，单边≤3600px）。

## replicate.py 产出的提示词包（生成调用的 JSON）

```jsonc
{
  "replication_strategy": "复刻策略 2-3 句",
  "base_params": "基础参数段（时长/比例/画质/帧率/质感约束）",
  "shots": [{"id": "A", "time": "0-3.9s", "purpose": "用途", "prompt": "≤200字提示词"}],
  "full_prompt": "完整可粘贴整段（≤800字，镜头A(0-3.9s)：… 分隔）",
  "negative_prompt_cn": "中文负面约束", "negative_prompt_en": "英文负面提示词",
  "reference_assets": [{"name": "@产品参考图1", "how": "拍摄建议"}],
  "fidelity_notes": "与原片差异说明（LOGO/水印等不可控元素处理）",
  "upgrades": ["升级建议"]
}
```

## 风格开关（--style）

- `faithful` 忠实复刻：最大程度还原原片（仅替换不可控元素）
- `organic` 自然流：真实场景/自然光/种草叙事/补促单（参考"自然流五维"）
- `paid` 付费流：视觉奇观钩子/极致特写/硬光/快节奏/行动号召

## 实现约定

- 镜头切分：scene cuts（合并 <0.8s 碎片）；无切换 → 整片一镜
- 每镜头关键帧 ≤4 张（首/1/4/中/3/4/尾），标签 `[t=X.Xs]`，base64 内联
  （视频短帧少，无需 Files API）
- 全局分析输入 = 每镜头中间帧 + 拆解卡摘要
- 生成：开推理 + max_tokens 32768 + JSON；截断时截尾补救解析
- 时间锚点按 `--duration` 目标时长等比缩放（10s 原片 → 15s 成片）
