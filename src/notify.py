"""企业微信机器人通知。

第三道闸门：企微 text 消息上限 **2048 字节**、markdown 上限 **4096 字节**。
超了平台会直接截断，**末尾的 MR 链接会消失**——这比不发还糟，因为人点不进去。

所以这里不能"尾部硬切"，必须**减少条数后重新渲染**。
"""
import json

from .codeup import http_json

TEXT_LIMIT = 2048
MD_LIMIT = 4096


def _bytes(s):
    return len(s.encode("utf-8"))


def build_text(mr, result, max_issues):
    """纯文本摘要。给"手机通知栏"看的，越短越好。"""
    issues = (result.get("issues") or [])[:max_issues]
    risk = result.get("risk") or "unknown"
    head = {"high": "高危", "medium": "中危", "low": "低风险"}.get(risk, "待确认")
    lines = ["【AI 评审】%s · %s" % (head, mr.get("title") or "")[:40]]
    for it in issues:
        lines.append("· [%s] %s" % (it.get("level", ""), (it.get("title") or "")[:40]))
    total = len(result.get("issues") or [])
    if total > len(issues):
        lines.append("（共 %d 条，仅列前 %d 条）" % (total, len(issues)))
    if mr.get("url"):
        lines.append("详情：%s" % mr["url"])
    return "\n".join(lines)


def build_markdown(mr, result, max_issues):
    issues = (result.get("issues") or [])[:max_issues]
    total = len(result.get("issues") or [])
    risk = result.get("risk") or "unknown"
    color = {"high": "warning", "medium": "comment", "low": "info"}.get(risk, "comment")
    lines = ["<font color=\"%s\">**AI 评审 · %s**</font>" % (color, mr.get("title") or "")]
    if result.get("summary"):
        lines.append("> %s" % result["summary"])
    for it in issues:
        lines.append("· **%s** `%s`" % (it.get("level", ""), it.get("file") or ""))
        lines.append("  %s" % (it.get("title") or ""))
    if total > len(issues):
        lines.append("<font color=\"comment\">共 %d 条，仅列前 %d 条</font>" % (total, len(issues)))
    if mr.get("url"):
        lines.append("[查看合并请求](%s)" % mr["url"])
    return "\n".join(lines)


def fit(mr, result, limit, builder, max_issues=5):
    """不断减少条数重新渲染，直到不超限。保证末尾链接一定发得出去。"""
    n = max_issues
    while n >= 1:
        msg = builder(mr, result, n)
        if _bytes(msg) <= limit:
            return msg
        n -= 1
    # 连一条都放不下：退化成只有标题和链接
    return builder(mr, {"issues": [], "risk": result.get("risk"),
                        "summary": result.get("summary")}, 0)


def notify_review(webhooks, mr, result, max_issues=5, level_filter=None):
    """webhooks: 该仓库对应的群机器人地址列表。

    level_filter: 只在这些风险级别时才推群（例如只推 high）。
    """
    risk = result.get("risk") or "unknown"
    if level_filter and risk not in level_filter:
        return 0

    sent = 0
    for hook in webhooks or []:
        text = fit(mr, result, TEXT_LIMIT, build_text, max_issues)
        status, _ = http_json("POST", hook, body={"msgtype": "text", "text": {"content": text}})
        if status == 200:
            sent += 1
        else:
            # 群机器人失败不能影响评审本身，只记日志
            print("企微通知失败 HTTP %s" % status, flush=True)
    return sent
