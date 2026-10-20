# 踩坑清单

按"症状 → 根因 → 修法"组织。**装之前通读一遍，能省掉几个通宵。**

标 ★★ 的是**会造成静默错误**的坑——不报错、看起来正常，但结果是错的。这类最危险。

---

## ★★ 1. 静默漏评：评论正常，但评的是旧代码

**症状**：MR 推了新版本，群里也收到通知，评论也有，但评的内容明显是上一版。改的东西它完全没提。

**根因**：去重逻辑取了 `patchsets` 列表的**最后一项**。

云效 `/diffs/patches` 返回的列表里，最后一项是 **Base**（`relatedMergeItemType = MERGE_TARGET`，`patchSetName = "Base"`）。而 Base 在**目标分支不动时恒定不变**。

于是：
1. 版本 1 评完，记下 Base 的 bizId
2. 之后任何新推送，算出来的还是同一个 Base
3. 判定"已评审过" → **静默跳过**

更糟的是：合并时云效合入的是**源分支当前内容**，所以"AI 说没问题"和"实际合进去的代码"完全是两码事。

**修法**：

```python
kind = str(p.get("relatedMergeItemType") or "").upper()
if kind and kind != "MERGE_SOURCE":
    continue                      # 跳过 Base / MERGE_TARGET
src.sort(key=lambda x: x[0])      # 按 versionNo 升序
latest = src[-1]                  # 最后一个才是最新版本
```

**自查**：`python -m src.cli check` 会打印每个 opened MR 的最新版本，和你在云效界面看到的一致才算对。

**实测字段**：`patchSetName`（版本N / Base）、`relatedMergeItemType`（MERGE_SOURCE | MERGE_TARGET）、`versionNo`、`patchSetBizId`、`createTime`、`ref`。

---

## ★★ 2. 改了 values 不生效（最容易怀疑人生）

**症状**：改了 `LLM_MAX_TOKENS` / `MAX_DIFF_CHARS`，`helm upgrade` 成功，Pod 也重启了，但参数还是旧的。`kubectl exec` 进去 `echo $LLM_MAX_TOKENS` 是原值。

**根因**：chart 里设了 `existingSecret`，于是**不渲染 `templates/secret.yaml`**。而这些调参项恰好只写在 Secret 里 → Secret 不渲染 → 值永远不更新。

**修法**：在 `deployment.yaml` 和 `cronjob.yaml` 的 `env:` 里**显式下发**（`env` 优先级高于 `envFrom`）：

```yaml
env:
  - name: LLM_MAX_TOKENS
    value: {{ .Values.llm.maxTokens | quote }}
  - name: MAX_DIFF_CHARS
    value: {{ .Values.options.maxDiffChars | quote }}
```

**本 chart 已经全部这么做了**。但你新增任何调参项时，一定要照抄这个模式，否则同样的坑会再来一次。

**自查**（部署后必跑）：

```bash
kubectl exec -n mr-review deploy/<release> -- \
  sh -c 'echo "tokens=$LLM_MAX_TOKENS diff=$MAX_DIFF_CHARS batch=$BATCH_REVIEW"'
```

---

## ★★ 3. push 事件里根本没有 MR 编号

**症状**：MR 建好之后又推了几个提交，这些提交从来没被评过，但 MR 照样能合并。

**根因**：云效的 push 事件 payload **只带 `ref` 和 `repository`，没有 `number` 也没有 MR 编号**。所以"从事件里直接拿到 MR"这条路根本不存在。

**修法**：走兜底对账——

```
push 事件
└─ opened_mrs(only_repo=repo)     # 扫这个仓库所有 opened MR
   └─ 谁的版本变了就评谁            # 靠 state.json 去重
```

**这不是 bug，是有意设计**，不要"优化"掉它。堵掉会导致新提交漏评却照样合并。

**另外必须检查**：云效 webhook 配置里 **一定要勾上 push 事件**。只勾合并请求事件的话，MR 建好后的新提交会全部漏评。

---

## ★ 4. 首次启动刷屏：所有历史 MR 被评一遍

**症状**：刚装完，群里瞬间弹出几十条评审通知。

**根因**：PVC 是空盘，`state.json` 为空，兜底逻辑扫到所有 opened MR，每个都判定"没评过"。

**修法**：开 cronjob 或接 webhook **之前**先数一下：

```bash
kubectl exec -n mr-review deploy/<release> -- python -m src.cli list | wc -l
```

如果 opened MR 很多，先手动挑几个跑，把 state 填上；或者临时把 `repos` 只配一个仓库试水。

---

## ★ 5. "没动静"≠"失败了"（最高频误判）

**症状**：提交 MR 后，评论和群消息都不来。等了 3 分钟没有任何反应，以为挂了。

**真相**：分批是**串行**的，每批约 110 秒。4 批就是 7~8 分钟。这期间**完全静默**——不发中间状态。

**怎么判断它是在跑还是真挂了**：看日志最后一行。

| 日志最后一行 | 含义 |
|---|---|
| `第 2/4 批：…` | **还在跑，等着** |
| `第 2/4 批失败` / `Traceback` | 真失败了 |
| 什么都没有 | 事件没进来，去查云效 webhook 配置 |

还有个判据：`state.json` 里没有这个 MR 的记录 = **还没跑完**（跑完才写）。

---

## ★ 6. 多副本 → 重复评论

**症状**：同一个 MR 出现两条一模一样的 AI 评论。

**根因**：`state.json` 在 RWO 云盘上，**跨节点不可共享**。两个副本各在各的节点上，互相看不到去重记录。

**修法**：`replicaCount: 1`，并且用 `strategy: Recreate`（本 chart 已设置）。

想真正多副本，得把状态换成 Redis 或数据库——但那已经不是"轻量自托管"了。

---

## ★ 7. 云效评论 65535 字符上限，超了整条发不出去

**症状**：大 MR 评审完，**一条评论都没有**，日志里发评论接口报错。

**根因**：云效评论 `content` 官方限制是 **1 ~ 65535 字符**。超了不是截断，是**整条失败**。

**修法**：按行切成多条评论，用 `【AI 评审 i/n】` 标记，**绝不丢内容**。见 `src/diffutil.py: split_comment`。

**实测体积参考**：4 批 / 40 条问题约 10k 字符，离上限还很远。真撞上限的通常是没分批的大 MR。

---

## ★ 8. 企微消息被截断，末尾的 MR 链接没了

**症状**：群里收到通知，但最后一行"详情：https://..." 不见了，点不进去。

**根因**：企微机器人 **text 上限 2048 字节**、**markdown 上限 4096 字节**（注意是字节，中文一个字 3 字节）。超了平台直接截断尾部。

**修法**：不能"尾部硬切"，必须**减少条数后重新渲染**：

```python
n = 5
while n >= 1:
    msg = build_text(mr, result, n)      # 只列前 n 条
    if len(msg.encode("utf-8")) <= 2048:
        return msg
    n -= 1
```

这样保证末尾的链接一定发得出去。见 `src/notify.py: fit`。

**实测**：60 条问题时 text 约 1690 字节、markdown 约 3776 字节，刚好卡在边界。所以 `max_issues` 默认给 5。

---

## 9. 输出撞 max_tokens → JSON 断在半路，整批白跑

**症状**：日志报 `无法解析模型返回的 JSON`，或者返回内容为空（`finish_reason=length`）。

**根因**：模型输出被截断，JSON 停在 `issues` 数组中间。推理型模型会先吃掉一部分预算在"思考"上，更容易撞。

**三层兜底**（本项目的做法）：
1. 先尝试**抢救**：从尾部往前找可闭合位置，把已完整的部分解析出来，标 `_salvaged`
2. 不行就**对半拆**（`MAX_SPLIT_DEPTH` 控制最多拆几层）
3. 再不行就当这批失败，但**必须在评论里写明"这批复未完成"**

**根因修法**：`LLM_MAX_TOKENS` 给足（131072 基本不会成为限制）。

---

## 10. 跨文件的问题它看不见（方案固有缺陷）

**症状**：改了 service 的接口，但鉴权在 gateway 的 filter 里。它说"没有权限问题"。

**根因**：分批是按文件切的，模型看一批的时候天然看不到别的文件。这不是模型能力问题，**是输入里没有那份信息**。

**修法（只能减轻）**：
1. 全局通道额外读"横切文件"全文
2. prompt 里明确告知缺口："本批只有 M 个文件，未包含：xxx"，并要求跨文件结论写成"需结合上下文确认"

实测：加了缺口声明后，模型会主动说"鉴权情况需结合上下文确认"，而不是硬下结论。**这一条的收益比调模型大得多。**

---

## 11. `maxDiffChars` 调小不会更安全（反直觉）

**直觉**：diff 太大模型看不完，所以调小一点更保险。

**实际**：调小只会把**文件切碎**，让模型看到半个文件，误判率反而上升。

**实测数据**（真实代码填充，不是重复文本）：

| 输入字符 | prompt_tokens | 结果 |
|---|---|---|
| 100,000 | 23,268 | 通过 |
| 600,000 | 139,373 | 通过 |
| 1,600,000 | 395,545 | 通过 |
| 2,400,000 | 593,299 | 通过 |

结论：**输入侧几乎没有约束**。`maxDiffChars` 决定的是"分几批"，不是"能看多少"。所以应该**给大**（默认 400000）。

真正会丢结果的是**输出侧**，而输出侧已经有三层兜底。

---

## 12. `project_id` ≠ `repositoryId`

**症状**：接口 404 或 500，但 token 明明是对的。

**根因**：同一个仓库在云效里有两个号。push 事件里的 `project_id`（比如 1000001）和 changeRequests/patchsets 用的 `repositoryId`（比如 1000002）**不是一个东西**。

**修法**：最稳的是**别猜数字**，直接用仓库全路径 URL 编码：

```
your-org/your-group/demo-service  →  your-org%2Fyour-group%2Fdemo-service
```

**用错不会给明确报错**，只会 404/500，极难排查。

---

## 13. 改 Secret 后 Pod 不会自动感知

**症状**：更新了 token，`kubectl get secret` 看到是新值，但服务还用旧的。

**根因**：`envFrom` 是**启动时注入**的，改 Secret 不影响运行中的 Pod。

**修法**：

```bash
kubectl rollout restart deploy/<release> -n mr-review
```

---

## 14. `kubectl create secret --from-literal` 会清空别的键

**症状**：只想更新 `CODEUP_TOKEN`，更新完 `LLM_API_KEY` 没了，服务起不来。

**根因**：`kubectl create secret ... | kubectl apply -f -` 这种写法会**整体替换** Secret，其它键全丢。

**修法**：先备份，再用 patch 只改一个键：

```bash
kubectl get secret <name> -n mr-review -o yaml > /tmp/bak.yaml
kubectl patch secret <name> -n mr-review --type=json \
  -p='[{"op":"replace","path":"/data/CODEUP_TOKEN","value":"'$(echo -n NEWTOKEN | base64 -w0)'"}]'
```

---

## 15. `kubectl exec` 里的 heredoc 不生效

**症状**：`kubectl exec ... -- python3 - <<'PY' ... PY` 静默无输出，也不报错。

**根因**：远程 shell 拿不到 stdin。

**修法**：用单行 `-c`：

```bash
kubectl exec -n mr-review deploy/<release> -- python -c 'import os;print(os.environ.get("CODEUP_ORG_ID"))'
```

---

## 16. 集群重建后拉不到镜像（私有仓库）

**症状**：`ImagePullBackOff`，`pull access denied ... insufficient_scope: authorization failed`。

**根因**：ACK 集群重建后，所有 `dockerconfigjson` 类型的 Secret **全部归零**。

**三步判定**：

```bash
kubectl get secret -A -o json | grep -c dockerconfigjson   # 0 就是没凭据
kubectl get pod -A | grep -i acr                            # 空就是没装免密组件
curl -s https://<仓库域名>/v2/<ns>/<img>/manifests/<tag>     # 401 = 私有仓库
```

**修法**：

```bash
kubectl create secret docker-registry acr-credential -n mr-review \
  --docker-server=<仓库域名> --docker-username=<用户> --docker-password=<密码>

helm upgrade --install mr-review . -n mr-review -f values-local.yaml \
  --set image.pullSecrets[0]=acr-credential
```

---

## 17. 已合并 / 已关闭的 MR 补评不了

**症状**：想给上周合掉的 MR 补一次评审，报"可能已合并或关闭"。

**根因**：代码只查 `state=opened` 的 MR，云效 API 里已合并的要另查。

**修法**：当前版本不支持。想补评只能自己先存 MR 列表，或者临时在云效里重开一个。

---

## 18. 提交评论不能行内，也读不回来

**症状**：分支评审的问题全堆在一条评论里，没有行内标注；而且发完想校验发没发成功，查不到。

**根因**：
- 提交评论（commit comment）**没有 `file_path` / `line_number` 概念**，云效就是这么设计的。行内评论只有 MR 才有。
- 也没有 List 接口：`GET /commits/{sha}/comments` 返回 404。

**修法**：接受它。问题写进正文，用"文件:行号"文字标注。发完去界面看：

```
https://codeup.aliyun.com/<org>/<repo>/commit/<sha>
```

---

## 快速排错表

| 现象 | 先查什么 |
|---|---|
| 群里没消息 | ① 日志末行是 `第 i/N 批：…` → **还在跑**；② 有没有 `收到事件，N 字节`（没有 = 云效没发过来） |
| 日志有两条"收到事件"但只评一次 | 正常。多副本都收到，靠 state 去重 |
| 评论内容像旧代码 | **见坑 1**，去重取了 Base |
| 改了配置不生效 | **见坑 2**，先确认参数真的进 Pod 了（`kubectl exec` 查） |
| 一条评论都没有 | 见坑 7（超 65535）或发评论接口 401 |
| `finish_reason=length` | 调大 `LLM_MAX_TOKENS`（或减少单批规模） |
| 评论写"后 N 批未送审" | 到了 `MAX_BATCHES`，调大或设 0（不限） |
| 两条重复评论 | 副本数 > 1 且没钉同一节点（见坑 6） |
| 群里消息末尾链接没了 | 企微字节上限（见坑 8），调小 `max_issues` |
