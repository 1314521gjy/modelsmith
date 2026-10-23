#!/usr/bin/env python3
"""客服决策展示台：stdlib-only HTTP 服务 + 规则/模型双后端。

本文件只服务 apps/cs-demo/ 下的静态页面和 /decide，不碰 ModelSmith core。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
HOST = "127.0.0.1"
DEFAULT_PORT = 8766
HTTP_TIMEOUT_S = 30.0  # 本地 VL 模型首轮生成可能超过 3s；规则后端不受影响

ACTIONS = [
    "安抚话术",
    "补发",
    "退款",
    "换货",
    "运费补偿",
    "发红包",
    "转人工",
    "拒绝并解释",
]
EMOTIONS = ["平静", "失望", "不满", "愤怒", "焦虑", "威胁投诉"]
EMOTION_LEVEL = {
    "平静": 1,
    "失望": 2,
    "焦虑": 3,
    "不满": 4,
    "愤怒": 5,
    "威胁投诉": 5,
}

THREAT_WORDS = ("投诉", "二次投诉", "多次投诉", "又投诉", "起诉", "曝光", "监管", "315", "12315", "平台举报", "工商", "媒体", "上级", "经理", "负责人", "告你们", "差评", "维权")
ANGER_WORDS = ("破东西", "破玩意", "垃圾", "废物", "什么玩意", "气死", "滚", "恶心", "离谱", "坑人", "脑残", "妈的", "特么", "傻", "烂", "情绪失控")
ANXIETY_WORDS = ("急", "怎么办", "多久", "担心", "焦虑", "催", "尽快", "害怕", "不安", "赶紧", "等着用")
DISAPPOINTMENT_WORDS = ("失望", "没想到", "本来", "期望", "可惜", "寒心")
DISSATISFACTION_WORDS = ("不满意", "太差", "态度", "敷衍", "问题", "等了", "没收到", "坏了", "错发", "漏发", "质量", "差劲", "体验差")

REFUSE_WORDS = ("十倍", "精神损失", "无凭证", "索赔", "超过", "超时", "超出", "人为", "损坏", "不符合", "不能退", "不支持", "无理由", "用过了", "已拆封")
REFUND_WORDS = ("退款", "退钱", "把钱退", "返还", "退回来", "重复扣费", "重复扣款", "多扣")
RESHIP_WORDS = ("补发", "再发一份", "漏发", "少发", "没收到", "缺件", "少件", "物流不更新", "长时间不更新", "丢件", "未达")
EXCHANGE_WORDS = ("换货", "换一个", "换个", "换新", "换型号", "换颜色", "错发", "破损", "尺码", "颜色不符")
FREIGHT_WORDS = ("运费", "邮费", "快递费", "退货运费", "运费差价")
REDPACKET_WORDS = ("红包", "赔偿", "补偿", "优惠券", "体验差", "态度差", "服务差", "老客", "回头客")
HUMAN_WORDS = ("人工", "转人工", "经理", "负责人", "客服", "投诉", "上级", "监管")
CONSULT_WORDS = ("你好", "咨询", "怎么", "如何", "多少", "什么时候", "请问", "为什么", "使用")
VAGUE_WORDS = ("在吗", "？", "?", "点歌", "闹钟", "天气", "讲笑话", "随便聊聊")


def _hits(text: str, words: tuple[str, ...]) -> list[str]:
    return [w for w in words if w in text]


def detect_emotion(text: str) -> tuple[str, int, list[str]]:
    for name, words in (
        ("威胁投诉", THREAT_WORDS),
        ("愤怒", ANGER_WORDS),
        ("焦虑", ANXIETY_WORDS),
        ("不满", DISSATISFACTION_WORDS),
        ("失望", DISAPPOINTMENT_WORDS),
    ):
        hits = _hits(text, words)
        if hits:
            return name, EMOTION_LEVEL[name], hits
    return "平静", 1, []


def detect_intent(text: str) -> str:
    if _hits(text, REFUSE_WORDS):
        return "越界索赔"
    if _hits(text, REFUND_WORDS):
        return "退款"
    if _hits(text, RESHIP_WORDS):
        return "补发"
    if _hits(text, EXCHANGE_WORDS):
        return "换货"
    if _hits(text, FREIGHT_WORDS):
        return "运费补偿"
    if _hits(text, REDPACKET_WORDS):
        return "发红包"
    if _hits(text, HUMAN_WORDS):
        return "转人工"
    if _hits(text, CONSULT_WORDS):
        return "咨询"
    return "其他"


def choose_action(emotion: str, intent: str, text: str = "") -> tuple[str, str]:
    # 策略表按优先级：威胁/失控/人工 > 越界索赔 > 物流 > 换货 > 运费 > 退款 > 补偿 > 咨询 > 模糊。
    if emotion == "威胁投诉":
        return "转人工", f"{emotion} + {intent} → 转人工"
    if emotion == "愤怒":
        if intent == "退款":
            return "转人工", "愤怒 + 退款 + 无二次投诉 → 转人工"
        return "转人工", f"{emotion} + 情绪失控 → 转人工"
    if intent == "转人工":
        return "转人工", f"{emotion} + 要求人工 → 转人工"
    if intent == "越界索赔" or _hits(text, REFUSE_WORDS):
        return "拒绝并解释", "越界索赔/超出售后范围 → 拒绝并解释"
    if intent == "补发":
        return "补发", f"{emotion} + 物流未达 → 补发"
    if intent == "换货":
        return "换货", f"{emotion} + 错发/破损/不符 → 换货"
    if intent == "运费补偿":
        return "运费补偿", f"{emotion} + 运费争议 → 运费补偿"
    if intent == "退款":
        return "退款", f"{emotion} + 明确退款 → 退款"
    if intent == "发红包":
        return "发红包", f"{emotion} + 体验差但无越界 → 发红包"
    if intent == "咨询":
        return "安抚话术", f"{emotion} + 一般咨询 → 安抚话术"
    if emotion in ("失望", "不满", "焦虑"):
        return "安抚话术", f"{emotion} + 轻微不满 → 安抚话术"
    return "转人工", "极模糊/与客服无关 → 转人工"


def confidence_for(emotion: str, intent: str, hits: list[str], action: str) -> float:
    score = 0.52 + min(0.22, 0.06 * len(hits))
    if intent != "咨询":
        score += 0.14
    if emotion in ("愤怒", "威胁投诉"):
        score += 0.04
    if action == "转人工":
        score += 0.04
    return round(max(0.5, min(0.98, score)), 2)


REPLIES = {
    "安抚话术": "我在，先别着急。您把订单号和具体问题发我，我帮您一步步处理。",
    "补发": "抱歉给您添麻烦了。我帮您登记补发，核对收货信息后尽快安排。",
    "退款": "理解您想退款。请提供订单号，我核对后按原支付路径处理；具体到账时间以支付渠道为准。",
    "换货": "可以帮您走换货流程，请提供订单号和需要更换的型号或颜色。",
    "运费补偿": "运费问题我帮您登记补偿，请提供订单号和运费凭证。",
    "发红包": "这次体验确实不好，我帮您申请一个补偿红包；具体金额以审核结果为准。",
    "转人工": "我先把您的情况升级给人工客服，预计 1 分钟内接入。请不要重复提交，之前的记录我会带过去。",
    "拒绝并解释": "理解您的诉求。不过这笔订单已超过售后期或不符合退款条件，我先向您说明原因，再看是否有其他补救方式。",
}


def rule_decide(text: str) -> dict:
    text = (text or "").strip()
    if not text:
        return fallback("empty_input")
    emotion, level, hits = detect_emotion(text)
    intent = detect_intent(text)
    action, rule = choose_action(emotion, intent, text)
    return {
        "emotion": emotion,
        "emotion_level": level,
        "intent": intent,
        "action": action,
        "confidence": confidence_for(emotion, intent, hits, action),
        "rule": rule,
        "reply": REPLIES[action],
        "fallback": False,
        "backend": "rule",
    }


def fallback(reason: str) -> dict:
    return {
        "emotion": "平静",
        "emotion_level": 1,
        "intent": "未知",
        "action": "转人工",
        "confidence": 0.5,
        "action_confidence": 0.0,
        "emotion_confidence": 0.0,
        "rule": "fallback",
        "reply": "模型输出无法解析或动作不在白名单，已自动转人工处理。",
        "reply_source": "builtin",
        "template_fallback": False,
        "fallback": True,
        "reason": str(reason or "fallback"),
        "backend": "fallback",
    }


def normalize_result(data: object) -> dict | None:
    if not isinstance(data, dict):
        return None
    for wrapper in ("result", "decision", "data"):
        if isinstance(data.get(wrapper), dict):
            data = data[wrapper]
            break
    if not isinstance(data, dict):
        return None
    action = str(data.get("action", "")).strip()
    if action not in ACTIONS:
        return None
    emotion = str(data.get("emotion", "")).strip()
    if emotion not in EMOTIONS:
        return None
    required = ("rule", "reply")
    if any(k not in data for k in required):
        return None
    try:
        action_conf = float(data.get("action_confidence", data.get("confidence", 0.0)))
        emotion_conf = float(data.get("emotion_confidence", action_conf))
        purpose_conf = float(data.get("purpose_confidence", 0.0))
        level = int(data.get("emotion_level", EMOTION_LEVEL[emotion]))
    except (TypeError, ValueError):
        return None
    action_conf = max(0.0, min(1.0, action_conf))
    emotion_conf = max(0.0, min(1.0, emotion_conf))
    purpose_conf = max(0.0, min(1.0, purpose_conf))
    level = max(1, min(5, level))
    rule = str(data.get("rule") or "模型命中")
    return {
        "emotion": emotion,
        "emotion_level": level,
        "emotion_confidence": round(emotion_conf, 4),
        "emotion_probabilities": data.get("emotion_probabilities") if isinstance(data.get("emotion_probabilities"), dict) else {},
        "purpose": str(data.get("purpose") or data.get("intent") or ""),
        "purpose_confidence": round(purpose_conf, 4),
        "purpose_probabilities": data.get("purpose_probabilities") if isinstance(data.get("purpose_probabilities"), dict) else {},
        "intent": str(data.get("intent") or data.get("purpose") or ""),
        "action": action,
        "action_confidence": round(action_conf, 4),
        "action_probabilities": data.get("action_probabilities") if isinstance(data.get("action_probabilities"), dict) else {},
        "confidence": round(action_conf, 4),
        "confidence_source": str(data.get("confidence_source") or "raw"),
        "thresholds": data.get("thresholds") if isinstance(data.get("thresholds"), dict) else {"emotion": None, "purpose": None, "action": None},
        "low_confidence": data.get("low_confidence") if isinstance(data.get("low_confidence"), dict) else {"emotion": False, "purpose": False, "action": False},
        "rule": rule,
        "reply": str(data.get("reply") or ""),
        "reply_source": str(data.get("reply_source") or "model"),
        "template_fallback": bool(data.get("template_fallback")),
        "fallback": bool(data.get("fallback")) or rule in ("fallback", "model-fallback") or rule.endswith("-fallback"),
        "backend": "http",
    }


def http_decide(text: str, backend: str) -> tuple[dict | None, str]:
    endpoint = backend.rstrip("/")
    if not endpoint.endswith("/decide"):
        endpoint += "/decide"
    try:
        body = json.dumps({"input": text}, ensure_ascii=False).encode("utf-8")
        req = Request(
            endpoint,
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        result = normalize_result(json.loads(raw))
        if result is None:
            return None, "backend result invalid or action not in whitelist"
        return result, ""
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
        return None, f"{type(exc).__name__}: {exc}"


def decide(text: str) -> dict:
    backend = (os.environ.get("CS_BACKEND") or "rule").strip()
    if not backend or backend.lower() == "rule":
        return rule_decide(text)
    if backend.startswith(("http://", "https://")):
        result, error = http_decide(text, backend)
        if result is not None:
            return result
        return fallback(error or "model backend failed")
    return fallback("CS_BACKEND must be rule or http(s)://host:port")


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt, *args):
        sys.stderr.write("[cs-demo] " + (fmt % args) + "\n")

    def _json(self, status: int, payload: dict):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        if urlparse(self.path).path != "/decide":
            self.send_error(404, "not found")
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
            if length <= 0 or length > 1024 * 1024:
                self._json(400, fallback("invalid content-length"))
                return
            payload = json.loads(self.rfile.read(length).decode("utf-8", errors="replace"))
            text = payload.get("input") if isinstance(payload, dict) else ""
            if not isinstance(text, str) or not text.strip():
                self._json(200, fallback("empty_input"))
                return
            self._json(200, decide(text))
        except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
            self._json(400, fallback(f"invalid json: {exc}"))
        except Exception as exc:  # keep the demo process alive
            self._json(500, fallback(f"server error: {type(exc).__name__}"))


def main() -> int:
    parser = argparse.ArgumentParser(description="客服决策展示台（stdlib-only）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    if args.port == 8765:
        print("拒绝使用 8765：该端口属于 ModelSmith 核心", file=sys.stderr)
        return 2
    server = ThreadingHTTPServer((HOST, args.port), Handler)
    server.daemon_threads = True
    print(f"cs-demo serving on http://{HOST}:{args.port}/", flush=True)
    print(f"backend={os.environ.get('CS_BACKEND', 'rule')} (default rule)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\ncs-demo stopped", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
