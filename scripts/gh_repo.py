#!/usr/bin/env python3
"""GitHub 仓库发布工具（子命令式）。

用法：
    python -u gh_repo.py check
    python -u gh_repo.py audit    <目录>
    python -u gh_repo.py create   <仓库名> [--desc 描述] [--private] [--topics a,b,c]
    python -u gh_repo.py push     <本地仓库目录> [--branch main] [--remote origin]
    python -u gh_repo.py verify   <本地仓库目录> [--owner 账号] [--repo 仓库名] [--branch main]

设计要点：
- 凭据取自 Git 凭据管理器（GCM），全程不落盘、不打印。
- `push` 会自动探测通道：github.com 可达则走常规 git push，
  不可达但 api.github.com 可达则改走 Git Data API —— 后者因 Git 对象内容寻址，
  重建出的 commit SHA 与本地完全相同，不会造成分叉。
- `verify` 只认服务端实况，不用本地远端跟踪引用（本机该引用可能回滚）。
- 建议加 `-u` 并重定向到文件运行：本机长脚本的 stdout 偶发整体丢失。
"""

import argparse
import base64
import datetime
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

API = "https://api.github.com"
UA = "gh-repo-publish-skill"


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
class ApiError(Exception):
    def __init__(self, code, path, detail):
        super().__init__("HTTP %s %s -> %s" % (code, path, detail[:300]))
        self.code = code
        self.detail = detail


def get_token(tries=3, timeout=120):
    """从 GCM 取 GitHub 令牌。必须传完整 os.environ，否则 GCM 找不到凭据库。

    凭据助手偶发挂起（实测同一函数前一次调用正常、紧接着一次超时），故带重试。
    """
    env = dict(os.environ)
    env["GCM_INTERACTIVE"] = "never"
    env["GIT_TERMINAL_PROMPT"] = "0"
    last = None
    for _ in range(tries):
        try:
            r = subprocess.run(["git", "credential", "fill"],
                               input="protocol=https\nhost=github.com\n\n",
                               capture_output=True, text=True, timeout=timeout, env=env)
        except subprocess.TimeoutExpired:
            last = "等待凭据助手超时（%ds）" % timeout
            continue
        if r.returncode != 0:
            last = "退出码 %s：%s" % (r.returncode, (r.stderr or "").strip()[:200])
            continue
        for line in (r.stdout or "").splitlines():
            if line.startswith("password="):
                return line.split("=", 1)[1]
        last = "输出中没有 password 字段"
        time.sleep(2)
    raise SystemExit("凭据读取失败（已重试 %d 次）：%s" % (tries, last))


def api(method, path, payload=None, token=None, tries=4, raw=False):
    """调用 REST API，带退避重试。raw=True 时返回 (数据, 响应头)。"""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(
                API + path, data=data, method=method,
                headers={"Authorization": "Bearer " + token,
                         "Accept": "application/vnd.github+json",
                         "User-Agent": UA,
                         "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read().decode("utf-8")
                parsed = json.loads(body) if body else {}
                return (parsed, r.headers) if raw else parsed
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            if e.code in (401, 403, 404, 409, 422):
                raise ApiError(e.code, path, detail)
            last = e
        except Exception as exc:                       # 网络类错误才重试
            last = exc
        time.sleep(2 * (i + 1))
    raise SystemExit("请求失败 %s：%s" % (path, last))


def git(repo_dir, *args, binary=False, check=True):
    r = subprocess.run(["git", "-C", repo_dir, *args], capture_output=True)
    if check and r.returncode != 0:
        raise SystemExit("git %s 失败：%s" % (" ".join(args),
                                             r.stderr.decode("utf-8", "replace")[:300]))
    return r.stdout if binary else r.stdout.decode("utf-8", "replace").strip()


def probe(url, timeout=15):
    """探测可达性，返回 HTTP 状态码；连不上返回 0。"""
    for method in ("HEAD", "GET"):
        try:
            req = urllib.request.Request(url, method=method, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code
        except Exception:
            continue
    return 0


def parse_github_url(url):
    """从远端 URL 解析 (owner, repo)。"""
    m = re.search(r"github\.com[:/]+([^/]+)/([^/\s]+?)(?:\.git)?$", url.strip())
    if not m:
        raise SystemExit("无法从远端 URL 解析 owner/repo：%s" % url)
    return m.group(1), m.group(2)


def parse_ident(line):
    """解析 'author Name <email> 1700000000 +0800' 为 API 需要的 author 对象。"""
    m = re.match(r"^(?:author|committer)\s+(.*?)\s*<([^>]*)>\s+(\d+)\s+([+-]\d{4})$", line)
    if not m:
        raise SystemExit("无法解析身份行：%s" % line)
    name, email, epoch, tz = m.group(1), m.group(2), int(m.group(3)), m.group(4)
    sign = 1 if tz[0] == "+" else -1
    off = sign * (int(tz[1:3]) * 60 + int(tz[3:5]))
    dt = datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc) + datetime.timedelta(minutes=off)
    iso = dt.strftime("%Y-%m-%dT%H:%M:%S") + "%s:%s" % (tz[:3], tz[3:])
    return {"name": name, "email": email, "date": iso}


def local_commit_meta(repo_dir, ref="HEAD"):
    """从本地 commit 对象提取原样元数据，用于重建出相同 SHA。"""
    raw = git(repo_dir, "cat-file", "commit", ref, binary=True)
    headers, message = raw.split(b"\n\n", 1)
    text = headers.decode("utf-8", "replace")
    meta = {"message": message.decode("utf-8")}
    for line in text.splitlines():
        if line.startswith("tree "):
            meta["tree"] = line[5:].strip()
        elif line.startswith("parent "):
            meta.setdefault("parents", []).append(line[7:].strip())
        elif line.startswith("author "):
            meta["author"] = parse_ident(line)
        elif line.startswith("committer "):
            meta["committer"] = parse_ident(line)
    meta.setdefault("parents", [])
    return meta


def all_tree_entries(repo_dir, ref="HEAD"):
    """列出 <ref> 下全部文件的 (path -> (mode, sha))。"""
    out = git(repo_dir, "ls-tree", "-r", ref)
    entries = {}
    for line in out.splitlines():
        if not line.strip():
            continue
        left, path = line.split("\t", 1)
        mode, _type, sha = left.split()
        entries[path] = (mode, sha)
    return entries


# --------------------------------------------------------------------------- #
# 子命令
# --------------------------------------------------------------------------- #
def cmd_check(args):
    print("=== 1. 凭据 ===")
    token = get_token()
    print("令牌前缀 %s...  长度 %d（内容不打印）" % (token[:4], len(token)))
    user, headers = api("GET", "/user", token=token, raw=True)
    scopes = headers.get("x-oauth-scopes")
    print("账号 %s (id %s)" % (user.get("login"), user.get("id")))
    print("权限范围 %s" % (scopes if scopes is not None else "未提供该响应头（可能为细粒度令牌）"))
    can_create = scopes is None or "repo" in scopes
    print("可建仓 %s" % ("是" if can_create else "否（缺少 repo 权限）"))

    print()
    print("=== 2. 通道探测 ===")
    targets = ["https://github.com", "https://api.github.com", "https://codeload.github.com"]
    codes = {}
    for u in targets:
        codes[u] = probe(u)
        print("  %-32s -> %s" % (u, codes[u] or "000（不可达）"))
    gh_ok = codes["https://github.com"] not in (0,)
    api_ok = codes["https://api.github.com"] == 200
    print()
    if gh_ok:
        print("结论：通道 A（常规 git push）可用；通道 B 作为兜底。")
    elif api_ok:
        print("结论：通道 A 不可用（github.com 不通），改用通道 B（Git Data API）。")
    else:
        print("结论：两条通道均不可用，需检查代理设置。")


AUDIT_PATTERNS = [
    (r"ghp_[A-Za-z0-9]{20,}", "GitHub 经典令牌"),
    (r"gho_[A-Za-z0-9]{20,}", "GitHub OAuth 令牌"),
    (r"github_pat_[A-Za-z0-9_]{20,}", "GitHub 细粒度令牌"),
    (r"Bearer\s+[A-Za-z0-9_\-\.]{20,}", "Bearer 凭据串"),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "私钥"),
    (r"C:\\\\Users\\\\[^\\\\\s<]+", "本机用户绝对路径"),
    (r"C:/Users/[^/\s<]+", "本机用户绝对路径"),
    (r"/Users/[^/\s<]+/", "macOS 用户路径"),
    (r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", "邮箱地址"),
]

TEXT_EXT = {".md", ".txt", ".py", ".js", ".ts", ".json", ".yaml", ".yml", ".toml",
            ".sh", ".ps1", ".bat", ".cfg", ".ini", ".html", ".css", ".xml", ".sql", ".rs", ".go"}


def cmd_audit(args):
    root = os.path.abspath(args.directory)
    if not os.path.isdir(root):
        raise SystemExit("不是目录：%s" % root)
    extra = [(p, "自定义规则") for p in (args.pattern or [])]
    hits = []
    scanned = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".git", "node_modules", "__pycache__")]
        for fn in filenames:
            path = os.path.join(dirpath, fn)
            if os.path.splitext(fn)[1].lower() not in TEXT_EXT:
                continue
            scanned += 1
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    for lineno, line in enumerate(f, 1):
                        for pattern, label in AUDIT_PATTERNS + extra:
                            m = re.search(pattern, line)
                            if m:
                                hits.append((os.path.relpath(path, root), lineno, label, m.group(0)[:60]))
            except OSError:
                continue

    print("=== 内容审计 ===")
    print("扫描目录：%s" % root)
    print("文本文件：%d 个" % scanned)
    print()
    if not hits:
        print("未发现敏感内容。")
        return
    print("发现 %d 处命中（请逐条确认是否为需脱敏的真实数据）：" % len(hits))
    for rel, lineno, label, sample in hits:
        print("  %-40s :%-5d [%s] %s" % (rel, lineno, label, sample))
    print()
    print("提示：本机固有的占位写法（如 <user>）不会命中；命中项需人工判断，不要无脑放行。")


def cmd_create(args):
    token = get_token()
    topics = [t.strip().lower() for t in (args.topics or "").split(",") if t.strip()]
    payload = {"name": args.name, "description": args.desc or "",
               "private": bool(args.private), "has_issues": True,
               "has_wiki": False, "has_projects": False, "auto_init": False}
    try:
        repo = api("POST", "/user/repos", payload, token=token)
        print("创建成功：%s" % repo["html_url"])
    except ApiError as e:
        if e.code != 422:
            raise
        # 同名仓库已存在属正常，继续配置 topics
        print("仓库已存在（HTTP 422），跳过创建，继续后续步骤。")
        repo = None
    if topics:
        login = api("GET", "/user", token=token)["login"]
        name = args.name.split("/")[-1]
        try:
            r = api("PUT", "/repos/%s/%s/topics" % (login, name),
                    {"names": topics}, token=token)
            print("topics 已设置：%s" % ", ".join(r.get("names", [])))
        except ApiError as e:
            print("topics 设置失败：%s" % e)
    if repo:
        print("可见性：%s" % repo.get("visibility"))
        print("克隆地址：%s" % repo.get("clone_url"))


def cmd_push(args):
    repo_dir = os.path.abspath(args.repo_dir)
    remote_url = git(repo_dir, "remote", "get-url", args.remote)
    owner, repo = parse_github_url(remote_url)
    token = get_token()

    head = git(repo_dir, "rev-parse", "HEAD")
    meta = local_commit_meta(repo_dir)
    print("本地 HEAD : %s" % head)
    print("远端仓库  : %s/%s" % (owner, repo))

    remote_head = None
    try:
        remote_head = api("GET", "/repos/%s/%s/git/ref/heads/%s" % (owner, repo, args.branch),
                          token=token)["object"]["sha"]
    except ApiError as e:
        # 分支不存在时 GitHub 返回 404；但**空仓库**（尚无任何提交）返回的是
        # 409 "Git Repository is empty."——两者语义相同，都按"待创建"处理。
        # 少了 409 这一支，首次推送到新建仓库会直接失败。
        if e.code not in (404, 409):
            raise
        print("远端分支 %s 尚不存在（HTTP %d），将创建。" % (args.branch, e.code))
    print("远端 HEAD : %s" % (remote_head or "(无)"))

    if remote_head == head:
        print("远端与本地已一致，无需推送。")
        return

    # 安全闸：远端必须是本地父提交，否则可能覆盖他人提交
    if remote_head and remote_head not in meta["parents"]:
        raise SystemExit("远端 HEAD 不是本地 HEAD 的父提交（%s）。"
                         "可能有他人提交或本地历史被改写，已中止以免覆盖。"
                         % ", ".join(p[:7] for p in meta["parents"]))

    # 选择通道
    upload_url = "https://github.com/%s/%s.git/info/refs?service=git-receive-pack" % (owner, repo)
    code = probe(upload_url)
    print()
    print("=== 通道探测 ===")
    print("  %s -> %s" % (upload_url, code or "000（不可达）"))
    print("  指定通道：%s" % args.channel)

    if args.channel == "api":
        print("  按指定走通道 B：Git Data API")
    elif code not in (0, 404):
        print("  选用通道 A：常规 git push")
        r = subprocess.run(["git", "-C", repo_dir, "push", args.remote,
                            "HEAD:refs/heads/%s" % args.branch],
                           capture_output=True, text=True,
                           env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
        if r.returncode == 0:
            print("  推送成功。")
            return
        print("  通道 A 失败：%s" % (r.stderr or r.stdout).strip()[:300])
        print("  自动降级到通道 B。")
    else:
        print("  通道 A 不可达，选用通道 B：Git Data API")

    _push_via_api(repo_dir, owner, repo, args.branch, head, meta, remote_head, token,
                  dry_run=args.dry_run)


def _push_via_api(repo_dir, owner, repo, branch, head, meta, remote_head, token,
                  dry_run=False):
    local_entries = all_tree_entries(repo_dir, head)

    print()
    print("=== 上传 blob ===")
    if remote_head:
        base_tree = api("GET", "/repos/%s/%s/git/commits/%s" % (owner, repo, remote_head),
                        token=token)["tree"]["sha"]
        base_entries = all_tree_entries(repo_dir, remote_head)
        changed = sorted({p for p, v in local_entries.items() if base_entries.get(p) != v}
                         | {p for p in base_entries if p not in local_entries})
        print("变更文件 %d 个" % len(changed))
    else:
        base_tree = None
        base_entries = {}
        changed = sorted(local_entries)
        print("新建分支，全部 %d 个文件" % len(changed))

    tree_items = []
    for path in changed:
        if path not in local_entries:                  # 删除
            tree_items.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
            print("  - %-40s 删除" % path)
            continue
        mode, local_sha = local_entries[path]
        content = git(repo_dir, "cat-file", "blob", "%s:%s" % (head, path), binary=True)
        r = api("POST", "/repos/%s/%s/git/blobs" % (owner, repo),
                {"content": base64.b64encode(content).decode("ascii"), "encoding": "base64"},
                token=token)
        ok = r["sha"] == local_sha
        print("  %s %-40s %s" % ("+" if ok else "!", path, "OK" if ok else "SHA 不一致，中止"))
        if not ok:
            raise SystemExit("blob 内容在校验中被改写，已中止。")
        tree_items.append({"path": path, "mode": mode, "type": "blob", "sha": r["sha"]})

    print()
    print("=== 构造 tree ===")
    payload = {"tree": tree_items}
    if base_tree:
        payload["base_tree"] = base_tree
    new_tree = api("POST", "/repos/%s/%s/git/trees" % (owner, repo), payload, token=token)["sha"]
    same_tree = new_tree == meta["tree"]
    print("新 tree %s / 本地 tree %s  %s"
          % (new_tree[:12], meta["tree"][:12], "一致" if same_tree else "不同"))

    print()
    print("=== 构造 commit ===")
    commit_payload = {"message": meta["message"], "tree": new_tree,
                      "parents": [remote_head] if remote_head else [],
                      "author": meta["author"], "committer": meta["committer"]}
    new_commit = api("POST", "/repos/%s/%s/git/commits" % (owner, repo),
                     commit_payload, token=token)["sha"]
    print("新 commit %s / 本地 commit %s  %s"
          % (new_commit[:12], head[:12], "完全一致，无分叉" if new_commit == head else "SHA 不同"))

    print()
    print("=== 更新分支引用 ===")
    if dry_run:
        print("  [dry-run] 已跳过写引用。正式执行时将把 %s 指向 %s"
              % (branch, new_commit[:12]))
        print("  [dry-run] 远端分支未发生任何改动。")
        return
    if remote_head:
        api("PATCH", "/repos/%s/%s/git/refs/heads/%s" % (owner, repo, branch),
            {"sha": new_commit, "force": False}, token=token)
    else:
        api("POST", "/repos/%s/%s/git/refs" % (owner, repo),
            {"ref": "refs/heads/%s" % branch, "sha": new_commit}, token=token)
    now = api("GET", "/repos/%s/%s/git/ref/heads/%s" % (owner, repo, branch),
              token=token)["object"]["sha"]
    print("远端 HEAD 现已为 %s" % now)
    print("结论：%s" % ("成功，本地与远端 SHA 一致" if now == head else "已推送但与本地不一致，请复核"))


def cmd_verify(args):
    repo_dir = os.path.abspath(args.repo_dir)
    token = get_token()
    if args.owner and args.repo:
        owner, repo = args.owner, args.repo
    else:
        owner, repo = parse_github_url(git(repo_dir, "remote", "get-url", "origin"))

    local_head = git(repo_dir, "rev-parse", "HEAD")
    try:
        remote_head = api("GET", "/repos/%s/%s/git/ref/heads/%s" % (owner, repo, args.branch),
                          token=token)["object"]["sha"]
    except ApiError as e:
        if e.code not in (404, 409):
            raise
        raise SystemExit("远端分支 %s 不存在（HTTP %d），无可比对内容——"
                         "仓库可能为空、尚未推送，或分支名不符。" % (args.branch, e.code))
    print("=== 提交比对 ===")
    print("本地 HEAD : %s" % local_head)
    print("远端 HEAD : %s" % remote_head)
    print("一致      : %s" % ("是" if local_head == remote_head else "否"))

    tree = api("GET", "/repos/%s/%s/git/trees/%s?recursive=1" % (owner, repo, args.branch),
               token=token)
    remote_blobs = {i["path"]: i["sha"] for i in tree["tree"] if i["type"] == "blob"}
    local_blobs = {p: s for p, (_m, s) in all_tree_entries(repo_dir, "HEAD").items()}

    print()
    print("=== 文件比对 ===")
    print("%-42s %-14s %-14s %s" % ("路径", "远端", "本地", "结果"))
    print("-" * 86)
    ok = True
    for path in sorted(set(remote_blobs) | set(local_blobs)):
        r, l = remote_blobs.get(path), local_blobs.get(path)
        same = r == l
        ok = ok and same
        print("%-42s %-14s %-14s %s"
              % (path, (r or "-")[:12], (l or "-")[:12], "一致" if same else "不一致"))
    print("-" * 86)
    print("文件数：远端 %d / 本地 %d" % (len(remote_blobs), len(local_blobs)))
    print("结论：%s" % ("全部一致" if ok else "存在差异"))

    info = api("GET", "/repos/%s/%s" % (owner, repo), token=token)
    print()
    print("=== 仓库状态 ===")
    print("可见性 : %s" % info.get("visibility"))
    print("描述   : %s" % (info.get("description") or ""))
    print("许可证 : %s" % ((info.get("license") or {}).get("spdx_id") or "未识别"))
    print("topics : %s" % ", ".join(info.get("topics", [])))
    print("网页   : %s" % info.get("html_url"))


# --------------------------------------------------------------------------- #
def main():
    p = argparse.ArgumentParser(description="GitHub 仓库发布工具")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check", help="探测凭据与网络通道")

    a = sub.add_parser("audit", help="发布前内容审计")
    a.add_argument("directory")
    a.add_argument("--pattern", action="append", help="附加正则，可重复")

    c = sub.add_parser("create", help="创建仓库并设置 topics")
    c.add_argument("name", help="仓库名，或 owner/name")
    c.add_argument("--desc", default="")
    c.add_argument("--private", action="store_true")
    c.add_argument("--topics", default="", help="逗号分隔，小写字母/数字/连字符")

    u = sub.add_parser("push", help="推送当前 HEAD（自动选通道）")
    u.add_argument("repo_dir")
    u.add_argument("--branch", default="main")
    u.add_argument("--remote", default="origin")
    u.add_argument("--channel", choices=["auto", "push", "api"], default="auto",
                   help="auto=按探测结果选（默认）；push=强制常规 git push；api=强制 Git Data API")
    u.add_argument("--dry-run", action="store_true",
                   help="走完通道 B 的全部构造与校验，但不更新分支引用（远端不产生可见改动）")

    v = sub.add_parser("verify", help="服务端校验")
    v.add_argument("repo_dir")
    v.add_argument("--owner")
    v.add_argument("--repo")
    v.add_argument("--branch", default="main")

    args = p.parse_args()
    {"check": cmd_check, "audit": cmd_audit, "create": cmd_create,
     "push": cmd_push, "verify": cmd_verify}[args.cmd](args)


if __name__ == "__main__":
    try:
        main()
    except ApiError as exc:
        raise SystemExit(str(exc))
