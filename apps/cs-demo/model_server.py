#!/usr/bin/env python3
"""客服决策模型服务：Laya（默认）或 4B（封存备选），127.0.0.1:8767。

Laya 分支只做两件判定：情绪(6 选 1) + 动作(8 选 1)，各带 choice 概率；
回复一律来自 apps/cs-demo/reply_templates.json，不把 Laya 原始输出透传。

运行请使用 E:\\ComfyUI\\python_embeded\\python.exe；脚本会把
E:\\视频\\modelsmith\\.pylibs-lf 和 Laya vendor 加入 sys.path。

POST /decide {"input":"..."}
GET  /health
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PYLIBS = PROJECT_ROOT / ".pylibs-lf"
if PYLIBS.is_dir() and str(PYLIBS) not in sys.path:
    sys.path.insert(0, str(PYLIBS))

LAYA_VENDOR = Path(r"E:\视频\拉亚决策模型\vendor")
LAYA_WRAPPER = LAYA_VENDOR / "拉亚本地包.py"
LAYA_MODEL = PROJECT_ROOT / "_runs" / "laya-ft-20261023-1555"
HF_MODEL = PROJECT_ROOT / "models" / "qwen3-vl-4b-instruct"
HF_ADAPTER = PROJECT_ROOT / "_runs" / "cs-decision-lf-epoch1"
HF_MERGED = PROJECT_ROOT / "_runs" / "cs-decision-merged"
REPLY_TEMPLATES = Path(__file__).resolve().parent / "reply_templates.json"
THRESHOLDS_FILE = Path(__file__).resolve().parent / "thresholds.json"

HOST = "127.0.0.1"
DEFAULT_PORT = 8767
ACTIONS = ["安抚话术", "补发", "退款", "换货", "运费补偿", "发红包", "转人工", "拒绝并解释"]
EMOTIONS = ["平静", "失望", "焦虑", "不满", "愤怒", "威胁投诉"]
EMOTION_LEVEL = {"平静": 1, "失望": 2, "焦虑": 3, "不满": 4, "愤怒": 5, "威胁投诉": 5}

LAYA_QUESTIONS = {
    "emotion": {
        "type": "choice",
        "instructions": "客户当前的情绪属于哪一档？",
        "criteria": {
            "平静": "语气正常，没有明显负面情绪",
            "失望": "对结果失望，但仍可沟通",
            "焦虑": "着急、担心、催促处理",
            "不满": "明确抱怨、体验差、态度不满",
            "愤怒": "强烈愤怒、辱骂、情绪失控",
            "威胁投诉": "威胁投诉、曝光、找监管或起诉",
        },
    },
    "purpose": {
        "type": "choice",
        "instructions": "客户的目的是什么？（用于挑话术模板）",
        "criteria": {
            "退款": "要退钱、退款进度、重复扣费退款",
            "物流": "包裹、快递、发货、延迟、丢件、签收问题",
            "商品问题": "破损、错发、颜色、尺码、质量、少件",
            "换货": "明确要换一件、换码、换色",
            "运费": "包邮、运费差价、退货运费、超重加收",
            "账单扣费": "账单、扣费、发票、价格",
            "账户登录": "登录、密码、注销、取消订阅、数据导出",
            "功能故障": "功能、入口、链接、协作等用不了",
            "服务投诉": "投诉、要求上级、客服态度、二次投诉、辱骂",
            "越界索赔": "十倍赔偿、精神损失费、无凭证、超出售后",
            "模糊无关": "在吗/？等极模糊，或与客服无关",
            "其他": "无法归入以上各类",
        },
    },
    "action": {
        "type": "choice",
        "instructions": "客服下一步最合适的动作是什么？",
        "criteria": {
            "安抚话术": "一般咨询、轻微不满或需要先稳定情绪",
            "补发": "物流未达、丢件、漏发、长时间不更新",
            "退款": "明确退款申请、重复扣费",
            "换货": "错发、破损、尺码或颜色不符",
            "运费补偿": "运费争议、退货运费或运费差价",
            "发红包": "体验差但无越界，需要致歉补偿",
            "转人工": "威胁投诉、要求人工、二次投诉、情绪失控或越界",
            "拒绝并解释": "越界索赔、超出售后范围",
        },
    },
}

HF_INSTRUCTION = """你是中文电商客服决策助手。输入是一条客户消息；只输出一行严格 JSON（不要解释、不要代码块）：
{"emotion":"情绪","emotion_level":1-5,"intent":"意图短语","action":"动作","reply":"给客户的回复"}
emotion 只能取：平静/失望/焦虑/不满/愤怒/威胁投诉；emotion_level：平静1、失望2、焦虑3、不满4、愤怒5、威胁投诉5。
action 只能取八个之一：安抚话术/补发/退款/换货/运费补偿/发红包/转人工/拒绝并解释。
reply 为简洁、礼貌的中文回复（≤80 字），不要承诺越界赔偿。"""


def load_thresholds() -> dict:
    """每题阈值；null = 不启用。可用 thresholds.json 或 CS_LAYA_THRESHOLDS 覆盖。"""
    data = {}
    try:
        if THRESHOLDS_FILE.is_file():
            data = json.loads(THRESHOLDS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    raw_env = os.environ.get("CS_LAYA_THRESHOLDS", "").strip()
    if raw_env:
        try:
            env_data = json.loads(raw_env)
            if isinstance(env_data, dict):
                data = env_data
        except ValueError:
            pass
    out = {"emotion": None, "purpose": None, "action": None}
    if isinstance(data, dict):
        for key in out:
            value = data.get(key)
            if value is None or value == "":
                out[key] = None
                continue
            try:
                value = float(value)
            except (TypeError, ValueError):
                out[key] = None
                continue
            out[key] = max(0.0, min(1.0, value)) if math.isfinite(value) else None
    return out


def load_laya_questions(model_path: Path) -> dict:
    """优先读 checkpoint 自带的 questions.json；旧 checkpoint 则用内置模板。"""
    try:
        raw = json.loads((model_path / "questions.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    if isinstance(raw, dict) and "情绪" in raw and "动作" in raw:
        purpose = raw.get("意图") or raw.get("目的")
        out = {"emotion": raw["情绪"], "action": raw["动作"]}
        if isinstance(purpose, dict):
            out["purpose"] = purpose
        else:
            out["purpose"] = LAYA_QUESTIONS["purpose"]
        return out
    out = dict(LAYA_QUESTIONS)
    return out


def load_templates() -> dict:
    try:
        data = json.loads(REPLY_TEMPLATES.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    return data if isinstance(data, dict) else {}


def select_template(templates: dict, action: str, intent: str = "") -> tuple[str, bool]:
    node = templates.get(action)
    fallback = str(templates.get("_fallback") or "已记录您的情况，请把订单号和具体问题发我，我帮您处理。")
    if isinstance(node, dict):
        if intent and isinstance(node.get(intent), str) and node[intent].strip():
            return node[intent].strip(), False
        if isinstance(node.get("default"), str) and node["default"].strip():
            return node["default"].strip(), False
        for value in node.values():
            if isinstance(value, str) and value.strip():
                return value.strip(), False
    if isinstance(node, str) and node.strip():
        return node.strip(), False
    return fallback, True


def template_result(backend: str, emotion: str, level: int, emotion_conf: float,
                    action: str, action_conf: float, rule: str,
                    templates: dict, purpose: str = "", purpose_conf: float = 0.0,
                    emotion_probs: dict | None = None, purpose_probs: dict | None = None,
                    action_probs: dict | None = None, thresholds: dict | None = None,
                    low_confidence: dict | None = None, fallback: bool = False,
                    reason: str = "") -> dict:
    reply, template_missing = select_template(templates, action, purpose)
    out = {
        "emotion": emotion,
        "emotion_level": level,
        "emotion_confidence": round(max(0.0, min(1.0, float(emotion_conf))), 4),
        "emotion_probabilities": emotion_probs or {},
        "purpose": purpose,
        "purpose_confidence": round(max(0.0, min(1.0, float(purpose_conf))), 4),
        "purpose_probabilities": purpose_probs or {},
        "action": action,
        "action_confidence": round(max(0.0, min(1.0, float(action_conf))), 4),
        "action_probabilities": action_probs or {},
        "confidence_source": "raw",
        "thresholds": thresholds or {"emotion": None, "purpose": None, "action": None},
        "low_confidence": low_confidence or {"emotion": False, "purpose": False, "action": False},
        "rule": rule,
        "reply": reply,
        "reply_source": "template",
        "template_fallback": bool(template_missing),
        "fallback": bool(fallback),
        "backend": backend,
    }
    if reason:
        out["reason"] = str(reason)
    return out


def model_fallback(reason: str, backend: str = "laya", templates: dict | None = None) -> dict:
    templates = templates if templates is not None else load_templates()
    return template_result(backend, "平静", 1, 0.0, "转人工", 0.0,
                           "model-fallback", templates, fallback=True, reason=reason)


def _max_prob(answer: dict) -> float:
    probs = answer.get("probabilities") if isinstance(answer, dict) else None
    if isinstance(probs, dict) and probs:
        try:
            return float(max(float(v) for v in probs.values()))
        except (TypeError, ValueError):
            pass
    try:
        return float(answer.get("confidence", 0.0))
    except (TypeError, ValueError):
        return 0.0


def _probs(answer: dict) -> dict:
    probs = answer.get("probabilities") if isinstance(answer, dict) else None
    return dict(probs) if isinstance(probs, dict) else {}


class LayaModel:
    """Laya only decides emotion + action; replies come from templates."""

    def __init__(self):
        if not LAYA_VENDOR.is_dir():
            raise FileNotFoundError("Laya vendor not found: " + str(LAYA_VENDOR))
        if not LAYA_WRAPPER.is_file():
            raise FileNotFoundError("Laya wrapper not found: " + str(LAYA_WRAPPER))
        if str(LAYA_VENDOR) not in sys.path:
            sys.path.insert(0, str(LAYA_VENDOR))
        spec = importlib.util.spec_from_file_location("laya_local_pkg", str(LAYA_WRAPPER))
        if spec is None or spec.loader is None:
            raise ImportError("cannot load Laya wrapper")
        self.laya = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.laya)
        from laya import Router  # vendor path is now in sys.path

        self.model_path = Path(os.environ.get("CS_LAYA_MODEL") or LAYA_MODEL)
        if not self.model_path.is_dir():
            raise FileNotFoundError("Laya checkpoint not found: " + str(self.model_path))
        self.templates = load_templates()
        self.thresholds = load_thresholds()
        self.questions = load_laya_questions(self.model_path)
        self.lock = threading.Lock()
        self.device = "cuda"
        self.router = Router(
            models={"typed-decisions": (str(self.model_path), None)},
            device="cuda",
            preload=False,
            max_loaded=1,
        )
        self.router.load("typed-decisions")

    def health(self) -> dict:
        return {"ok": True, "model": str(self.model_path), "adapter": "laya", "device": self.device}

    def decide(self, text: str) -> dict:
        with self.lock:
            # 拉亚本地包.决策 的第四个参数名是中文「模型」，不能写 keyword model=。
            result = self.laya.决策(self.router, {"message": text}, self.questions, "typed-decisions")
        answers = (result or {}).get("answers") or {}
        # questions.json 的题 id 是中文（情绪/目的/动作），answers 也按中文键回；
        # 统一映射成英文键，否则下面读到空 dict 会静默走 rule 回退。
        for _zh, _en in (("情绪", "emotion"), ("目的", "purpose"), ("动作", "action")):
            if not answers.get(_en) and answers.get(_zh):
                answers[_en] = answers[_zh]
        emo_ans = answers.get("emotion") or {}
        purpose_ans = answers.get("purpose") or {}
        act_ans = answers.get("action") or {}
        emotion = str(emo_ans.get("choice") or "")
        purpose = str(purpose_ans.get("choice") or "其他")
        action = str(act_ans.get("choice") or "")
        if emotion not in EMOTION_LEVEL:
            return model_fallback("Laya emotion not in whitelist: " + emotion, "laya", self.templates)
        if action not in ACTIONS:
            return model_fallback("Laya action not in whitelist: " + action, "laya", self.templates)
        emotion_conf = _max_prob(emo_ans)
        purpose_conf = _max_prob(purpose_ans)
        action_conf = _max_prob(act_ans)
        low = {
            "emotion": self.thresholds.get("emotion") is not None and emotion_conf < self.thresholds["emotion"],
            "purpose": self.thresholds.get("purpose") is not None and purpose_conf < self.thresholds["purpose"],
            "action": self.thresholds.get("action") is not None and action_conf < self.thresholds["action"],
        }
        rule = "template:" + action
        if low["action"]:
            action = "转人工"
            rule = "low-confidence"
        return template_result(
            "laya", emotion, EMOTION_LEVEL[emotion], emotion_conf,
            action, action_conf, rule, self.templates,
            purpose=purpose, purpose_conf=purpose_conf,
            emotion_probs=_probs(emo_ans), purpose_probs=_probs(purpose_ans), action_probs=_probs(act_ans),
            thresholds=self.thresholds, low_confidence=low,
        )


def parse_json_object(raw: str) -> dict | None:
    text = (raw or "").strip()
    if not text:
        return None
    text = re.sub(r"<think\b[^>]*>.*?</think\s*>", "", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"</?think\b[^>]*>", "", text, flags=re.IGNORECASE).strip()
    if "```" in text:
        lines = [line for line in text.splitlines() if not line.strip().startswith("```")]
        text = "\n".join(lines).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(text[start:end + 1])
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


class Hf4BModel:
    """Legacy 4B backend retained for comparison; Laya is the default."""

    def __init__(self):
        import torch
        from transformers import AutoProcessor, Qwen3VLForConditionalGeneration

        self.torch = torch
        self.lock = threading.Lock()
        self.templates = load_templates()
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        use_peft = os.environ.get("CS_MODEL_USE_PEFT", "").strip().lower() in ("1", "true", "yes", "on")
        if HF_MERGED.is_dir() and not use_peft:
            self.model_file = HF_MERGED
            self.adapter_label = "merged"
            self.processor = AutoProcessor.from_pretrained(str(HF_MERGED), trust_remote_code=True)
            model = Qwen3VLForConditionalGeneration.from_pretrained(
                str(HF_MERGED),
                torch_dtype=torch.bfloat16 if torch.cuda.is_available() else torch.float32,
                device_map="auto",
                trust_remote_code=True,
            )
        else:
            from peft import PeftModel

            if not HF_MODEL.is_dir():
                raise FileNotFoundError("base model not found: " + str(HF_MODEL))
            if not HF_ADAPTER.is_dir():
                raise FileNotFoundError("adapter not found: " + str(HF_ADAPTER))
            self.model_file = HF_MODEL
            self.adapter_label = str(HF_ADAPTER)
            self.processor = AutoProcessor.from_pretrained(str(HF_MODEL), trust_remote_code=True)
            base = Qwen3VLForConditionalGeneration.from_pretrained(
                str(HF_MODEL),
                torch_dtype=torch.bfloat16 if torch.cuda.is_available() else torch.float32,
                device_map="auto",
                trust_remote_code=True,
            )
            model = PeftModel.from_pretrained(base, str(HF_ADAPTER), is_trainable=False)
        self.model = model
        self.model.eval()

    def health(self) -> dict:
        return {"ok": True, "model": str(self.model_file), "adapter": self.adapter_label, "device": self.device}

    def _confidence(self, output, generated_ids) -> float:
        try:
            scores = getattr(output, "scores", None)
            if not scores:
                return 0.0
            probs = []
            for i, score in enumerate(scores):
                if i >= len(generated_ids):
                    break
                token = int(generated_ids[i])
                prob = self.torch.softmax(score[0].float(), dim=-1)[token].item()
                probs.append(max(prob, 1e-9))
            if not probs:
                return 0.0
            return float(math.exp(sum(math.log(p) for p in probs) / len(probs)))
        except Exception:
            return 0.0

    def decide(self, text: str) -> dict:
        with self.lock:
            prompt = HF_INSTRUCTION + "\n\n" + text
            messages = [{"role": "user", "content": prompt}]
            rendered = self.processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
            inputs = self.processor(text=[rendered], return_tensors="pt")
            inputs = {k: v.to(self.device) for k, v in inputs.items()}
            input_len = int(inputs["input_ids"].shape[1])
            with self.torch.inference_mode():
                output = self.model.generate(
                    **inputs,
                    max_new_tokens=256,
                    do_sample=False,
                    return_dict_in_generate=True,
                    output_scores=True,
                )
            generated = output.sequences[0][input_len:]
            raw = self.processor.tokenizer.decode(generated, skip_special_tokens=True)
            confidence = self._confidence(output, generated)
            parsed = parse_json_object(raw)
            if parsed is None:
                return model_fallback("JSON parse failed: " + raw[:200], "hf4b", self.templates)
            action = str(parsed.get("action") or "")
            emotion = str(parsed.get("emotion") or "")
            if action not in ACTIONS or emotion not in EMOTION_LEVEL:
                return model_fallback("action/emotion not in whitelist", "hf4b", self.templates)
            try:
                level = int(parsed.get("emotion_level", EMOTION_LEVEL[emotion]))
            except (TypeError, ValueError):
                level = EMOTION_LEVEL[emotion]
            level = max(1, min(5, level))
            return template_result(
                "hf4b", emotion, level, confidence, action, confidence,
                str(parsed.get("rule") or "model"), self.templates,
                purpose=str(parsed.get("intent") or ""), purpose_conf=confidence,
                thresholds={"emotion": None, "purpose": None, "action": None},
            )


class FakeModel:
    """No-torch HTTP contract stub, used only for local wiring tests."""

    def __init__(self, mode: str = "valid"):
        self.mode = (mode or "valid").strip().lower()
        self.templates = load_templates()

    def health(self) -> dict:
        return {"ok": True, "model": "fake", "adapter": "fake", "device": "cpu", "fake": True}

    def decide(self, text: str) -> dict:
        if self.mode in ("invalid-json", "bad-json", "bad_json"):
            return model_fallback("fake invalid JSON", "fake", self.templates)
        if self.mode in ("invalid-action", "bad-action", "bad_action"):
            return model_fallback("fake invalid action", "fake", self.templates)
        if "退款" in text:
            action, emotion, level, purpose = "转人工", "愤怒", 5, "退款"
        elif text.strip() in ("在吗", "？", "?") or not text.strip():
            action, emotion, level, purpose = "转人工", "平静", 1, "模糊无关"
        else:
            action, emotion, level, purpose = "安抚话术", "平静", 1, "其他"
        return template_result(
            "laya", emotion, level, 0.9, action, 0.85,
            "template:" + action, self.templates,
            purpose=purpose, purpose_conf=0.8,
            emotion_probs={emotion: 0.9}, purpose_probs={purpose: 0.8}, action_probs={action: 0.85},
            thresholds={"emotion": None, "purpose": None, "action": None},
        )


class Handler(BaseHTTPRequestHandler):
    model = None

    def log_message(self, fmt, *args):
        sys.stderr.write("[cs-model] " + (fmt % args) + "\n")

    def _json(self, status: int, payload: dict):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path.split("?", 1)[0] != "/health":
            self.send_error(404, "not found")
            return
        try:
            self._json(200, self.model.health())
        except Exception as exc:
            self._json(500, {"ok": False, "error": type(exc).__name__})

    def do_POST(self):
        if self.path.split("?", 1)[0] != "/decide":
            self.send_error(404, "not found")
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
            if length <= 0 or length > 1024 * 1024:
                self._json(400, model_fallback("invalid content-length", "laya"))
                return
            payload = json.loads(self.rfile.read(length).decode("utf-8", errors="replace"))
            text = payload.get("input") if isinstance(payload, dict) else ""
            if not isinstance(text, str) or not text.strip():
                self._json(200, model_fallback("empty_input", "laya"))
                return
            started = time.perf_counter()
            result = self.model.decide(text)
            result["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
            self._json(200, result)
        except Exception as exc:
            self._json(200, model_fallback("server exception: " + type(exc).__name__, "laya"))


def main() -> int:
    parser = argparse.ArgumentParser(description="客服决策本地模型服务（Laya 默认）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--fake", action="store_true", help="不加载模型，仅测 HTTP 契约")
    parser.add_argument("--backend", choices=("laya", "hf4b"), default=None)
    args = parser.parse_args()
    if args.port == 8765:
        print("拒绝使用 8765：该端口属于 ModelSmith 核心", file=sys.stderr)
        return 2
    backend = (args.backend or os.environ.get("CS_MODEL_BACKEND") or "laya").strip().lower()
    if backend not in ("laya", "hf4b"):
        print("CS_MODEL_BACKEND 只能是 laya 或 hf4b", file=sys.stderr)
        return 2
    fake_mode = args.fake or os.environ.get("CS_MODEL_FAKE", "").strip().lower() not in ("", "0", "false", "off")
    try:
        if fake_mode:
            mode = os.environ.get("CS_MODEL_FAKE", "valid") if not args.fake else (os.environ.get("CS_MODEL_FAKE") or "valid")
            Handler.model = FakeModel(mode)
            print("cs-model FAKE mode=" + str(mode), flush=True)
        elif backend == "laya":
            print("loading Laya checkpoint: " + str(LAYA_MODEL), flush=True)
            Handler.model = LayaModel()
        else:
            print("loading HF 4B (legacy backend)", flush=True)
            Handler.model = Hf4BModel()
        print("loaded:", json.dumps(Handler.model.health(), ensure_ascii=False), flush=True)
    except Exception as exc:
        print("model load failed: " + type(exc).__name__ + ": " + str(exc), file=sys.stderr, flush=True)
        return 1
    server = ThreadingHTTPServer((HOST, args.port), Handler)
    server.daemon_threads = True
    print("cs-model serving on http://%s:%d/ backend=%s" % (HOST, args.port, backend), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\ncs-model stopped", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
