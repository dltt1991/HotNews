# 飞书热点订阅 Agent

群成员 `@机器人` 创建多条共享订阅，由本机 Codex 搜索国内外资讯、生成中文摘要并定时推送。常驻 Python 服务只负责飞书回调、本机管理页和出站发送；一个每 5 分钟运行的 Codex Scheduled Task 负责理解消息和研究资讯，复用 Codex 的模型与网页搜索，不调用 OpenAI API。

默认每天北京时间 09:00；也支持至少 5 分钟的间隔计划。最近 24 小时内容不足时依次查找 7 天、30 天，最多 10 条，旧内容注明日期与“历史补充”。没有合格未推送内容时保持安静。处理延迟通常为 0–5 分钟；机器离线后只补执行一次。

## 本机运行（推荐）

需要 Python 3.8+、`cryptography>=2.8`，以及保持在线的电脑和 Codex 桌面应用。在项目根目录执行：

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e .
cp config.example.json config.json
```

配置只包含数据库路径、两个监听地址和运行限制。密钥仅从常驻服务进程的环境变量加载：

| 变量 | 来源 |
| --- | --- |
| `FEISHU_APP_ID` | 飞书自建应用 App ID |
| `FEISHU_APP_SECRET` | 同一应用的 App Secret |
| `FEISHU_VERIFICATION_TOKEN` | 事件订阅 Verification Token |
| `FEISHU_ENCRYPT_KEY` | 可选；启用加密回调时配置的 Encrypt Key |

通过系统服务的受限环境文件或当前终端设置这些值，避免把真实密钥写入项目配置、命令示例或 Codex prompt；Codex 的研究进程不需要这些变量。启动时程序迁移数据库并自动查询机器人的 open ID：

```bash
hotnews --config config.json serve
# 未安装 console script 时的等价命令
PYTHONPATH=src python3 -m hotnews.cli --config config.json serve
```

默认回调监听 `127.0.0.1:8080`，本机管理页为 `http://127.0.0.1:8081/`。`GET /healthz` 返回基本存活状态。两个端口必须不同，管理地址必须为 `127.0.0.1`。SIGINT/SIGTERM 会关闭回调、管理服务和出站工作器；任一后台服务异常会终止整体服务并返回非零退出码。可由操作系统服务管理器自动重启。

管理页可查看、筛选、编辑、暂停/恢复、立即推送或取消订阅。创建入口只在飞书群。关键词修改会立即清除旧扩展词并显示“等待 Codex 更新搜索词”；下一轮任务更新后恢复可研究状态。编辑不自动推送；暂停后立即推送不恢复定时计划。页面使用 version 防止两标签页互相覆盖，冲突时要求刷新。

## 飞书应用与公网回调

在飞书开放平台创建企业自建应用，启用机器人能力，申请 `im:message.group_at_msg`（接收群聊中 @机器人 消息）及 `im:message:send_as_bot`（以应用身份发消息）；控制台若使用兼容的 `im:message` 权限，按[发送消息文档](https://open.feishu.cn/document/server-docs/im-v1/message/create)确认所需授权。订阅 `im.message.receive_v1`，发布并安装应用，将机器人加入测试群。接收范围说明见[飞书权限说明](https://open.feishu.cn/solutions/detail/ticket?lang=zh-CN)。

将事件订阅请求地址设为 `https://你的回调域名/callbacks/feishu`。启用加密时保持后台 Encrypt Key 与服务环境一致；配置 Verification Token 后由程序响应 URL verification。

公网 HTTPS 使用 Cloudflare Tunnel 或反向代理，只转发回调监听端口 8080。Tunnel 的该域名映射到 `http://127.0.0.1:8080`，其他规则设为 404，不能指向 8081。Nginx 的核心路由可写为：

```nginx
location = /callbacks/feishu {
    proxy_pass http://127.0.0.1:8080;
    client_max_body_size 1m;
    proxy_read_timeout 15s;
}
location / { return 404; }
```

TLS、域名及 Tunnel 本身由操作者配置。即使使用反向代理，管理页仍只在本机访问，不能合并到公网域名下。

群内任何成员可使用：

```text
@机器人 订阅 AI Agent 和大模型，每天早上 9 点
@机器人 订阅新能源汽车，每隔 2 小时
@机器人 查看订阅
@机器人 立即推送 1
@机器人 取消订阅 2
@机器人 帮助
```

只接收明确 @当前机器人 的群文本消息；私聊、普通群消息和机器人发送的消息会被忽略。订阅编号以群为界、取消后不复用。

## 配置一个 Codex Scheduled Task

先在桌面应用中打开这个本地项目，并在普通 Codex 对话请求“使用 `$hotnews-agent` dry-run 预览到期订阅”，确认网页搜索、项目技能和 CLI 权限可用。

在桌面应用 Scheduled 页面创建一个独立任务：选择这个项目、选择 **Local 本地项目目录**，将 [automations/hotnews-agent-prompt.md](automations/hotnews-agent-prompt.md) 全文作为 prompt，周期设为每 5 分钟。高级自定义周期可使用 `RRULE:FREQ=MINUTELY;INTERVAL=5`，保存后检查 UI 的下一次运行时间是否每 5 分钟递增；账户可用功能和周期限制以实际应用为准。

必须使用与常驻服务相同的 `config.json` 和 SQLite 文件；不要选择会创建独立数据库的后台 worktree。桌面任务需保持电脑及应用运行、本地项目仍在磁盘上。网页版任务和 CLI 本身不提供这个本地定时管理入口。[OpenAI 官方 Scheduled Tasks 文档](https://learn.chatgpt.com/docs/automations?surface=app)说明了 Local 模式、技能调用、在线要求和自定义周期。

技能位于 [.agents/skills/hotnews-agent/SKILL.md](.agents/skills/hotnews-agent/SKILL.md)，被任务显式调用。每轮默认最多处理 20 条事件、3 条到期订阅和 3 条扩展词更新，4 分钟软预算、15 分钟工作租约。默认出站最多尝试 5 次，退避从 10 秒倍增、上限 5 分钟；飞书明确返回更长等待时间时遵守该时间。失败的单条消息不阻塞其他订阅；连续失败达到 3 次提醒一次，成功后清零。

创建任务后观察首轮日志与 Scheduled 运行摘要；此仓库只提供技能和 prompt，不会自动修改你的账户或创建线上任务。

## Dry-run 卡片预览

Codex 的 dry-run 用只读 `agent list-due` 和 `agent history` 获取上下文，再执行真实搜索。最终把选好的资讯交给下方渲染接口；该接口本身不搜索，不读取数据库或凭据，也不发送/领取工作/记录成功历史：

```bash
PYTHONPATH=src python3 -m hotnews.cli dry-run <<'JSON'
{"subscription":{"display_number":1,"topic":"人工智能","keywords":["人工智能"]},"search_window_days":30,"results":[]}
JSON
```

返回 `{ "card": null, "result_count": 0, "search_window_days": 30 }`；有结果时 card 是实际 `render_digest` 渲染的飞书卡片。将 results 换成已核实的资讯数组，最多 10 条，每条须有 `title/url/source/published_at/summary/event_key`，可选最多两条 `references`。日期必须带明确时区且在声明的 1/7/30 天窗口内，摘要为 2–3 句中文；渲染接口会验证 schema、窗口和 URL，并做 URL/事件去重。

## 备份与恢复

SQLite 默认位于 `data/hotnews.db`，采用 WAL。在线备份用 SQLite backup API，避免只复制主文件而漏掉 WAL；例如有 sqlite3 命令行时：

```bash
mkdir -p backups
sqlite3 data/hotnews.db ".backup 'backups/hotnews.db'"
```

备份保留订阅、文章投递历史、队列与租约；另保留非秘密配置。恢复时先暂停 Scheduled Task、停止常驻服务，保留当前数据库及 `-wal/-shm` 文件到独立目录，再将备份放回配置的数据库路径，并确保不存在旧数据库遗留的 WAL/SHM。重新启动服务会执行前向迁移，恢复 Scheduled Task 后未完成工作在租约过期（默认 15 分钟）后可重领；已确认历史仍去重。只有飞书 API 明确成功且返回 message ID 后才记入已推送记录。

此版本是新的 schema，不直接读取旧原型数据库；上线应使用新数据库路径。开发阶段已经执行过旧的未发布 v1/v2 迁移的数据库可能缺少后来增加的字段，请保留原文件并使用新库，或由维护者编写明确的前向迁移。

## 测试与上线验收

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 -m compileall -q src tests
PYTHONPATH=src python3 -m hotnews.cli --help
```

自动化覆盖回调鉴权/幂等、共享订阅、调度、租约、版本冲突、搜索结果验证、消息重试与原子确认、本机 API 和页面行为。外部 HTTP 被替换，不联系真实飞书或搜索网站。真实浏览器布局、测试群收发和无人值守 Codex 调度仍需操作者验收，尚未以自动测试证明。

首次上线按[设计规格 §12.2](docs/superpowers/specs/2026-10-02-feishu-hotnews-agent-design.md)逐项验收：两条订阅、另一成员立即执行、国内外来源/日期/历史标记、事件重放、重启恢复、取消不影响其他订阅、发送失败重试、dry-run 不写成功历史、后台关键词/时间修改与词更新、暂停/恢复、两标签页 409，以及公网域名无法访问管理页。没有真实凭据的开发环境不会将这些手工项目标为通过。

## 容器可选部署

优先本机运行以使用管理页；容器默认只发布回调。复制配置后将 `callback.host` 改为 `0.0.0.0`（容器内部），仍保持 `admin.host` 为 `127.0.0.1`：

```bash
docker compose up -d --build
```

Compose 从运行环境读取四个飞书变量，将 `./data` 持久化到 `/app/data`，仅映射宿主机 `127.0.0.1:8080`。Dockerfile 仅 EXPOSE 8080，8081 不发布；容器内的管理页不会直接出现在宿主机浏览器。Codex 仍运行在宿主机本地项目，必须读取同一个挂载目录下的数据库。公网 Tunnel/反向代理只连接宿主机回调端口。
