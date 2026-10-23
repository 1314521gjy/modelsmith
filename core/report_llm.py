#!/usr/bin/env python3
"""Generate a Chinese four-section report with the local Qwen2.5-0.5B model.

Run with the ComfyUI embedded Python (transformers/torch are already there).
The parent core enforces the real timeout; this script also refuses to write a
partial report on failure.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(
    os.environ.get("MODELSMITH_HOME") or Path(__file__).resolve().parent.parent
).resolve()
DEFAULT_MODEL = str(ROOT / "resources" / "03-LLM-小模型" / "Qwen2.5-0.5B-Instruct")
SECTIONS = ("## 总体", "## 错例", "## 归因", "## 建议")
CAVEAT = "in-sample ≠ 泛化"


def _fmt(value, digits=4):
    try:
        return ("%." + str(digits) + "f") % float(value)
    except (TypeError, ValueError):
        return "unknown"


def build_prompt(payload: dict) -> str:
    metrics = payload.get("metrics") or {}
    errors = (payload.get("errors") or [])[:5]
    lines = [
        "你是模型评测报告助手。只根据下面给出的数字和错例写中文报告。",
        "硬约束：不得估算、推测或编造任何新的小数/百分比；只能引用下面已经出现的数字。",
        "必须输出四段，标题逐字使用：## 总体 / ## 错例 / ## 归因 / ## 建议。",
        "最后一节最后一行必须逐字包含：in-sample ≠ 泛化。",
        "",
        "数据来源：%s；样本数：%s" % (payload.get("data_source"), payload.get("n")),
        "metrics：",
    ]
    for key in (
        "accuracy",
        "loss",
        "ece",
        "brier_multi",
        "brier_top1",
        "mean_conf",
        "abstain_rate",
    ):
        lines.append("- %s = %s" % (key, _fmt(metrics.get(key))))
    lines.append("错例（最多 5 条）：")
    if errors:
        for index, item in enumerate(errors, 1):
            if isinstance(item, dict):
                lines.append(
                    "%d. 题目=%s；答案=%s；标注=%s"
                    % (
                        index,
                        str(item.get("q") or "")[:80],
                        str(item.get("answer") or "")[:80],
                        str(item.get("expected") or "")[:80],
                    )
                )
    else:
        lines.append("- 无逐条错例")
    lines.append("")
    lines.append("请开始写报告，不要输出任何额外解释。")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metrics", required=True)
    parser.add_argument("--out", required=True, help="output directory")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args(argv)
    started = time.monotonic()
    try:
        payload = json.loads(Path(args.metrics).read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict):
            raise ValueError("metrics JSON must be an object")
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        tokenizer = AutoTokenizer.from_pretrained(
            args.model, local_files_only=True, trust_remote_code=True
        )
        model = AutoModelForCausalLM.from_pretrained(
            args.model,
            local_files_only=True,
            trust_remote_code=True,
            torch_dtype=torch.float16,
            device_map="cuda",
        )
        model.eval()
        prompt = build_prompt(payload)
        messages = [
            {"role": "system", "content": "你是严谨的中文模型评测报告助手。"},
            {"role": "user", "content": prompt},
        ]
        text = tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True
        )
        inputs = tokenizer(text, return_tensors="pt").to(model.device)
        with torch.no_grad():
            generated = model.generate(
                **inputs,
                max_new_tokens=512,
                do_sample=False,
                temperature=None,
                top_p=None,
                pad_token_id=tokenizer.eos_token_id,
            )
        answer = tokenizer.decode(
            generated[0][inputs["input_ids"].shape[1] :],
            skip_special_tokens=True,
        ).strip()
        if not answer:
            raise RuntimeError("model generated an empty report")
        for title in ("总体", "错例", "归因", "建议"):
            answer = re.sub(
                r"(?m)^#{1,4}\s*" + title + r"[^\n]*$",
                "## " + title,
                answer,
                count=1,
            )
        if not all(section in answer for section in SECTIONS):
            raise RuntimeError("model output missing the four required sections")
        if CAVEAT not in answer:
            answer += "\n\n> " + CAVEAT + "：校准切片只代表训练分布，上线前请用 held-out 复测。\n"
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "report.md").write_text(answer + "\n", encoding="utf-8")
        (out_dir / "report.json").write_text(
            json.dumps(
                {
                    "engine": "llm",
                    "model": args.model,
                    "elapsed_s": round(time.monotonic() - started, 3),
                    "markdown": answer,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(json.dumps({"ok": True, "elapsed_s": round(time.monotonic() - started, 3)}))
        return 0
    except Exception as exc:
        print("report_llm failed: %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
