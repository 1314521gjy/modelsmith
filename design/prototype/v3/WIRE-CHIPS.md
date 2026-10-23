# WIRE-CHIPS

- 2026-10-22 删除 Agent 接入：用户未要求该功能，且为无处理器的死按钮。
- 环境 chip 统一挂到 #envPanel，#envBody 由 wire 填充，#envClose 关闭。
- 训练页显存卡使用已有统计栅格，值由 wire 写入 #vramUsed。
- 炫彩仅由 body.training-glow 触发，CSS 动画在 prefers-reduced-motion: no-preference 下生效。
