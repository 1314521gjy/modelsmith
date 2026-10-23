# ModelSmith UI 速写（MVP 设计席专用）

> 只读这一页 + `design/skills/frontend-design/SKILL.md`。不要读侦查大报告。
> 目标：先产出可点击原型，不接后端、不找外部素材、不花时间截图。

## 1. 产品
Windows 本地训练工作台。首屏四个并列入口：
1. 决策模型训练（Laya）
2. LLM 微调
3. 生图 LoRA 训练
4. 视频 LoRA 训练

点进去是引导式流程：选模型/底座 → 选数据 → 数据体检 → 参数 → 预估并开跑 → 运行 → 完成/导出。

## 2. 首屏
- 2×2 四卡等权；每卡：内联 SVG 线性图标、标题、一行副标题、状态点（可选）。
- 除标题、四卡、右上角“环境/设置/Agent 接入”外，不加其他内容。
- 背景深色 + 2 个低饱和径向光晕，缓慢呼吸，不抢内容。
- 四卡入场：60–80ms 错峰 fade + 8px rise，总时长 ≤700ms；只做一次。
- 图标 6–9s 上下漂浮 6–10px，错峰；hover 暂停并抬升 2px。

## 3. 流程页骨架
- 左侧步骤条（4–6 步）；中间一次只问一件事；右侧固定“本次预计：显存/步数/时长/产物”；底部“上一步 / 下一步（或开始训练）”。
- 高级参数默认折叠。
- 步骤切换 180–240ms 淡入 + 4px 位移；不要整页横滑。
- 运行页：阶段时间线、loss 曲线（SVG 即可）、VRAM 条、日志尾随、取消。
- 完成页：结果摘要、产物路径、打开文件夹、再训一次。
- Agent 抽屉：默认关闭，显示 MCP 状态、复制配置、工具列表。

## 4. 四条流程的步骤（原型必须覆盖）
- 决策：底座 → 数据（JSONL/文件夹）→ 体检 → 参数（epochs/lr/batch/max_len）→ 预估开跑 → 结果。
- LLM：模型 → 方法（LoRA/QLoRA）→ 数据（chat/instruction）→ 体检 → 参数（rank/alpha/lr/epochs）→ 预估开跑 → adapter。
- 生图：架构（FLUX/Z-Image/Qwen-Image/SDXL）→ 底模 → 图片文件夹（缩略图墙/caption 检查）→ 参数（分辨率/rank/lr/steps）→ 预估开跑 → 采样预览 + LoRA。
- 视频：架构（Wan/H3/LTX）→ 底模 → 视频+caption → 参数（帧数/分辨率/rank/lr/steps）→ 预估开跑 → 采样帧 + LoRA。

假数据可以，但步骤、返回、校验提示、预计值、进度感要真实。

## 5. 视觉 token（可微调，但保持克制）
```css
--bg:#080B10; --bg2:#0D1219;
--glass:rgba(255,255,255,.055); --line:rgba(255,255,255,.12);
--text:#E8EEF7; --dim:#93A1B5;
--laya:#5AD1A5; --llm:#7AA7FF; --image:#F0B35A; --video:#C08CFF;
--danger:#E06C75;
```
- 深色、低饱和、毛玻璃 `blur(18px)`；正文清晰，不糊。
- 中文 `"Noto Sans SC","Microsoft YaHei UI",sans-serif`；拉丁/数字 `"Manrope","Segoe UI",sans-serif`；数字 tabular-nums。
- 圆角 20–24px；间距 8pt；内容最大宽 1100–1200px。
- 同屏最多两个强调色；标题保持中性，颜色只给图标/细线/状态。
- 不用：赛博霓虹、紫蓝渐变堆满、发光边框、游戏 HUD、SaaS 卡片墙、全大写小标签、每个按钮加 →、粒子/鼠标光斑/无限 spinner。

## 6. 交付（只写 `E:\视频\modelsmith\design\`）
1. `DESIGN-SPEC.md` ≤120 行：token、字体、布局、动效、组件状态、可访问性、自评（点名删掉了什么）。
2. `prototype/index.html` **单文件**：内联 CSS/JS，无外部 CDN、无构建；四入口 + 四流程 + 运行/完成 + Agent 抽屉 + 移动端；键盘可达；`prefers-reduced-motion` 关闭动画。
3. `NOTES.md` ≤60 行：CSS 变量、组件边界、假数据、未接后端项。

验收：四个入口都能进/返回/走到开始与完成；无死链；断网打开正常；四卡等权；首屏无多余说明。
