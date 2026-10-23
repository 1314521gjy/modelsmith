#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Laya 训练语料校验器（零依赖，stdlib）。

用法：
    python tools\\validate_laya_corpus.py <file.jsonl> [--max-choice 20] [--quiet]

判据（任一 FAIL 即退出码 1）：
  - 每行是 JSON 对象，含 state / questions / gold
  - state 是对象
  - questions 是对象，每项含 type ∈ {choice,score,noul} + instructions（非空）
  - choice: criteria 是 {label: 描述}；标签数 2..max_choice
  - score:  criteria 是有序数组，长度 2..7
  - noul:   不带 criteria
  - gold 与 questions 同键；每项 {"probabilities": {...}}
      choice → 键集合 == criteria 键集合
      score  → 键集合 == {"0".."n-1"}
      noul   → 键集合 == {"true","false"}
  - 每个问题的概率和 = 1（容差 1e-3），且都在 [0,1]
WARN（不阻断）：
  - state 估算 token 超底座上限（默认 512；--max-tokens 可调）
  - 某问题只有一个标签概率为 1（硬标签，校准会当"确定"）
  - 重复（state 完全相同）
  - 某问题的答案分布长期单一（>95% 同一标签）
"""
import argparse
import json
import sys
from collections import Counter, defaultdict

TYPES = {"choice", "score", "noul"}


def est_tokens(text):
    # 粗估：中文 1 字≈1.5 token，其他 1 字符≈0.3 token
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return int(cjk * 1.5 + (len(text) - cjk) * 0.3)


class Report:
    def __init__(self):
        self.fails = []
        self.warns = []

    def fail(self, line, msg):
        self.fails.append((line, msg))

    def warn(self, line, msg):
        self.warns.append((line, msg))


def check_row(row, idx, rep, max_choice, max_tokens):
    where = "L%d" % idx
    if not isinstance(row, dict):
        return rep.fail(where, "整行不是 JSON 对象")
    for k in ("state", "questions", "gold"):
        if k not in row:
            return rep.fail(where, "缺字段 %s" % k)
    state, questions, gold = row["state"], row["questions"], row["gold"]
    if not isinstance(state, dict) or not state:
        return rep.fail(where, "state 必须是非空对象")
    text = "\n".join(str(v) for v in state.values())
    tk = est_tokens(text)
    if tk > max_tokens:
        rep.warn(where, "state 估算 %d tokens > 上限 %d（底座可能截断）" % (tk, max_tokens))

    if not isinstance(questions, dict) or not questions:
        return rep.fail(where, "questions 必须是非空对象")
    if not isinstance(gold, dict):
        return rep.fail(where, "gold 必须是对象")
    if set(questions) != set(gold):
        return rep.fail(
            where,
            "gold 与 questions 键不一致：questions=%s gold=%s"
            % (sorted(questions), sorted(gold)),
        )

    for name, q in questions.items():
        w = "%s[%s]" % (where, name)
        if not isinstance(q, dict):
            rep.fail(w, "问题不是对象")
            continue
        t = q.get("type")
        if t not in TYPES:
            rep.fail(w, "type=%r 非法（只能是 choice/score/noul）" % t)
            continue
        ins = q.get("instructions")
        if not isinstance(ins, str) or not ins.strip():
            rep.fail(w, "instructions 为空")
        crit = q.get("criteria")

        if t == "choice":
            if not isinstance(crit, dict) or not crit:
                rep.fail(w, "choice 缺 criteria（应为 {标签: 描述}）")
                continue
            n = len(crit)
            if n < 2:
                rep.fail(w, "choice 只有 1 个标签")
            if n > max_choice:
                rep.fail(w, "choice 标签 %d 个 > 上限 %d" % (n, max_choice))
            if any(not isinstance(k, str) or not k for k in crit):
                rep.fail(w, "choice 标签名必须是非空字符串")
            keys = set(crit)
        elif t == "score":
            if not isinstance(crit, list) or not crit:
                rep.fail(w, "score 缺 criteria（应为有序档位数组）")
                continue
            if not (2 <= len(crit) <= 7):
                rep.fail(w, "score 档位 %d 个，建议 2..7" % len(crit))
            keys = {str(i) for i in range(len(crit))}
        else:  # noul
            keys = {"true", "false"}

        g = gold.get(name)
        if not isinstance(g, dict) or "probabilities" not in g:
            rep.fail(w, "gold 应为 {\"probabilities\": {...}}")
            continue
        probs = g["probabilities"]
        if not isinstance(probs, dict) or not probs:
            rep.fail(w, "probabilities 不是非空对象")
            continue
        if set(probs) != keys:
            rep.fail(w, "概率键与 criteria 不一致：多 %s 少 %s"
                     % (sorted(set(probs) - keys)[:5], sorted(keys - set(probs))[:5]))
            continue
        try:
            vals = [float(v) for v in probs.values()]
        except (TypeError, ValueError):
            rep.fail(w, "概率不是数字")
            continue
        if any(v < -1e-9 or v > 1 + 1e-9 for v in vals):
            rep.fail(w, "概率越界（应 0..1）")
        s = sum(vals)
        if abs(s - 1.0) > 1e-3:
            rep.fail(w, "概率和 %.4f ≠ 1" % s)
        if max(vals) >= 0.999:
            rep.warn(w, "硬标签（某一项概率≈1），校准会当\"确定\"")


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--max-choice", type=int, default=20)
    ap.add_argument("--max-tokens", type=int, default=512,
                    help="state 估算 token 上限（标准底座 512 / 题型版 1024 / 多语言长上下文可更大）")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)

    rep = Report()
    seen = {}
    answers = defaultdict(Counter)
    rows = 0
    bad_json = 0
    try:
        fh = open(a.path, encoding="utf-8-sig")  # 容忍 BOM
    except OSError as exc:
        print("FAIL 打不开文件：%s" % exc)
        return 1
    with fh:
        for idx, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            rows += 1
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                bad_json += 1
                rep.fail("L%d" % idx, "JSON 解析失败：%s" % exc)
                continue
            check_row(row, idx, rep, a.max_choice, a.max_tokens)
            if isinstance(row, dict) and isinstance(row.get("state"), dict):
                key = json.dumps(row["state"], ensure_ascii=False, sort_keys=True)
                if key in seen:
                    rep.warn("L%d" % idx, "state 与 L%d 重复" % seen[key])
                else:
                    seen[key] = idx
            if isinstance(row, dict) and isinstance(row.get("gold"), dict):
                for qn, g in row["gold"].items():
                    try:
                        p = g.get("probabilities") or {}
                        best = max(p.items(), key=lambda kv: float(kv[1]))[0]
                        answers[qn][best] += 1
                    except Exception:
                        pass

    if rows == 0:
        print("FAIL 文件没有有效行")
        return 1
    if not a.quiet:
        for ln, msg in rep.fails[:40]:
            print("FAIL %s  %s" % (ln, msg))
        if len(rep.fails) > 40:
            print("... 还有 %d 条 FAIL 未显示" % (len(rep.fails) - 40))
        for ln, msg in rep.warns[:20]:
            print("WARN %s  %s" % (ln, msg))
        if len(rep.warns) > 20:
            print("... 还有 %d 条 WARN 未显示" % (len(rep.warns) - 20))
        for qn, c in sorted(answers.items()):
            total = sum(c.values()) or 1
            top, n = c.most_common(1)[0]
            if n / total > 0.95 and total >= 20:
                print("WARN 分布  %s 有 %.0f%% 都是 %r（%d 条），模型会只学会这一个答案"
                      % (qn, 100.0 * n / total, top, total))

    status = "PASS" if not rep.fails else "FAIL"
    print("RESULT %s  rows=%d  fails=%d  warns=%d  file=%s"
          % (status, rows, len(rep.fails), len(rep.warns), a.path))
    return 0 if status == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
