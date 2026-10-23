# UI 施工契约 v1（维护者 定 · 2026-10-22）

- 唯一产物：`E:\视频\modelsmith\design\prototype\index.html`（**单文件**；`file://` 直接开；零外链、零构建、断网可用）。
- 旧件已归档（**不要动**）：`E:\视频\modelsmith\design\prototype\archive\index-unreviewed-20261022.html`
  - 读数：47,219 B / 827 行 / SHA256 `09245B998A658E2B8E290558FB0630EBD31AD8124683058961CC4FF37F8F708F`
- 冲突优先级：**本文（技术接口与阶段）** > `E:\视频\modelsmith\docs\UI-BRIEF-MINI.md`（产品与页面） > `E:\视频\modelsmith\design\DESIGN-SPEC.md`（视觉） > `E:\视频\modelsmith\design\NOTES.md`（实现建议）。

---

## 1 阶段与写手（**同一时刻只允许一个写手改 index.html**）

| 阶段 | 角色 | 允许改 | 禁止改 |
|---|---|---|---|
| P-fix 框架修复 | `ui-frame` / DeepSeek V4.1 | DOM、JS 逻辑、必要的 CSS 结构、`:root` token | 视觉气质（色板、光晕、字阶、图标造型）不重做 |
| P-art 美术 | `ui-art` / **MiMo V2.6 Pro** | CSS 视觉层、内联 SVG 图标、动效参数 | state / render / 校验 / 流程数据一律不动 |
| P-qa 验收 | 验收 | 只写验收记录 | 不改 `index.html` |

**交接顺序**：P-fix 完成后（`send_message` 回报 + 产物可开）才起 P-art；P-art 完成后才起 P-qa 终审。任何人不得越阶段改同一个文件。

## 2 冻结的类名与结构（美术按这套写 CSS；改名=违约）

- 入口：`.card`（4 个，`data-flow` = `decision|llm|image|video`）、`.card__title`、`.card__sub`、`.icn`、`.dot`（`.dot--ok/--idle/--fail`）。
- 视图：`#home`、`#flow.flow.active`、`#run.run.active`、`#done.done-page.active`；一次只显示一个。
- 流程：`nav.steps > .step-item[aria-current="step"]`（`.done`、`.num`）；`.center > h2#stepH1[tabindex="-1"]`；`.aside .est > .k/.v`；底部 `.actionbar #prevBtn #nextBtn`。
- 表单：`.field`（错误态 `.field.err`）、`.field .msg`、`.field select/input`、`.seg button[aria-pressed]`、`.dropzone .path`、`details.adv`。
- 运行：`.timeline .tl-node`（`.done/.cur`）`.tn-dot` `.tn-label` `.tl-line`；`.losschart svg`；`.vram .bar i`；`.log .ln`；`.run__prog .bar i`；`.readouts .ro .k/.v`。
- 抽屉：`.drawer`（`.open`）、`.scrim`（`.open`）、`.switch[aria-checked]`、`.tool`、`.cfgbox`、`#tools`、`#cfgBox`、`#copyCfg`。
- 轻面板（环境/设置）：`.pop`、`#envPop`、`#setPop`（`hidden` 切换）—— **2026-10-22 由 ui-frame-2 新增，维护者 已登记**。
- 规则：**类名只许增不许改**；新增类必须先登记在本表并在 `send_message` 里告知 维护者。

## 3 P-fix 必须修好的 P0（行号针对归档 v1，可复核）

1. **真校验**：`validate()` 现在六个分支全 `return true`（v1 617–624）。改为按步骤校验必填/范围/枚举；错误用 `.field.err` + `.msg` + `aria-invalid` + `aria-describedby`，提交时焦点跳第一个错字段；`nextBtn` 的 `aria-disabled` 必须真能变 true（v1 561 恒 false）。
2. **状态真持久**：所有选择/输入写进 `state.data`；前进/后退/回首页再进都必须从 state 重绘（v1 476 的 `state.data` 从未被写入，495–505 每次进流程都清空）。`.seg` 点击后重渲染不得回落到第一项（v1 629–633 会）。
3. **预估真联动**：显存/步数/时长必须是 `f(样本数, epochs, batch, 架构)`；参数变了读数必须变（v1 419–458 全写死）。
4. **顶部「环境」「设置」不许是死按钮**（v1 250–251 无监听）：各自开一个面板或轻抽屉，可明标「未接后端」，但必须有可见响应。
5. **Agent 抽屉不许撒谎**：MCP 默认显示 `未连接`（v1 386 写「已连接 · localhost:8712」是假的）；配置示例按 `PLAN.md` §3.3 的 **stdio** 形状（`modelsmith-mcp.exe --autostart` + env），不要再出现 streamable_http/8712；不显示任何假 token。
6. **删假读数**：v1 318/337/674 的「RTX 4090 24GB」改为「本机 GPU（未检测）」或由环境面板读；本机实际是 RTX 4080 SUPER 32GB。
7. **文案禁令**：删掉用「·」拼元信息（v1 270/278/386/551 等，`DESIGN-SPEC.md` §2 明令禁止）。
8. **aria**：日志区 `role="log"` 且 `aria-live="polite"`（v1 342 是 off）；`runStatus` 常驻 polite；每个视图只有一个 h1。
9. **token 落地**：`:root` 补齐 `--r-panel/--r-ctrl/--r-tag`、`--w-max/--col-steps/--col-side`、`--sp-1..5`、`--t-fast/--t-step/--t-enter/--ease`（`NOTES.md` §1）；组件内不得再写零散 hex。
10. **保持**：单文件、`file://` 可开、零外链、`prefers-reduced-motion` 真分支、四条流程能走到底、`.card` 四卡等权。

## 4 判据（能变红，自测必报读数）

- 静态：`(Get-Content).Count`、`(Get-Item).Length`、`Select-String 'https?://'` **必须 0**、`'<link|@import'` **必须 0**、`'<script[^>]+src='` **必须 0**。
- 状态：第 4 步改 rank/batch → 上一步×3 → 下一步×3，值不变；回首页再进回到原步骤。
- 校验：清空必填 → 下一步 → 红边 + 文案 + 焦点在第一错字段；`aria-disabled` 变 true。
- 预估：batch 10→1，步数必须变。
- 死按钮：点「环境」「设置」必须出现面板（截图或 DOM 断言 `aria-expanded` / 面板可见）。
- 负控：把某条判据故意写坏必须能红；只报"我看过了"不算通过。

## 5 已知陷阱

- `:focus{outline:none}`（v1 25）必须配 `:focus-visible` 环，否则 D3 红。
- 本机 pwsh 是 5.1 + ACP 936：**改含中文的文件一律用 read/write/edit 工具**，不要 `Set-Content`；必须用 pwsh 读写时显式 `-Encoding UTF8`。
- 判"没有"必须全量计数，禁止 `Select-Object -First N` 截断。
- 不碰 `E:\视频\modelsmith\third_party\`（只读源码）。
- 覆盖前无需再备份（维护者 已归档 v1）；但改完必须报 SHA256 与行数。
