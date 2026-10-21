"""命令行入口：不依赖集群，本地就能跑通并看到要发的评论。"""
import argparse
import os
import sys

from . import notify, state as statemod
from .codeup import Codeup, mr_version, opened_mrs
from .config import Config
from .diffutil import diff_stats, make_diff, split_diff_batches
from .reviewer import merge_batch_results, post_review, render_comment, review_batches


def find_repo(cfg, key):
    for r in cfg.repos:
        if key in (r.get("id"), (r.get("gitUrl") or "").rstrip("/").split("/")[-1]):
            return r
    return None


def run_review(cfg, cu, mr, no_post=False):
    """评一个 MR。返回合并后的结果，被去重跳过时返回 None。"""
    diff = make_diff(cfg, mr["repo"], mr["source"], mr["target"])
    if not diff.strip():
        print("%s diff 为空，跳过" % repo_key(mr), flush=True)
        return None

    stats = diff_stats(diff)
    batches, all_files, truncated = split_diff_batches(
        diff, cfg.max_diff_chars, cfg.max_batches)
    print("%s 分 %d 批评审，共 %d 个文件"
          % (repo_key(mr), len(batches), stats["files"]), flush=True)

    results, metas = review_batches(cfg, mr, batches, all_files)
    merged = merge_batch_results(results)
    text = render_comment(cfg, mr, merged, stats, truncated, metas)

    if no_post or cfg.dry_run:
        print("\n" + "=" * 60)
        print(text)
        print("=" * 60)
        return merged

    post_review(cu, cfg, mr, text)
    notify.notify_review(cfg.wecom_webhooks, mr, merged,
                         max_issues=cfg.max_issues,
                         level_filter=cfg.notify_levels)
    print("%s 已发评论" % repo_key(mr), flush=True)
    return merged


def repo_key(mr):
    return "%s#%s" % (mr["repo"]["id"], mr["local_id"])


def cmd_list(cfg, cu, args):
    for m in opened_mrs(cu, cfg):
        print("%s  #%s  %s  %s -> %s"
              % ((m["repo"].get("gitUrl") or "").split("/")[-1],
                 m["local_id"], (m.get("title") or "")[:40],
                 m.get("source"), m.get("target")))


def cmd_check(cfg, cu, args):
    """自查：配置有没有生效、版本去重是否正常。部署后第一个该跑的命令。"""
    print("=== 配置 ===")
    print("模型        : %s @ %s" % (cfg.llm_model, cfg.llm_base))
    print("仓库数      : %d" % len(cfg.repos))
    print("每批上限    : %d 字符" % cfg.max_diff_chars)
    print("最多批数    : %d" % cfg.max_batches)
    print("全局通道    : %s" % ("开" if cfg.global_review else "关"))
    print("单次条数    : %d" % cfg.max_issues)
    print("企微群      : %s" % ("%d 个" % len(cfg.wecom_webhooks) if cfg.wecom_webhooks else "未配置（不推群）"))
    print("推群级别    : %s" % (",".join(cfg.notify_levels) or "全部"))
    print("提交状态    : %s（context=%s）"
          % ("开" if cfg.commit_status else "关", cfg.check_context))
    print("dry-run     : %s" % ("是" if cfg.dry_run else "否"))
    print("状态文件    : %s" % cfg.state_file)
    print()
    print("=== opened MR 与最新版本 ===")
    for m in opened_mrs(cu, cfg):
        ver = mr_version(cu, m["repo"], m["local_id"])
        print("#%s  version=%s  %s" % (m["local_id"], ver or "?", (m.get("title") or "")[:40]))


def cmd_once(cfg, cu, args):
    repo = find_repo(cfg, args.repo) if args.repo else (cfg.repos[0] if cfg.repos else None)
    if not repo:
        sys.exit("找不到仓库：%s" % args.repo)

    mrs = opened_mrs(cu, cfg, only_repo=repo)
    target = next((m for m in mrs if str(m["local_id"]) == str(args.mr)), None)
    if not target:
        sys.exit("找不到 opened 的 MR #%s（已合并/关闭的补评不了）" % args.mr)

    run_review(cfg, cu, target, no_post=args.dry_run)


def cmd_poll(cfg, cu, args):
    """定时兜底：把所有 opened MR 扫一遍，版本变了的评。

    给 cronjob 用。和 webhook 走同一套去重状态，所以两边同时开也不会重复评。
    """
    st = statemod.load(cfg.state_file)
    done = skipped = 0

    for m in opened_mrs(cu, cfg):
        key = repo_key(m)
        ver = mr_version(cu, m["repo"], m["local_id"])
        if ver and st.get(key) == ver:
            skipped += 1
            continue
        if args.limit and done >= args.limit:
            print("已达本次上限 %d 个，剩下的下次再说" % args.limit, flush=True)
            break
        try:
            run_review(cfg, cu, m)
        except Exception as exc:
            print("%s 评审失败：%r" % (key, exc), flush=True)
            continue
        if ver:
            st[key] = ver
            statemod.save(cfg.state_file, st)
        done += 1

    print("本轮：评了 %d 个，跳过 %d 个（版本未变）" % (done, skipped), flush=True)


def main(argv=None):
    p = argparse.ArgumentParser(prog="codeup-ai-reviewer")
    p.add_argument("--env", default=None, help="dotenv 文件路径，默认 .env")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("serve")
    s = sub.add_parser("list"); s.set_defaults(func=cmd_list)
    s = sub.add_parser("check"); s.set_defaults(func=cmd_check)
    s = sub.add_parser("once"); s.set_defaults(func=cmd_once)
    s.add_argument("--mr", required=True, help="合并请求编号")
    s.add_argument("--repo", default=None, help="仓库 id 或名字")
    s.add_argument("--dry-run", action="store_true", help="只打印，不发评论")
    s = sub.add_parser("poll"); s.set_defaults(func=cmd_poll)
    s.add_argument("--limit", type=int, default=0, help="本轮最多评几个，0 = 不限")

    args = p.parse_args(argv)

    # --env 之前是摆设：声明了但没接到 Config 上。现在通过 MRR_ENV_FILE 传进去。
    if args.env:
        os.environ["MRR_ENV_FILE"] = args.env

    cfg = Config()
    cfg.require()
    cu = Codeup(cfg)

    if args.cmd == "serve":
        from .server import serve
        serve(cfg, cu)
        return
    if not getattr(args, "func", None):
        p.print_help()
        return
    args.func(cfg, cu, args)


if __name__ == "__main__":
    main()
