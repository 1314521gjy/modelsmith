# ART-NOTES（美术层说明）

范围：美术本次只写三份独立件 `E:\视频\modelsmith\design\art\art.css`、`E:\视频\modelsmith\design\art\icons.html`、本件；
未碰只读锁定的 `E:\视频\modelsmith\design\prototype\index.html`。合并由 维护者 用脚本完成：
art.css 整体追加进原 `<style>` 末尾，icons.html 四段替换进对应 `.icn-slot`（其余 DOM 零改动）。
本件只登记本次覆盖层的事；更早一轮的美术自评已另行归档，不在此重复。

## 1 本次删掉的装饰（逐条）

1. 卡片阴影与卡片底色：`.card` 显式 `box-shadow:none;background:transparent`，面板只留 1px 十字发丝线。
2. 图标常亮品牌色：静止全 `--dim`，仅 hover/focus-within 转该线色（一次只亮一条线）。
3. 按钮「→」后缀、全大写小标签、每段 eyebrow、标题单词变色或斜体：CSS 不给任何对应样式。
4. 装饰性 01/02/03 编号：只保留步骤条 `.num`（序列是真信息）。
5. 等宽字体读数：改 `font-variant-numeric:tabular-nums`（`.est .v`、`.readouts .v`、`.path`、`.log` 等）。
6. 用「·」拼元信息、「WORD — fragment」式标签：本次文案一律不用（DESIGN-SPEC §2 禁令）。
7. 发光边框、渐变文字、鼠标光斑、粒子、骨架 shimmer、无限 spinner：art.css 内零对应规则。
8. hover 放大与阴影加深：只留 2px 抬升 + `--glass-hi` 底 + `--line-strong` 描边。
9. hero 大数字与说明段：标题中性，数字只出现在预估与运行读数。
10. 卡片投影改浮层专用：阴影只给 `.pop` / `.drawer`（`0 8px 32px rgba(0,0,0,.45)`）。

## 2 token 变更与理由

- 色板 16 项（`--bg/--bg2/--glass/--glass-hi/--line/--line-strong/--text/--dim/--laya/--llm/--image/--video/--danger/--warn/--focus`）
  按 DESIGN-SPEC §1 收进 `:root` 重定义：数值不改，散值统一为 token，组件内不再写零散 hex。
- `--sp-1..5` 改为 `8/16/24/32/48px`：对齐 DESIGN-SPEC §3 的 8pt 阶（原型旧值 4/8/12/16/24 偏小一档）。
- `--t-fast:120ms / --t-step:200ms / --t-enter:260ms / --ease:cubic-bezier(.22,.61,.36,1)`：动效参数收归 token。
- `--cn` 追加 `"Microsoft YaHei"` 第三回落位：断网且无 Noto Sans SC 时中文不塌，仍零外部字体。
- 次级圆角 20px/10px：**以字面量登记**（原计划建 `--r-sub/--r-sub-ctrl` token，实际未建，CSS 里 0 命中 —— 见 `review\FINAL-AUDIT-v2.md` 的 R1）。

## 3 新增类与次级圆角登记（CONTRACT-UI-v1 §2：类名只许增不许改）

- 新增类：0 个。art.css 只用原型已有选择器；icons.html 的四段沿用冻结类 `.icn`（元素上同时带原型已有的 `.icn-slot`）。
- 次级圆角登记：`.aside`、`.card-box` = 20px（`--r-sub`，次级面板低主档一档）；`.xbtn` = 10px（`--r-sub-ctrl`，次级小控件）。
  主三档保持面板 24 / 控件 12 / 小签 8。
- 进程缝（不改 DOM）：`nav.steps::before` 2px 线，`.step-item[aria-current="step"] .num` 高亮、`.step-item.done .num` 填色。

## 4 未做项

- 对比度未实测（DESIGN-SPEC §6 目标值，实测归 P-qa 的 QA H 组）。
- 入场「每会话只播一次、返回不重播」由原型 JS 的 cardsPlayed 控制，CSS 不重复实现。
- loss 描线动画、整页横滑按禁令未做；四图标未替换进 index.html（等 维护者 脚本合并）。

## 5 事故留碑

2026-10-22 前一美术实例两次整文件覆写 index.html 丢失 JS，已逐字节恢复；此后美术只交素材、维护者 脚本合并。

## 6 fix-round1 视觉 token 变更（框架席，依 QA 修复轮 1）

- `--line`：`rgba(255,255,255,.12)` → `.38`；`--line-strong`：`.22` → `.55`（H8：对 `--bg` 非文本对比度 ≥3:1，实测约 3.56 / 6.33）。
- `.chip` 高度 `36px` → `40px`（G4：触控目标 ≥40）。
- 新增 `@media (max-width:899px)`：首屏 `.grid2` 单列 + 卡片分割线改底边（G1；原 640 档保留）。
- `.field label` 一律加 `for` 指向控件 id（段/按钮组用 `aria-label`），标签文案改为类型说明以去掉与 h2 的逐字重复（H12 / A8）；无新增颜色或字体。

