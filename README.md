# github-repo-publish

> 把一个本地目录发布（或更新）为 GitHub 仓库的 Agent 技能：发布前内容审计 → 凭据探测 → **双传输通道自动选择** → 推送 → **服务端实况校验**。

**What is this?** An [Agent Skill](https://docs.claude.com/en/docs/claude-code/skills) for publishing local directories to GitHub from a restricted network environment — no `gh` CLI, no manual steps — plus a zero-fork fallback path when `github.com` itself is unreachable.

---

## 为什么需要它

在受限网络里发布代码，会遇到三个具体障碍：

| 障碍 | 常规做法为什么不行 |
|---|---|
| **`gh` CLI 未安装** | 官方推荐的 `gh repo create` 直接用不了 |
| **`github.com` 可能不可达，而 `api.github.com` 仍正常** | 两者走不同代理出口，`git push` 报 `CONNECT tunnel failed, response 502` 或 `Empty reply from server`，但 API 调用完全通畅 |
| **发布前不做审计** | 开源不可逆，公开仓库会被抓取索引，事后删除也留痕 |

本技能把这三点都处理掉：用 Git 凭据管理器里已缓存的令牌走 REST API 完成建仓，用两条可切换的推送通道绕开网络中断，并在推送后用**服务端实况**（而不是本地状态）校验结果。

## 核心：双通道推送

```
探测 github.com 可达性
├── 返回任何非 0 状态码（200 / 301 / 405 均算）→ 通道 A：常规 git push
└── 返回 000，但 api.github.com 返回 200     → 通道 B：Git Data API
```

> **判据是"服务端有没有应答"，不是"是不是 200"。** `git-receive-pack` 的 `info/refs` 端点只接受 POST，用 GET/HEAD 探测时正常返回 **405**——这恰恰说明服务端是通的。实测可达时为 405、故障时为 000。

**通道 B 的关键性质是「零分叉」**：Git 的 blob / tree / commit 都是**内容寻址**，只要 tree 内容、父提交、提交信息、作者与提交者元数据完全一致，经 API 重建出的 commit SHA 与本地**完全相同**，因此不会在本地与远端之间制造分叉。顺序为：确认远端 HEAD = 本地父提交 → 逐个上传 blob 并校验 SHA → 基于 `base_tree` 构造 tree → 构造 commit（含完整 author/committer 元数据）→ 更新引用。

## 内容结构

```
.
├── SKILL.md                          # 主规范：审计 → 凭据 → 通道 → 校验 → 常见坑
├── references/
│   └── sandbox-git-pitfalls.md       # 本机网络与 Git 的实测坑清单、判定方法与原始报错
└── scripts/
    └── gh_repo.py                    # 子命令工具：check / audit / create / push / verify
```

## 工具用法

`scripts/gh_repo.py` 是纯标准库实现，无第三方依赖：

```bash
# 1. 凭据与通道探测（含账号、权限范围、通道可达性结论）
python scripts/gh_repo.py check

# 2. 发布前内容审计
python scripts/gh_repo.py audit <目录>

# 3. 建仓（已存在时返回 422，跳过继续即可）
python scripts/gh_repo.py create --name <仓库名> --description "<描述>" --public

# 4. 推送当前 HEAD（auto = 按探测结果选通道）
python scripts/gh_repo.py push <仓库目录>
python scripts/gh_repo.py push <仓库目录> --channel api   # 强制走 Git Data API
python scripts/gh_repo.py push <仓库目录> --dry-run       # 只演练，不落地

# 5. 服务端逐文件校验
python scripts/gh_repo.py verify <仓库目录>
```

## 若干反直觉的实测结论

- **不要用本地 `git status` 判断是否推送成功。** 某些环境下 remote-tracking 引用会回滚，出现「本地显示已同步、实际没推上去」或「显示 ahead N、其实早已一致」的假象。**一致性只认服务端实况**：`GET /git/ref/heads/<branch>` 比对 HEAD，`GET /git/trees/<branch>?recursive=1` 逐路径比对 blob SHA。
- **`git-receive-pack` 返回 405 是正常且健康的信号**，不是错误。
- **网络中断通常只在分钟量级**，不必因为一次 `CONNECT tunnel failed` 就改方案；「稍后重试」与「改走 API」两条路都成立。
- **提交信息一律用文件传入**（`git commit -F <文件>`）：内联在命令行里的长文本可能触发安全过滤器**整条拦截**，报错理由还会指向一个与语义完全无关的方向。
- **造测试提交时必须回读 `git rev-parse HEAD` 确认 SHA 真的变了**——测试文件名若被仓库自己的 `.gitignore` 命中，`git add` 会静默无效，随后 `git commit` 报 `nothing to commit`，而所有命令都"成功"，测试其实是空的。
- **凭据调用需传完整环境变量**：用 `git credential fill` 取令牌时若裁剪了 `os.environ`，凭据管理器会找不到凭据库并报 `could not read Username`。令牌只在内存与管道中使用，不落盘、不打印。
- **建仓用 `POST /user/repos` 且带 `auto_init: false`**，便于随后直接推送本地已有历史。
- **topics 需单独设置**：`PUT /repos/{owner}/{repo}/topics`，名称只能是小写字母、数字、连字符。

## 使用方式

**用户级安装**（跨项目可用）：

```bash
git clone https://github.com/ztc1522021381/github-repo-publish.git
mkdir -p ~/.workbuddy/skills
mv github-repo-publish ~/.workbuddy/skills/
```

**项目级安装**（随项目共享）：

```bash
git clone https://github.com/ztc1522021381/github-repo-publish.git
mkdir -p <你的项目>/.workbuddy/skills
cp -r github-repo-publish <你的项目>/.workbuddy/skills/
```

> 技能目录名需为 `github-repo-publish`，与 `SKILL.md` 中的 `name` 字段一致。

## 适用范围与免责声明

**适用范围**：本技能记录的是一组**特定环境**下的实测观察（Windows + 沙箱化 Agent 运行时 + 系统代理），其中部分限制来自沙箱安全策略而非 Git 或 GitHub 本身，在**其他环境中未必成立**。

「零分叉推送」这一性质本身是 Git 对象内容寻址的必然结果，与环境无关；但通道可达性的具体表现会随网络环境变化，**请先运行 `check` 探测，再决定通道**，不要照搬结论。

脚本只与 GitHub 官方 API 和本机 Git 凭据管理器交互，不上传凭据、不访问第三方服务。

## License

[MIT](LICENSE)
