# NOTICE

ModelSmith 自身的代码以 MIT 许可发布（见 `LICENSE`）。它**不含**任何模型权重，
也不分发第三方训练后端的源码树；下列组件由使用者按各自许可自行获取。

## 训练后端（源码不进本仓库，按 `third_party/MANIFEST.md` 固定 SHA 获取）

| 组件 | 固定点 | 许可证 | LICENSE 副本 |
|---|---|---|---|
| NandhaKishorM/laya | v0.4.13 | Apache-2.0 | `third_party/LICENSES/laya-LICENSE` |
| hiyouga/LlamaFactory | v0.9.5 | Apache-2.0 | `third_party/LICENSES/LlamaFactory-LICENSE` |
| ostris/ai-toolkit | main@14f8f950… | MIT | `third_party/LICENSES/ai-toolkit-LICENSE` |
| kohya-ss/sd-scripts | main@11d8cedd… | Apache-2.0 | `third_party/LICENSES/sd-scripts-LICENSE` |
| modelscope/DiffSynth-Studio | 本机克隆 | Apache-2.0 | `third_party/LICENSES/diffsynth-LICENSE` |

## 模型权重（本仓库一律不含）

- **Qwen-Image 2.1**：Qwen RESEARCH LICENSE AGREEMENT（**非商用**；商用须另行申请）。
  使用者自备权重并自担许可责任；本仓库只提供训练配方（`recipes/qwen_image_21_lora.md`）。
- Qwen2.5-0.5B-Instruct、SD1.5、Laya 底座等同理：各自许可见源站，本仓库不含权重文件。

## 语料

`resources/` 下只保留说明书与指针；语料本体（业务语料 / dolly-15k / 合成图集）作为
**单独的数据包**分发，逐条来源与许可见各目录的 `MANIFEST.md`。

## 设计

UI 设计方法参考 Anthropic `frontend-design`（Apache-2.0），副本见 `design/skills/frontend-design/`。
