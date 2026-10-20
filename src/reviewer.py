"""评审主流程：分批通道 + 全局通道 → 合并 → 渲染。

两条通道的必查清单**必须不重叠**。开了全局通道，分批通道的 prompt 里就要把
"越权/事务/契约/并发"四项去掉，并明确告诉模型"别报这些"——否则同一个问题报两遍，
评论噪声翻倍，团队很快就会把这个机器人静音。
"""
import json

from .config import env_bool
from .diffutil import diff_stats, split_comment, split_diff_batches
from .llm import llm_chat, parse_json_reply, salvage_truncated_json

# ---------------------------------------------------------------- prompts

_LOCAL_ITEMS = """1. 空指针与未处理的分支（null / 空集合 / 越界）
2. 注入与拼接（SQL 拼接、命令拼接、路径拼接、反序列化）
3. 资源未释放（连接、流、锁、线程池）
4. 异常处理（吞异常、捕获过宽、异常信息泄露）
5. 边界与并发（整数溢出、时区、重复提交、非原子操作）
6. 敏感信息（日志打印口令/token、硬编码密钥）"""

_GLOBAL_ITEMS = """1. 越权与鉴权缺口（新增接口是否被鉴权拦截覆盖、是否存在路径绕过）
2. 事务边界（注解是否生效、自调用是否失效、跨服务的一致性）
3. 接口契约（改签名/改返回值是否破坏现有调用方）
4. 并发与共享状态（ThreadLocal 是否清理、单例里的可变状态）"""

SYSTEM_PROMPT_FULL = """你是一名资深代码评审工程师，负责审查合并请求的代码变更。

必查清单（只报这些，其余一律不提）：
%s

硬性要求：
- 只报**会导致故障、数据错误、安全问题**的问题。风格、命名、注释多少、格式，一律不提。
- 每条问题必须能落到具体的文件和行号，说不清位置就不要报。
- 必须给出"会导致什么后果"。给不出后果的，说明它不重要，不要报。
- 无法确认的地方，写"需结合上下文确认"，**禁止**下"可以安全合并"这类绝对结论。
- 宁可少报，不要凑数。一次评审超过 10 条，说明标准松了。

输出必须是 JSON：
{"summary": "一句话结论", "risk": "high|medium|low",
 "issues": [{"file": "路径", "line": 12, "level": "high|medium|low",
             "title": "问题一句话", "detail": "为什么是问题，会导致什么后果",
             "suggestion": "怎么改"}]}
"""

SYSTEM_PROMPT_LOCAL = """你是一名资深代码评审工程师，负责审查合并请求的代码变更。

必查清单（只报这些，其余一律不提）：
%s

注意：本次评审按文件分批进行，你只看得到其中一批文件。
以下四类问题**由另一条通道负责，不要报**：越权/鉴权、事务边界、接口契约、并发共享状态。

硬性要求：
- 只报**会导致故障、数据错误、安全问题**的问题。风格、命名、注释多少、格式，一律不提。
- 每条问题必须能落到具体的文件和行号。
- 必须给出"会导致什么后果"。
- 跨文件才能判定的问题，写成"需结合上下文确认"并指出要人工核对哪个文件。
- **禁止**下"可以安全合并"这类绝对结论。
- 宁可少报，不要凑数。

输出必须是 JSON：
{"summary": "一句话结论", "risk": "high|medium|low",
 "issues": [{"file": "路径", "line": 12, "level": "high|medium|low",
             "title": "问题一句话", "detail": "为什么是问题，会导致什么后果",
             "suggestion": "怎么改"}]}
"""

SYSTEM_PROMPT_GLOBAL = """你是一名资深架构评审工程师，本次只做**全局视角**审查。

你拿到的是：变更骨架（这个 MR 改了哪些类/方法）+ 若干"横切文件"的全文
（鉴权 filter、拦截器、事务配置、公共上下文等）。

只报这四类：
%s

硬性要求：
- 不要报局部代码问题（空指针、资源释放等由另一条通道负责）。
- 每条结论必须指出：你依据的是哪个横切文件、变更里的哪一处触发了它。
- 说不清依据的，不要报。

输出必须是 JSON：
{"summary": "一句话结论", "risk": "high|medium|low",
 "issues": [{"file": "横切文件路径", "line": 0, "level": "high|medium|low",
             "title": "问题一句话", "detail": "依据什么、会导致什么",
             "suggestion": "怎么改", "scope": "global"}]}
"""


def system_prompt(cfg):
    if cfg.global_review:
        return SYSTEM_PROMPT_LOCAL % _LOCAL_ITEMS
    return SYSTEM_PROMPT_FULL % (_LOCAL_ITEMS + "\n" + _GLOBAL_ITEMS)


def build_user_prompt(mr, diff_text, batch=None, ctx=None):
    """把"上下文缺口"明确告诉模型——这是降低误判最有效的一招。

    实测：模型收到缺口说明后，会主动把跨文件结论写成"需结合上下文确认"，
    而不是硬下判断。
    """
    parts = [
        "合并请求：%s" % (mr.get("title") or "(无标题)"),
        "仓库：%s" % (mr.get("repo", {}).get("gitUrl") or ""),
        "分支：%s → %s" % (mr.get("source", ""), mr.get("target", "")),
    ]
    if batch and ctx:
        parts.append(
            "本次共 %d 个文件，本批只包含 %d 个。未包含：%s"
            % (
                len(ctx.get("all_files") or []),
                len(batch.get("files") or []),
                ", ".join((ctx.get("missing") or [])[:20]) or "无",
            )
        )
        parts.append("跨文件才能判定的问题，请写成「需结合上下文确认」，并指明要核对哪个文件。")
    parts.append("\n===== diff =====\n")
    parts.append(diff_text)
    return "\n".join(parts)


# ---------------------------------------------------------------- review

def review_text(cfg, mr, text, tag, depth=0, ctx=None):
    """评一批。输出撞上限时自适应对半拆，最多 depth 层。"""
    messages = [
        {"role": "system", "content": system_prompt(cfg)},
        {"role": "user", "content": build_user_prompt(mr, text, {"files": []}, ctx)},
    ]
    try:
        content, usage, finish = llm_chat(cfg, messages)
    except Exception as exc:
        return {"issues": [], "summary": "%s 调用失败：%s" % (tag, exc), "risk": "unknown"}

    try:
        return parse_json_reply(content)
    except Exception:
        pass

    # 输出被截断：先抢救，再考虑拆
    salvaged = salvage_truncated_json(content)
    if salvaged:
        salvaged.setdefault("summary", "%s（输出被截断，仅保留已完整部分）" % tag)
        return salvaged

    if depth < cfg.max_split_depth and len(text) > 2000:
        half = len(text) // 2
        cut = text.rfind("\ndiff --git ", 0, half)
        cut = cut if cut > 0 else half
        left = review_text(cfg, mr, text[:cut], tag + "-a", depth + 1, ctx)
        right = review_text(cfg, mr, text[cut:], tag + "-b", depth + 1, ctx)
        return {
            "summary": "%s（已拆分为两批）" % tag,
            "risk": _max_risk([left.get("risk"), right.get("risk")]),
            "issues": (left.get("issues") or []) + (right.get("issues") or []),
        }

    return {"issues": [], "summary": "%s 解析失败且无法抢救" % tag, "risk": "unknown"}


def review_batches(cfg, mr, batches, all_files):
    results, metas = [], []
    total = len(batches)
    for i, b in enumerate(batches, 1):
        in_batch = b.get("files") or []
        missing = [f for f in all_files if f not in in_batch]
        ctx = {"all_files": all_files, "in_batch": in_batch, "missing": missing}
        r = review_text(cfg, mr, b["text"], "第 %d/%d 批" % (i, total), 0, ctx)
        results.append(r)
        metas.append({"index": i, "files": in_batch, "ok": bool(r.get("issues") is not None)})
        print("第 %d/%d 批：%d 个问题" % (i, total, len(r.get("issues") or [])), flush=True)
    return results, metas


def merge_batch_results(parts):
    """合并多批结果。同一文件同一行只留一条，避免重复刷屏。"""
    issues, seen = [], set()
    summaries, risks = [], []
    for p in parts:
        if p.get("summary"):
            summaries.append(p["summary"])
        risks.append(p.get("risk"))
        for it in (p.get("issues") or []):
            key = (it.get("file"), it.get("line"), (it.get("title") or "")[:20])
            if key in seen:
                continue
            seen.add(key)
            issues.append(it)

    order = {"high": 0, "medium": 1, "low": 2}
    issues.sort(key=lambda x: order.get(x.get("level"), 3))
    return {
        "summary": "；".join(summaries[:3]) or "未发现问题",
        "risk": _max_risk(risks),
        "issues": issues,
    }


def _max_risk(risks):
    order = {"high": 0, "medium": 1, "low": 2}
    valid = [r for r in (risks or []) if r in order]
    if not valid:
        return "unknown"
    return sorted(valid, key=lambda r: order[r])[0]


# ---------------------------------------------------------------- render

LEVEL_LABEL = {"high": "高", "medium": "中", "low": "低"}


def render_comment(cfg, mr, result, stats, truncated, batch_meta=None):
    issues = result.get("issues") or []
    risk = result.get("risk") or "unknown"
    head = {
        "high": "发现高危问题，建议修改后再合并",
        "medium": "发现中危问题，建议确认",
        "low": "发现低风险问题",
        "unknown": "评审未完成或结果不完整",
    }.get(risk, "评审完成")

    lines = ["## AI 代码评审 · %s" % head, ""]
    lines.append("**结论**：%s" % (result.get("summary") or ""))
    lines.append("")
    lines.append("变更规模：%d 个文件，+%d / -%d 行"
                 % (stats["files"], stats["plus"], stats["minus"]))
    if truncated:
        lines.append("")
        lines.append("> 变更体量超过单次上限，**只评审了前 %d 批**，后序未覆盖。"
                     % len(batch_meta or []))
    lines.append("")

    if not issues:
        lines.append("本次未发现必查清单内的问题。")
    else:
        for it in issues:
            lvl = LEVEL_LABEL.get(it.get("level"), "低")
            loc = it.get("file") or "?"
            if it.get("line"):
                loc += ":%s" % it["line"]
            lines.append("### [%s] %s" % (lvl, it.get("title") or "问题"))
            lines.append("位置：`%s`" % loc)
            if it.get("detail"):
                lines.append("说明：%s" % it["detail"])
            if it.get("suggestion"):
                lines.append("建议：%s" % it["suggestion"])
            lines.append("")

    if batch_meta and len(batch_meta) > 1:
        lines.append("---")
        lines.append("> 本 MR 分批评审（%d 批）。跨文件判断可能不完整，请以人工复核为准。"
                     % len(batch_meta))

    return "\n".join(lines)


def post_review(cu, cfg, mr, text):
    """发评论。超长按行拆成多条，绝不丢内容。"""
    parts = split_comment(text, cfg.comment_max_chars)
    for i, part in enumerate(parts, 1):
        content = part if len(parts) == 1 else "【AI 评审 %d/%d】\n\n%s" % (i, len(parts), part)
        status, data = cu.create_comment(mr["repo"]["id"], mr["local_id"], content)
        if status not in (200, 201):
            raise RuntimeError("发评论失败 HTTP %s: %s" % (status, str(data)[:200]))
