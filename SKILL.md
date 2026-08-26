---
name: ecom-video-seedance-prompt
description: 带货视频反向拆解与 Seedance（即梦）复刻提示词生成。当用户提供带货/电商/广告/商销/种草/千川视频（或提到即梦、Seedance、文生视频、复刻爆款、拆解视频、提示词），想分析镜头、复刻效果、生成视频提示词时，使用本skill。自动产出镜头级分镜表（景别/运镜/光影/动作时序/卖点）和符合 S-A-C-S-C 编导规范的完整提示词包（分镜头提示词+基础参数+负面词+@参考图清单），支持忠实复刻/自然流/付费流三种风格。
---

# Ecom Video → Seedance Prompt

看懂一条带货视频，把它翻译成可复刻的 Seedance 提示词。两步走：

1. **analyze.py 拆解**：抽帧 + 场景切换检测切分镜头 → 视觉模型逐镜头产出
   S-A-C-S-C 拆解卡（景别/视角/景深/运镜/光影/色彩/主体/动作时序/物理交互/卖点）→
   全片带货全局分析（产品/框架/自然流或付费流/钩子/节奏）→ storyboard.json（缓存）
2. **replicate.py 生成**：分镜表 + 编导规则知识库 → 一次推理调用产出提示词包
   （每镜头≤200字 + 完整版≤800字 + 负面词中英 + @参考图清单 + 差异说明 + 升级建议）→
   markdown + 自包含 HTML（每镜头提示词配原片关键帧对照）

## 环境要求

- Python：`openai`；**ffmpeg/ffprobe 在 PATH**
- API key：环境变量 `DEEPSEEK_API_KEY` 或 `~/.deepseek_api_key` 首行（代码不内置密钥）

## 工作流

### 第 1 步：拆解（每份视频一次）

```bash
python scripts/analyze.py "<带货视频路径>"
```

输出分镜数与每个镜头的一句话摘要。常用：`--force` 重建｜`--min-shot 0.8` 最短镜头｜
`--clean` 清缓存。10 秒视频约 15-20 秒完成。

### 第 2 步：生成复刻提示词

```bash
python scripts/replicate.py "<视频路径>"                          # 忠实复刻
python scripts/replicate.py "<视频路径>" --style organic          # 自然流种草版
python scripts/replicate.py "<视频路径>" --style paid              # 付费流千川版
python scripts/replicate.py "<视频路径>" --product "品牌名，主打XX，色号XX"  # 补充产品信息（推荐）
```

参数：`--duration 15`（4-15 秒）｜`--shot A,B` 只重出指定镜头。
产出在 `./seedance-out/`：`<视频名>_<风格>_提示词.md` 和 `.html`。

### 第 3 步：向用户交付

1. **HTML 预览（首选）**：路径给用户双击打开——每镜头提示词旁就是原片关键帧，
   可逐镜对照检查是否抓住了原片精髓
2. 把「完整版提示词」直接贴给用户（可直接粘贴即梦）；提醒按「@参考图清单」准备素材
   （产品图纯色背景自然光拍摄、真人照片需打码/抠脸过审）
3. 用户不满意某镜头时，只带 `--shot X` 重出该镜头（省时省钱）
4. 需要变化时换 `--style` 重跑；产品信息以 `--product` 提供的为准

## 回答规范

- 交付时简述：原片是什么（产品/框架/流量类型判断）→ 复刻策略 → 提示词
- 提示词是否符合规范（S-A-C-S-C 齐全、≤200字/镜头、含约束尾、负面词）由 skill 保证，
  但要向用户说明：品牌 LOGO 等不可控元素已按"差异说明"处理
- 视频理解基于画面（无音频分析）；口播内容是从字幕/画面推断的，需注明

## 注意事项

- 生成用开推理 + max_tokens=32768（reasoning 吃预算，小了会空回复）
- 编导规则知识库在 `references/seedance-knowledge.md`（6 篇实战文档蒸馏），
  调整生成风格前先读它；分镜表结构见 `references/storyboard-schema.md`
- 单视频拆解一次永久缓存（.cache/<sha>/storyboard.json）；视频改了会自动另建
