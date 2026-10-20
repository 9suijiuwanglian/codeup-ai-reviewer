"""模型调用与 JSON 解析。

第一道闸门在这儿：模型输出撞上 max_tokens 时，JSON 会**断在半路**，
最常见是 issues 数组中间。整批丢掉太浪费，所以有一层"抢救"。
"""
import json
import re

from .codeup import http_json


def llm_chat(cfg, messages):
    """调 OpenAI 兼容接口。返回 (content, usage, finish_reason)。"""
    url = cfg.llm_base + "/chat/completions"
    headers = {"Authorization": "Bearer " + cfg.llm_key, "Accept": "application/json"}
    body = {
        "model": cfg.llm_model,
        "messages": messages,
        "stream": False,
        "temperature": cfg.llm_temperature,
        "response_format": {"type": "json_object"},
    }
    if cfg.llm_max_tokens > 0:
        body["max_tokens"] = cfg.llm_max_tokens

    timeout = max(cfg.timeout, 300)
    status, data = http_json("POST", url, headers=headers, body=body, timeout=timeout)

    # 不是所有模型/网关都支持 response_format，失败就去掉重试一次
    if status != 200:
        body.pop("response_format", None)
        status, data = http_json("POST", url, headers=headers, body=body, timeout=timeout)

    if status != 200:
        raise RuntimeError("调用模型失败 HTTP %s: %s" % (status, str(data)[:400]))

    try:
        choice = data["choices"][0]
        content = choice["message"]["content"]
        finish = choice.get("finish_reason")
    except Exception:
        raise RuntimeError("模型返回结构异常: %s" % str(data)[:400])

    if not content or not str(content).strip():
        raise RuntimeError(
            "模型返回内容为空 (finish_reason=%s)。常见原因：单批 diff 偏大，"
            "或输出预算被推理吃光。调大 LLM_MAX_TOKENS 或调小 MAX_DIFF_CHARS。"
            % finish
        )
    return content, (data.get("usage") or {}), finish


def parse_json_reply(text):
    """模型经常带 ```json 包裹，或者在 JSON 前后加一段解释，都得容错。"""
    t = (text or "").strip()
    if not t:
        raise ValueError("模型返回内容为空")
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except Exception:
        pass
    start, end = t.find("{"), t.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(t[start:end + 1])
        except Exception:
            pass
    raise ValueError("无法解析模型返回的 JSON：%s" % t[:300])


def salvage_truncated_json(text):
    """从被截断的 JSON 里救出已完整的部分。

    做法：从后往前逐步去掉残缺的尾部，直到能解析。
    救不出来返回 None（调用方决定是报错还是当空结果）。

    这是"宁可少报，不要整批白跑"的取舍——被截断说明输出撞上了上限，
    前面已经分析完的问题不该跟着一起丢。
    """
    t = (text or "").strip()
    if not t:
        return None
    t = re.sub(r"^```[a-zA-Z]*\s*", "", t)

    # 从尾部往前找可闭合的位置
    for cut in range(len(t), max(len(t) - 4000, 0), -1):
        frag = t[:cut]
        # 补齐未闭合的数组/对象括号
        for _ in range(4):
            try:
                obj = json.loads(frag)
                if isinstance(obj, dict):
                    obj["_salvaged"] = True
                    return obj
            except Exception:
                pass
            lf, rf = frag.rfind("{"), frag.rfind("[")
            if lf < 0 and rf < 0:
                break
            tail = "}" if lf > rf else "]"
            frag = frag + tail
    return None
