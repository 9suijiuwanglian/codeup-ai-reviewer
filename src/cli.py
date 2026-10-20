"""命令行入口：不依赖集群，本地就能跑通并看到要发的评论。"""
import argparse
import sys

from .codeup import Codeup, mr_version, opened_mrs
from .config import Config
from .diffutil import diff_stats, make_diff, split_diff_batches
from .reviewer import merge_batch_results, post_review, render_comment, review_batches


def find_repo(cfg, key):
    for r in cfg.repos:
        if key in (r.get("id"), (r.get("gitUrl") or "").rstrip("/").split("/")[-1]):
            return r
    return None


def cmd_list(cfg, cu, args):
    for m in opened_mrs(cu, cfg):
        print("%s  #%s  %s  %s -> %s"
              % (m["repo"].get("gitUrl", "").split("/")[-1],
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
    print("dry-run     : %s" % ("是" if cfg.dry_run else "否"))
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

    diff = make_diff(cfg, repo, target["source"], target["target"])
    stats = diff_stats(diff)
    batches, all_files, truncated = split_diff_batches(
        diff, cfg.max_diff_chars, cfg.max_batches)
    print("分 %d 批评审，共 %d 个文件" % (len(batches), stats["files"]), flush=True)

    results, metas = review_batches(cfg, target, batches, all_files)
    merged = merge_batch_results(results)
    text = render_comment(cfg, target, merged, stats, truncated, metas)

    print("\n" + "=" * 60)
    print(text)
    print("=" * 60)

    if not (args.dry_run or cfg.dry_run):
        post_review(cu, cfg, target, text)
        print("已发评论")


def main(argv=None):
    p = argparse.ArgumentParser(prog="codeup-ai-reviewer")
    p.add_argument("--env", default=".env", help="dotenv 文件路径")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("serve"); s.set_defaults(func=None)
    s = sub.add_parser("list"); s.set_defaults(func=cmd_list)
    s = sub.add_parser("check"); s.set_defaults(func=cmd_check)
    s = sub.add_parser("once"); s.set_defaults(func=cmd_once)
    s.add_argument("--mr", required=True, help="合并请求编号")
    s.add_argument("--repo", default=None, help="仓库 id 或名字")
    s.add_argument("--dry-run", action="store_true", help="只打印，不发评论")

    args = p.parse_args(argv)
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
