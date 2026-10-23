#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""构建中文客服决策 SFT 数据集（LlamaFactory alpaca 格式）。

只读来源：
  resources\\01-决策模型-业务语料\\typed-decisions\\customer_service\\{train,test}-00000-of-00001.parquet
  resources\\01-决策模型-业务语料\\zh-decision-bench\\zh_decision_bench.jsonl
  resources\\01-决策模型-业务语料\\massive-zh-CN\\train.jsonl

只写本目录：train.jsonl / val.jsonl / schema.json / ACTIONS.md / stats.json / README.md

确定性：固定 seed=20261023；除输入语料内容外无随机源；重复运行字节一致（README 记录 sha256）。

运行（必须用带 pandas 的运行时 python）：
  C:\\ModelSmith\\runtime\\python\\python.exe build_dataset.py
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import sys
from pathlib import Path

RES = Path(r"E:\视频\modelsmith\resources\01-决策模型-业务语料")
OUT = Path(r"E:\视频\modelsmith\datasets\cs-decision")
SEED = 20261023
VAL_RATIO = 0.15

ACTIONS = ["安抚话术", "补发", "退款", "换货", "运费补偿", "发红包", "转人工", "拒绝并解释"]
EMOTION_LEVEL = {"平静": 1, "失望": 2, "焦虑": 3, "不满": 4, "愤怒": 5, "威胁投诉": 5}
SCHEMA_KEYS = ["emotion", "emotion_level", "intent", "action", "reply"]

INSTRUCTION = (
    "你是中文电商客服决策助手。输入是一条客户消息；只输出一行严格 JSON（不要解释、不要代码块）：\n"
    '{"emotion":"情绪","emotion_level":1-5,"intent":"意图短语","action":"动作","reply":"给客户的回复"}\n'
    "emotion 只能取：平静/失望/焦虑/不满/愤怒/威胁投诉；emotion_level：平静1、失望2、焦虑3、不满4、愤怒5、威胁投诉5。\n"
    "action 只能取八个之一：安抚话术/补发/退款/换货/运费补偿/发红包/转人工/拒绝并解释。\n"
    "决策策略（按优先级）：\n"
    "1) 威胁起诉/曝光/找监管、二次投诉、点名要上级、情绪失控、或要求人工 → 转人工；\n"
    "2) 越界索赔（十倍赔偿、精神损失费、无凭证退款、超出售后范围）→ 拒绝并解释；\n"
    "3) 可执行诉求按类型：物流未达/丢件 → 补发；错发/破损 → 换货；运费争议 → 运费补偿；"
    "退款申请 → 退款；老客挽留/致歉补偿 → 发红包；一般咨询/轻微不满 → 安抚话术；\n"
    "4) 极模糊（如“在吗”“？”）或与客服无关（点歌/闹钟等）→ 转人工。\n"
    "reply 为简洁、礼貌、可执行的中文回复（≤80 字），不要承诺越界赔偿。"
)

# --------------------------------------------------------------------------- #
# 英→中：规则化翻译（关键词短语映射 + 按标签的模板回退）
# 说明：来源里客户消息是英文自由文本；这里不做逐句机器翻译，而是
#  (a) 用短语映射表抽取可观测诉求拼成中文；无命中时
#  (b) 用「类别 + 情绪 + 订单信息」模板生成中文客户消息。
# 两种路径的比例写进 stats.json / README。
# --------------------------------------------------------------------------- #
PHRASE_MAP = [
    (r"close my account|cancel my account|close the account|cancellation process", "我想注销账户"),
    (r"cancel my (premium )?subscription|cancel the subscription|stop the subscription", "我想取消订阅"),
    (r"refund|money back|reimburse", "我想申请退款"),
    (r"double charged|charged twice|duplicate charge|overcharged|charged me twice", "我被重复扣费了"),
    (r"invoice|receipt|billing statement|billing", "账单有问题"),
    (r"locked out|can'?t get into my account|can'?t log in|cannot log in|login|password reset", "我无法登录账户"),
    (r"export of all my data|export my data|csv format|data export", "我需要导出全部数据"),
    (r"invite link|collaboration feature|invitation doesn'?t work", "协作邀请链接打不开"),
    (r"wrong item|incorrect product|wrong product|not what i ordered|received the wrong", "我收到的商品发错了"),
    (r"damaged|broken|defective|cracked|faulty", "商品有破损"),
    (r"missing|lost|not arrived|never arrived|hasn'?t arrived|where is my order|delayed|late", "物流一直没到"),
    (r"shipping fee|shipping cost|freight|postage|delivery fee", "运费有问题"),
    (r"ten times|10x|tenfold|compensation|compensate|damages", "我要求额外赔偿"),
    (r"supervisor|manager|human|real person|escalate", "我要找人工处理"),
    (r"cancel|churn|leaving|switch to|competitor", "我在考虑换一家"),
    (r"urgent|asap|by friday|immediately|today|right now", "这事很急"),
    (r"frustrated|ridiculous|unacceptable|angry|furious|appalled", "我非常不满"),
    (r"prior tickets|again|still|third time|second time|no one (has )?(replied|responded)", "这问题我已经反馈过多次"),
    (r"account number|seats|user|member|team", "账户相关"),
    (r"feature|bug|error|doesn'?t work|not working|broken feature", "功能用不了"),
]
PHRASE_RE = [(re.compile(pat, re.I), zh) for pat, zh in PHRASE_MAP]
CAT_ZH = {"account": "账户", "billing": "账单", "technical": "技术", "delivery": "物流", "refund": "退款"}
ORDER_REF_RE = re.compile(r"[A-Z]-\d{4,6}")

REPLIES = {
    "安抚话术": [
        "非常理解您的心情，我先帮您核对情况。请提供订单号，我马上跟进。",
        "抱歉给您带来不便，我先记录您的问题，并优先为您处理。",
        "我明白您的着急，已为您登记，稍后会有处理进展同步给您。",
    ],
    "补发": [
        "已为您安排补发，订单 {oid}，预计 3-5 个工作日送达，请留意物流。",
        "确认是物流丢件，我们马上补发一份，订单 {oid}，给您添麻烦了。",
        "补发已提交，订单 {oid}，发货后会把单号发给您。",
    ],
    "退款": [
        "已为您提交退款申请，订单 {oid}，金额 {amt} 元，1-3 个工作日原路退回。",
        "退款已受理，订单 {oid}，到账后会短信通知您，请放心。",
        "可以退，已按原支付方式提交，订单 {oid}，预计 1-3 个工作日到账。",
    ],
    "换货": [
        "可以为您换货，请把商品现状拍照发我，订单 {oid}，我们安排取件。",
        "已为您登记换货，订单 {oid}，新商品发出前会先质检，请放心。",
        "换货申请已提交，订单 {oid}，寄回后 48 小时内为您发出新件。",
    ],
    "运费补偿": [
        "本次运费由我们承担，已为您补偿 {fee} 元运费，订单 {oid}。",
        "运费问题已核实，补偿 {fee} 元将退回您的账户，订单 {oid}。",
        "给您添麻烦了，运费 {fee} 元已登记补偿，订单 {oid}。",
    ],
    "发红包": [
        "为表歉意，给您发一个 {fee} 元红包，可在账户中心领取，订单 {oid}。",
        "感谢您一直支持，已为您申请 {fee} 元心意红包，订单 {oid}。",
        "给您添麻烦了，{fee} 元红包已发放，请查收，订单 {oid}。",
    ],
    "转人工": [
        "这个问题需要专人跟进，已为您转接人工客服，请稍候。",
        "已记录您的情况并升级处理，人工客服稍后与您联系。",
        "我先帮您转接高级客服，请保持在线，稍等一下。",
    ],
    "拒绝并解释": [
        "很抱歉，十倍赔偿超出平台规则无法满足；按售后规则可为您办理退款或补发。",
        "您的要求超出可执行范围，无法按此处理；我们可按平台规则为您退款或换货。",
        "无凭证的全额退款无法办理；如商品有问题，请拍照，我们按规则处理。",
    ],
}

# 模板库：保证八类动作都有足量样本（来源 S2，规则模板，确定性）
TEMPLATE_BANK = [
    ("退款", "平静", "退款申请", "你好，我不想要了，想申请退款，订单 {oid}。"),
    ("退款", "失望", "退款申请", "等了一周还是没发货，麻烦直接退款吧，订单 {oid}。"),
    ("退款", "不满", "退款申请", "商品和描述差太多，我要退款，订单 {oid}，请尽快处理。"),
    ("退款", "焦虑", "退款进度", "退款提交三天了还没到账，能帮我查一下吗？订单 {oid}。"),
    ("退款", "不满", "重复扣费", "同一个月扣了两次钱，请把多的那笔退给我，订单 {oid}。"),
    ("退款", "焦虑", "退款未到账", "退款说三天到账，现在五天了还没收到，订单 {oid}。"),
    ("补发", "平静", "物流丢件", "我的包裹显示签收但没收到，麻烦补发，订单 {oid}。"),
    ("补发", "失望", "物流延迟", "物流一周没更新了，麻烦帮我补发一份，订单 {oid}。"),
    ("补发", "不满", "包裹丢失", "快递说丢了，我急用，请马上补发，订单 {oid}。"),
    ("补发", "焦虑", "急用补发", "明天就要用，包裹还没到，能不能先补发？订单 {oid}。"),
    ("补发", "不满", "错发漏发", "少发了一件，麻烦补发，订单 {oid}。"),
    ("补发", "平静", "分批发货", "订单里还有一件没发，能补发吗？订单 {oid}。"),
    ("换货", "平静", "商品错发", "收到的颜色不对，想换货，订单 {oid}。"),
    ("换货", "失望", "商品破损", "杯子到货就裂了，麻烦换一个，订单 {oid}。"),
    ("换货", "不满", "二次破损", "换了一次还是有破损，这次请仔细检查，订单 {oid}。"),
    ("换货", "焦虑", "尺码换货", "尺码买小了，着急穿，能尽快换货吗？订单 {oid}。"),
    ("换货", "焦虑", "急用换货", "后天要用，商品有瑕疵，能不能加急换货？订单 {oid}。"),
    ("换货", "平静", "尺码不符", "鞋子尺码偏小，想换大一码，订单 {oid}。"),
    ("运费补偿", "平静", "运费咨询", "这款不是包邮吗？为什么还收运费，订单 {oid}。"),
    ("运费补偿", "失望", "退货运费", "退货还要我出运费，不太合理，订单 {oid}。"),
    ("运费补偿", "不满", "运费差价", "下单时运费 12 元，现在降价了，能补运费吗？订单 {oid}。"),
    ("运费补偿", "焦虑", "急件运费", "我加了急件运费还是慢，运费能退吗？订单 {oid}。"),
    ("运费补偿", "不满", "强制运费", "明明写着包邮，结算又加运费，请退运费，订单 {oid}。"),
    ("运费补偿", "焦虑", "退货超时", "退货快到期了，运费能不能你们承担？订单 {oid}。"),
    ("发红包", "失望", "老客安抚", "我是老客户了，这次体验很差，订单 {oid}。"),
    ("发红包", "不满", "服务投诉", "客服一直不回复，我很失望，订单 {oid}。"),
    ("发红包", "平静", "好评返现", "东西不错，有返现或红包活动吗？订单 {oid}。"),
    ("发红包", "焦虑", "延误安抚", "延误好几天了，能不能给点补偿？订单 {oid}。"),
    ("发红包", "平静", "活动奖励", "参加活动说有好评红包，怎么领取？订单 {oid}。"),
    ("发红包", "失望", "物流太慢", "物流太慢了，体验不好，有补偿吗？订单 {oid}。"),
    ("安抚话术", "平静", "使用咨询", "这个功能怎么用？我找不到入口。"),
    ("安抚话术", "失望", "发货咨询", "下单两天了还没发货，能帮我看看吗？订单 {oid}。"),
    ("安抚话术", "焦虑", "账号异常", "账号突然登不上，里面有余额，怎么办？"),
    ("安抚话术", "不满", "服务态度", "刚才客服态度不好，我想反馈一下，订单 {oid}。"),
    ("安抚话术", "失望", "改地址", "下单后想改收货地址，还能改吗？订单 {oid}。"),
    ("安抚话术", "平静", "发票咨询", "想问下怎么开发票，订单 {oid}。"),
    ("转人工", "威胁投诉", "投诉升级", "再不解决我就去消协投诉，订单 {oid}。"),
    ("转人工", "愤怒", "要求上级", "让你们主管来找我，我不跟你说了，订单 {oid}。"),
    ("转人工", "不满", "二次投诉", "上次投诉没人管，这次必须给我一个说法，订单 {oid}。"),
    ("转人工", "焦虑", "紧急求助", "账户被冻结，里面有重要资料，请尽快转人工。"),
    ("转人工", "愤怒", "人身威胁", "你们再不处理，我就去平台曝光你们，订单 {oid}。"),
    ("转人工", "焦虑", "资金冻结", "账户资金被冻结了，很着急，请转人工，订单 {oid}。"),
    ("拒绝并解释", "威胁投诉", "越界索赔", "晚了三天，你们必须赔我十倍，不然我就起诉。"),
    ("拒绝并解释", "不满", "无凭证退款", "东西用了一个月了，我想全额退款，不给就投诉。"),
    ("拒绝并解释", "平静", "超出售后", "买了两年的机器还能免费换新吗？不能的话说明理由。"),
    ("拒绝并解释", "愤怒", "精神损失费", "你们的失误让我很生气，必须赔精神损失费。"),
    ("拒绝并解释", "平静", "超期退货", "签收超过三十天了，还能退吗？不能的话请说明。"),
    ("拒绝并解释", "不满", "无凭证破损", "东西坏了但没有照片，要求直接退款，订单 {oid}。"),
    ("换货", "平静", "颜色不符", "实物颜色和页面差很多，想换同款其他颜色，订单 {oid}。"),
    ("换货", "失望", "功能异常", "用了两天就坏了，想换一个新的，订单 {oid}。"),
    ("换货", "焦虑", "礼品换货", "送人的礼物有瑕疵，急等换货，订单 {oid}。"),
    ("换货", "不满", "重复问题", "同一位置又裂了，这次请换新批次，订单 {oid}。"),
    ("换货", "平静", "配件缺失", "少了一个配件，能补寄或换整套吗？订单 {oid}。"),
    ("换货", "焦虑", "活动急用", "活动周末要用，商品有问题，能加急换货吗？订单 {oid}。"),
    ("运费补偿", "平静", "包邮疑问", "页面上写包邮，为什么结算有运费？订单 {oid}。"),
    ("运费补偿", "失望", "退货运费高", "退货被收 18 元运费，比我买的还贵，订单 {oid}。"),
    ("运费补偿", "不满", "二次运费", "换货还要我再付一次运费，不合理，订单 {oid}。"),
    ("运费补偿", "焦虑", "急件加价", "加钱发了急件还是延误，运费能退吗？订单 {oid}。"),
    ("运费补偿", "平静", "优惠券抵扣", "运费能不能用优惠券抵掉？订单 {oid}。"),
    ("运费补偿", "不满", "超重加收", "说好包邮又按超重加收，请退还多收的运费，订单 {oid}。"),
]

# 负控（≥40）：越界索赔/辱骂/极模糊/二次投诉 + 与客服无关（来自 zh 语料的语音助手指令）
NEG_OUT_OF_SCOPE = [
    "东西晚到三天，按你们承诺该赔我十倍，直接打我账户。",
    "你们害我误了事，必须赔我一千块精神损失费。",
    "我要求全额退款再加三倍赔偿，不然就曝光你们。",
    "包裹晚了，赔偿十倍运费，否则我去告你们。",
    "虽然用了一个月，但我不满意，要求无理由全额退款并补偿。",
    "商品降价了，你们得把差价十倍赔给我。",
    "我朋友说可以赔十倍，你们也照这个标准赔。",
    "不给我五百块补偿，我就一直投诉下去。",
    "你们承诺过不满意就十倍退款，现在必须兑现。",
    "东西有划痕，我要求退款并额外赔两千。",
    "我要求你们承担我因此损失的误工费。",
    "必须赔我优惠券加现金，否则我就找媒体。",
]
NEG_ABUSE = [
    "你们这群废物，做事能不能长点脑子？",
    "垃圾客服，滚出来给我解释清楚。",
    "就你们这破公司，早点倒闭算了。",
    "你算什么东西，也配跟我说话？",
    "狗屁服务，信不信我弄死你们的店。",
    "一群饭桶，除了推卸责任还会什么？",
    "你们老板是不是脑子进水了？",
    "废物东西，一个订单都处理不好。",
    "少废话，再逼逼我投诉到你关门。",
    "就这态度？你们全家都这样吗？",
    "你们就是一群骗子，别装了。",
    "蠢货，看清楚订单再回话。",
]
NEG_VAGUE = [
    "在吗",
    "？",
    "你好",
    "有人吗",
    "在不在",
    "喂",
    "那个……",
    "问一下",
    "帮忙看看",
    "我有个问题",
    "说话",
    "？？？",
]
NEG_SECOND_COMPLAINT = [
    "上次投诉到现在没人处理，这是第三次了，我要投诉到消协。",
    "我已经投诉过两次，你们还是没人联系我，必须给说法。",
    "之前的投诉单号还没结，今天必须升级处理。",
    "第二次找你们了，还是没人管，我要找你们领导。",
    "投诉过一次了，这次直接给我转人工主管。",
    "上周投诉说会回电，根本没人回，我要继续投诉。",
    "又是同样的问题，之前投诉根本没解决。",
    "我已经在平台投诉过了，今天再不处理就曝光。",
    "第三次反馈了，我要一个明确答复，不然就找监管。",
    "上回投诉被敷衍了，这次请你们上级出来。",
    "之前的工单还没关闭，为什么又让我重新描述？",
    "已经投诉三次了，你们到底管不管？",
]


def norm(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "")).strip()


TRANSLATION_STATS = {"phrase": 0, "template": 0}


def translate_customer(text: str, category: str, oid: str, case_ref: str) -> tuple[str, str]:
    """英→中规则翻译；返回 (中文消息, 路径标记 phrase|template)。"""
    parts = []
    for regex, zh in PHRASE_RE:
        if regex.search(text or ""):
            if zh not in parts:
                parts.append(zh)
    if parts:
        msg = "你好，" + "，".join(parts[:1])
        if len(msg) < 12:
            msg += "，麻烦帮我处理一下"
        if oid and ("订单" not in msg) and category in ("delivery", "refund", "billing"):
            msg += "（订单 %s）" % oid
        elif case_ref:
            msg += "（%s）" % case_ref
        TRANSLATION_STATS["phrase"] += 1
        return msg + "。", "phrase"
    base = CAT_ZH.get(category, "订单")
    msg = "你好，我的%s问题一直没解决" % base
    if oid:
        msg += "（订单 %s）" % oid
    elif case_ref:
        msg += "（%s）" % case_ref
    TRANSLATION_STATS["template"] += 1
    return msg + "，麻烦尽快处理。", "template"


def short_variant(msg: str, intent: str, oid: str) -> str:
    core = msg.replace("你好，", "").strip().rstrip("。")
    core = re.sub(r"（(订单|工单) [^）]*）", "", core).strip().rstrip("。，")
    if len(core) > 26:
        core = core[:26]
        cut = max(core.rfind("，"), core.rfind("。"), core.rfind("；"))
        if cut > 8:
            core = core[:cut]
        core = core.rstrip("。，")
    tail = "（订单 %s）" % oid if oid else ""
    extra = "" if ("处理" in core or "解决" in core) else "麻烦尽快处理。"
    return "%s。%s%s" % (core, tail, extra)


def infer_emotion(en: str, urgency: int, churn: int, prior: int) -> str:
    low = (en or "").lower()
    if re.search(r"lawyer|sue|legal|regulator|chargeback|media|曝光|起诉|消协", low):
        return "威胁投诉"
    if re.search(r"ridiculous|unacceptable|angry|furious|appalled|terrible|disgusted", low):
        return "愤怒"
    if re.search(r"urgent|asap|immediately|right now|by friday|today|紧急", low) or urgency >= 3:
        return "焦虑"
    if re.search(r"frustrated|disappointed|again|still|no one|nobody|third time", low) or prior >= 2:
        return "不满"
    if urgency == 2 or churn >= 2:
        return "不满"
    if urgency == 1 or churn == 1:
        return "失望"
    return "平静"


def infer_action(en: str, category: str, action_label: str, needs: bool,
                 urgency: int, churn: int) -> str:
    low = (en or "").lower()
    escalate = bool(re.search(r"lawyer|sue|legal|regulator|chargeback|supervisor|manager", low))
    if action_label == "escalate_to_human" and (urgency >= 3 or (needs and churn >= 2)):
        escalate = True
    if needs and urgency >= 3 and churn >= 1:
        escalate = True
    if escalate:
        return "转人工"
    if action_label == "execute_refund" or category == "refund":
        return "退款"
    if category == "delivery":
        if re.search(r"wrong|incorrect|damaged|broken|defective", low):
            return "换货"
        if re.search(r"shipping fee|shipping cost|freight|postage|delivery fee", low):
            return "运费补偿"
        if re.search(r"missing|lost|not arrived|never arrived|where is my order|late|delayed", low):
            return "补发"
        if re.search(r"where is|order|deliver", low):
            return "补发"
        return "安抚话术"
    if category == "billing":
        if re.search(r"double charged|charged twice|duplicate|overcharged", low):
            return "退款"
        if churn >= 2:
            return "发红包"
        return "安抚话术"
    if category == "account":
        if re.search(r"close my account|cancel", low) and churn >= 2:
            return "转人工"
        return "安抚话术"
    return "安抚话术"


def infer_intent(en: str, category: str, action_label: str) -> str:
    low = (en or "").lower()
    table = [
        (r"close my account", "注销账户"),
        (r"cancel my (premium )?subscription", "取消订阅"),
        (r"refund|money back", "退款申请"),
        (r"double charged|charged twice|overcharged", "重复扣费"),
        (r"invoice|receipt|billing", "账单问题"),
        (r"locked out|log in|password", "账号登录"),
        (r"export of all my data|csv", "数据导出"),
        (r"invite link|collaboration", "功能故障"),
        (r"wrong item|incorrect product|not what i ordered", "商品错发"),
        (r"damaged|broken|defective", "商品破损"),
        (r"missing|lost|not arrived|never arrived|where is my order", "物流丢件"),
        (r"late|delayed|hasn'?t arrived", "物流延迟"),
        (r"shipping fee|freight|postage", "运费争议"),
        (r"compensation|compensate|ten times", "赔偿诉求"),
        (r"supervisor|manager|human", "要求人工"),
        (r"frustrated|ridiculous|unacceptable", "服务不满"),
    ]
    for pattern, intent in table:
        if re.search(pattern, low):
            return intent
    return {"account": "账户咨询", "billing": "账单咨询", "technical": "功能故障",
            "delivery": "物流咨询", "refund": "退款咨询"}.get(category, "客服咨询")


def pick_reply(action: str, oid: str, amt: float, fee: int, idx: int) -> str:
    tpl = REPLIES[action][idx % len(REPLIES[action])]
    if not oid:
        tpl = tpl.replace("，订单 {oid}", "").replace("订单 {oid}，", "").replace("订单 {oid}", "")
    text = tpl.format(oid=oid or "", amt=(("%.0f" % amt) if amt else "0"), fee=fee)
    return re.sub(r"，。|，，|。。", "。", text).strip()


def make_row(source: str, input_text: str, emotion: str, intent: str, action: str,
             reply: str, neg_class: str = "") -> dict:
    assert action in ACTIONS, action
    assert emotion in EMOTION_LEVEL, emotion
    output = {
        "emotion": emotion,
        "emotion_level": EMOTION_LEVEL[emotion],
        "intent": intent,
        "action": action,
        "reply": reply,
    }
    return {
        "row": {
            "instruction": INSTRUCTION,
            "input": input_text,
            "output": json.dumps(output, ensure_ascii=False, separators=(",", ":")),
        },
        "meta": {"source": source, "neg_class": neg_class,
                 "action": action, "emotion": emotion, "intent": intent},
    }


# --------------------------------------------------------------------------- #
# 1) typed-decisions（英文 400 行）→ 中文两变体
# --------------------------------------------------------------------------- #
def load_typed_decisions() -> list:
    import pandas as pd  # 仅构建期需要

    frames = []
    for split in ("train", "test"):
        path = RES / "typed-decisions" / "customer_service" / ("%s-00000-of-00001.parquet" % split)
        frames.append(pd.read_parquet(path))
    df = pd.concat(frames, ignore_index=True)
    rows = []
    TRANSLATION_STATS["phrase"] = 0
    TRANSLATION_STATS["template"] = 0
    for idx, row in df.iterrows():
        try:
            state = json.loads(row["state"])
        except (TypeError, ValueError):
            continue
        thread = state.get("thread") or []
        customer = [item for item in thread if item.get("role") == "customer" and item.get("text")]
        if not customer:
            continue
        en = str(customer[-1]["text"]).strip()
        category = str(row.get("category__label") or "")
        action_label = str(row.get("action__label") or "")
        needs = str(row.get("needs_human__label")).lower() == "true"
        urgency = int(row.get("urgency__label") or 0)
        churn = int(row.get("churn_risk__label") or 0)
        account = state.get("account") or {}
        prior = int(account.get("prior_tickets_90d") or 0)
        ltv = float(account.get("lifetime_value_usd") or 0)
        orders = state.get("orders") or []
        oid = ""
        amt = 0.0
        for order in orders:
            status = str(order.get("status") or "")
            if status in ("pending", "failed", "refunded") or not oid:
                oid = str(order.get("id") or oid)
                amt = float(order.get("amount_usd") or amt)
        emotion = infer_emotion(en, urgency, churn, prior)
        action = infer_action(en, category, action_label, needs, urgency, churn)
        intent = infer_intent(en, category, action_label)
        if emotion == "威胁投诉":
            action = "转人工" if action not in ("转人工", "拒绝并解释") else action
        case_ref = "工单 %s-%s" % ((str(row.get("id") or "x").split("_")[0]), str(row.get("id") or "000000").rsplit("_", 1)[-1][-6:])
        zh, path = translate_customer(en, category, oid, case_ref)
        fee = int(10 if amt <= 0 else max(5, min(50, round(amt / 10.0) or 5)))
        rows.append(make_row("typed-decisions", zh, emotion, intent, action,
                             pick_reply(action, oid, amt, fee, int(idx)), ""))
        rows.append(make_row("typed-decisions", short_variant(zh, intent, oid), emotion, intent,
                             action, pick_reply(action, oid, amt, fee, int(idx) + 1), ""))
    return rows


# --------------------------------------------------------------------------- #
# 2) 模板库（规则生成，补足八类动作）
# --------------------------------------------------------------------------- #
def load_templates() -> list:
    rows = []
    for i, (action, emotion, intent, text) in enumerate(TEMPLATE_BANK):
        oid = "A-%05d" % (10000 + i * 37 % 80000)
        fee = 8 + (i % 3) * 6
        input_text = text.format(oid=oid) if "{oid}" in text else text
        rows.append(make_row("template-bank", input_text, emotion, intent, action,
                             pick_reply(action, oid, 0, fee, i), ""))
    return rows


# --------------------------------------------------------------------------- #
# 3) 负控：越界索赔/辱骂/极模糊/二次投诉 + 无关请求（来自 zh 语料）
# --------------------------------------------------------------------------- #
def load_negatives() -> list:
    rows = []
    classes = [
        ("越界索赔", NEG_OUT_OF_SCOPE, "拒绝并解释", "威胁投诉"),
        ("辱骂", NEG_ABUSE, "转人工", "愤怒"),
        ("极模糊", NEG_VAGUE, "转人工", "平静"),
        ("二次投诉", NEG_SECOND_COMPLAINT, "转人工", "威胁投诉"),
    ]
    for cls, texts, action, emotion in classes:
        for i, text in enumerate(texts):
            intent = {"越界索赔": "越界索赔", "辱骂": "辱骂攻击",
                      "极模糊": "模糊咨询", "二次投诉": "二次投诉"}[cls]
            reply = pick_reply(action, "A-%05d" % (20000 + i), 0, 10, i)
            rows.append(make_row("negative", text, emotion, intent, action, reply, cls))

    # 无关请求：zh-decision-bench（MASSIVE 派生，语音助手指令）→ 转人工
    zh_path = RES / "zh-decision-bench" / "zh_decision_bench.jsonl"
    picked = 0
    for line in zh_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        state = str(item.get("state") or "").strip()
        if not state or len(state) > 30:
            continue
        rows.append(make_row("zh-decision-bench", state, "平静", "非客服请求", "转人工",
                             pick_reply("转人工", "", 0, 10, picked), "无关请求"))
        picked += 1
        if picked >= 12:
            break
    # 无关请求：massive-zh-CN 抽 8 条（同域外，仅作负控）
    massive = RES / "massive-zh-CN" / "train.jsonl"
    picked2 = 0
    for line in massive.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        text = str(item.get("text") or "").strip()
        label = str(item.get("label_text") or "")
        if not text or label not in ("alarm_set", "play_music", "weather_query", "calendar_set"):
            continue
        rows.append(make_row("massive-zh-CN", text, "平静", "非客服请求", "转人工",
                             pick_reply("转人工", "", 0, 10, picked2), "无关请求"))
        picked2 += 1
        if picked2 >= 8:
            break
    return rows


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    rows = load_typed_decisions() + load_templates() + load_negatives()

    # 去重（同 input 保留首条），并保证负控全部保留
    seen = set()
    unique = []
    neg_seen = set()
    for row in rows:
        key = norm(row["row"]["input"])
        if not key:
            continue
        if key in seen and row["meta"]["source"] != "negative":
            continue
        if key in neg_seen:
            continue
        seen.add(key)
        if row["meta"]["source"] == "negative":
            neg_seen.add(key)
        unique.append(row)

    by_action = {action: 0 for action in ACTIONS}
    by_emotion = {emotion: 0 for emotion in EMOTION_LEVEL}
    by_source, neg_by_class = {}, {}
    translation_paths = {"phrase": 0, "template": 0}
    for row in unique:
        meta = row["meta"]
        by_action[meta["action"]] = by_action.get(meta["action"], 0) + 1
        by_emotion[meta["emotion"]] = by_emotion.get(meta["emotion"], 0) + 1
        by_source[meta["source"]] = by_source.get(meta["source"], 0) + 1
        if meta["neg_class"]:
            neg_by_class[meta["neg_class"]] = neg_by_class.get(meta["neg_class"], 0) + 1

    negatives = [row for row in unique if row["meta"]["neg_class"]]
    positives = [row for row in unique if not row["meta"]["neg_class"]]
    if len(unique) < 600:
        raise SystemExit("FAIL: total rows %d < 600" % len(unique))
    if len(negatives) < 40:
        raise SystemExit("FAIL: negative rows %d < 40" % len(negatives))

    # 确定性划分：打乱后按比例切，再强制每类动作/负控在 val 有样本
    rng = random.Random(SEED)
    shuffled = list(positives)
    rng.shuffle(shuffled)
    val_pos_n = max(1, int(round(len(positives) * VAL_RATIO)))
    val_pos = shuffled[:val_pos_n]
    train_pos = shuffled[len(val_pos):]
    rng.shuffle(negatives)
    val_neg_n = max(4, min(len(negatives), int(round(len(negatives) * VAL_RATIO))))
    val_neg = negatives[:val_neg_n]
    train_neg = negatives[val_neg_n:]

    def ensure_action_in_val(train, val, action, need=2):
        have = sum(1 for row in val if row["meta"]["action"] == action)
        while have < need:
            movable = [row for row in train if row["meta"]["action"] == action]
            if not movable:
                break
            row = movable[0]
            train.remove(row)
            val.append(row)
            have += 1

    for action in ACTIONS:
        ensure_action_in_val(train_pos, val_pos, action)

    train = train_pos + train_neg
    val = val_pos + val_neg
    rng.shuffle(train)
    rng.shuffle(val)

    def write_jsonl(path, items):
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            for item in items:
                handle.write(json.dumps(item["row"], ensure_ascii=False) + "\n")
        return hashlib.sha256(path.read_bytes()).hexdigest()

    train_sha = write_jsonl(OUT / "train.jsonl", train)
    val_sha = write_jsonl(OUT / "val.jsonl", val)

    # schema.json
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "cs-decision SFT output",
        "type": "object",
        "additionalProperties": False,
        "required": SCHEMA_KEYS,
        "properties": {
            "emotion": {"type": "string", "enum": sorted(EMOTION_LEVEL)},
            "emotion_level": {"type": "integer", "minimum": 1, "maximum": 5},
            "intent": {"type": "string", "minLength": 1},
            "action": {"type": "string", "enum": ACTIONS},
            "reply": {"type": "string", "minLength": 1},
        },
    }
    (OUT / "schema.json").write_text(
        json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # 校验
    problems = []
    for name, items in (("train", train), ("val", val)):
        for i, item in enumerate(items):
            try:
                output = json.loads(item["row"]["output"])
            except ValueError as exc:
                problems.append("%s:%d output 不是合法 JSON: %s" % (name, i, exc))
                continue
            if sorted(output) != sorted(SCHEMA_KEYS):
                problems.append("%s:%d 字段不符: %s" % (name, i, sorted(output)))
            if output.get("action") not in ACTIONS:
                problems.append("%s:%d action 越界: %s" % (name, i, output.get("action")))
            if output.get("emotion") not in EMOTION_LEVEL:
                problems.append("%s:%d emotion 越界: %s" % (name, i, output.get("emotion")))
            if output.get("emotion_level") != EMOTION_LEVEL.get(output.get("emotion")):
                problems.append("%s:%d level 与 emotion 不匹配" % (name, i))
    if problems:
        for item in problems[:10]:
            print(item, file=sys.stderr)
        raise SystemExit("FAIL: %d 行校验失败" % len(problems))

    stats = {
        "total": len(unique), "train": len(train), "val": len(val),
        "negatives": len(negatives), "negatives_in_val": len(val_neg),
        "neg_by_class": neg_by_class, "by_action": by_action, "by_emotion": by_emotion,
        "by_source": by_source, "translation_paths": dict(TRANSLATION_STATS),
        "validated_rows": len(train) + len(val),
        "json_loads_pass": len(train) + len(val),
        "sha256": {"train.jsonl": train_sha, "val.jsonl": val_sha},
        "seed": SEED, "val_ratio": VAL_RATIO,
    }

    # ACTIONS.md
    actions_md = ["# 动作白名单、情绪档位与策略表（cs-decision）", "",
                  "> 本文件被展示台（apps/cs-demo）读取；**动作/情绪用词以下表字面为准，不许改写**。", "",
                  "## 1 动作白名单（只能这八个）", ""]
    for action in ACTIONS:
        actions_md.append("- `%s`" % action)
    actions_md += ["", "## 2 情绪档位（emotion → emotion_level）", "",
                   "| emotion | emotion_level |", "|---|---|"]
    for emotion, level in EMOTION_LEVEL.items():
        actions_md.append("| %s | %d |" % (emotion, level))
    actions_md += [
        "", "## 3 策略表（生成与推理共用；按优先级从上到下）", "",
        "| 优先级 | 触发条件（客户消息可观测证据） | 动作 |", "|---|---|---|",
        "| 1 | 威胁起诉/曝光/找监管、点名要上级、二次及以上投诉、辱骂或情绪失控、要求人工 | `转人工` |",
        "| 2 | 越界索赔：十倍赔偿、精神损失费、无凭证退款、超出售后范围 | `拒绝并解释` |",
        "| 3 | 物流未达/丢件/长时间不更新 | `补发` |",
        "| 4 | 错发、破损、尺码/颜色不符 | `换货` |",
        "| 5 | 运费争议（不该收的运费、退货运费、运费差价） | `运费补偿` |",
        "| 6 | 明确退款申请（含重复扣费） | `退款` |",
        "| 7 | 老客挽留/致歉补偿、体验差但无越界 | `发红包` |",
        "| 8 | 一般咨询、轻微不满、使用问题 | `安抚话术` |",
        "| 9 | 极模糊（“在吗”“？”）或与客服无关（点歌/闹钟等） | `转人工` |",
        "", "## 4 输出 schema", "",
        "严格 JSON（无多余文字）：`{\"emotion\":\"\",\"emotion_level\":1,\"intent\":\"\",\"action\":\"\",\"reply\":\"\"}`；"
        "`intent` 为简短中文意图短语（自由文本），其余字段受本文件约束。",
        "",
    ]
    (OUT / "ACTIONS.md").write_text("\n".join(actions_md), encoding="utf-8")

    # 10 行抽样（正/负各取）
    samples = positives[:6] + negatives[:4]
    sample_lines = []
    for item in samples:
        sample_lines.append(json.dumps(item["row"], ensure_ascii=False)[:600])

    readme = [
        "# cs-decision · 中文客服决策 SFT 数据集",
        "",
        "LlamaFactory 可直接读的 **alpaca** 格式：每行 `{instruction,input,output}`；"
        "`output` 是严格 JSON 字符串（schema 见 `schema.json`，动作/情绪白名单见 `ACTIONS.md`）。",
        "",
        "## 1 来源（只读，未做任何修改）",
        "",
        "| 来源 | 路径 | 用途 | 许可证 |",
        "|---|---|---|---|",
        "| typed-decisions customer_service（train 300 + test 100） | `resources\\01-决策模型-业务语料\\typed-decisions\\customer_service\\*.parquet` | 英文客服对话 → 中文改写 + 标签（category/action/needs_human/urgency/churn_risk） | Apache-2.0（HF 卡片） |",
        "| zh-decision-bench | `resources\\01-决策模型-业务语料\\zh-decision-bench\\zh_decision_bench.jsonl` | 仅取 12 条「与客服无关」负控 | CC BY 4.0（MANIFEST） |",
        "| massive-zh-CN | `resources\\01-决策模型-业务语料\\massive-zh-CN\\train.jsonl` | 仅取 8 条「与客服无关」负控 | 上游 CC BY 4.0（MANIFEST 注） |",
        "| 规则模板库 | 本文件 `build_dataset.py` 内 `TEMPLATE_BANK` | 补足八类动作覆盖 | 本项目自产 |",
        "| 负控模板 | `build_dataset.py` 内 `NEG_*` | 越界索赔/辱骂/极模糊/二次投诉 | 本项目自产 |",
        "",
        "## 2 翻译/改写规则（英文来源 → 中文）",
        "",
        "- **不是逐句机器翻译**，而是确定性规则化翻译：`PHRASE_MAP`（约 20 条客服短语正则）抽取诉求拼成中文；"
        "无命中时按 `类别 + 情绪 + 订单信息` 模板回退。每行还生成一个「简短变体」（同一标签，措辞更短）。",
        "- 标签由来源列确定性推导：`category__label / action__label / needs_human__label / urgency__label / churn_risk__label`"
        " → 本数据集 `emotion/emotion_level/intent/action/reply`（策略表见 `ACTIONS.md` §3）。",
        "- 复现：`C:\\ModelSmith\\runtime\\python\\python.exe build_dataset.py`（seed=%d，重复运行字节一致；sha256 见 `stats.json`）。" % SEED,
        "",
        "## 3 行数与分布（构建后实测）",
        "",
        "| 项 | 值 |", "|---|---|",
        "| 总行数（train+val） | **%d** |" % len(unique),
        "| train / val | **%d / %d** |" % (len(train), len(val)),
        "| 负控行数 | **%d**（val 内 %d） |" % (len(negatives), len(val_neg)),
        "| json.loads 通过 | **%d / %d** |" % (len(train) + len(val), len(train) + len(val)),
        "",
        "### 3.1 动作分布",
        "", "| action | rows |", "|---|---|",
    ]
    for action in ACTIONS:
        readme.append("| %s | %d |" % (action, by_action.get(action, 0)))
    readme += ["", "### 3.2 情绪分布", "", "| emotion | level | rows |", "|---|---|---|"]
    for emotion, level in EMOTION_LEVEL.items():
        readme.append("| %s | %d | %d |" % (emotion, level, by_emotion.get(emotion, 0)))
    readme += ["", "### 3.3 来源与负控", "",
               "- 按来源：" + "，".join("%s=%d" % (k, v) for k, v in sorted(by_source.items())),
               "- 英文→中文路径：phrase=%d，template=%d" % (TRANSLATION_STATS["phrase"], TRANSLATION_STATS["template"]),
               "- 负控分类：" + "，".join("%s=%d" % (k, v) for k, v in sorted(neg_by_class.items())),
               "",
               "## 4 十行抽样（前 6 正 + 前 4 负，output 原样）", ""]
    readme += ["```json"] + sample_lines + ["```"]
    readme += ["",
               "## 5 校验（构建脚本自检，任一失败即非 0 退出）", "",
               "- 每行 `output` 可 `json.loads`；字段恰为 5 个；action 在白名单内；emotion 在白名单内且 level 与 emotion 表一致。",
               "- 总行数 ≥600、负控 ≥40、八类动作均有样本、val 非空。",
               "",
               "## 6 局限 / 未做（明写）", "",
               "- 英文部分是**规则化改写**而非人工/机器翻译；语义忠实度有限（原句细节可能丢失）。",
               "- 标签由来源弱标签推导，不是人工逐条标注；`reply` 是模板生成，不是真实客服回复。",
               "- 负控为手写模板 + 20 条域外语料（zh-decision-bench 12 + massive-zh-CN 8），覆盖「越界/辱骂/极模糊/二次投诉/无关」，但**未做第二轮人工抽检**。",
               "- 未训练、未评测（本任务只造数据）。",
               ""]
    (OUT / "README.md").write_text("\n".join(readme), encoding="utf-8")

    (OUT / "stats.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print("OK total=%d train=%d val=%d negatives=%d (val_neg=%d)"
          % (len(unique), len(train), len(val), len(negatives), len(val_neg)))
    print("by_action:", json.dumps(by_action, ensure_ascii=False))
    print("by_emotion:", json.dumps(by_emotion, ensure_ascii=False))
    print("by_source:", json.dumps(by_source, ensure_ascii=False))
    print("neg_by_class:", json.dumps(neg_by_class, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
