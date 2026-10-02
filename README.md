# HotNews Agent

一个可自行部署的热点信息订阅 Agent。它从 RSS/Atom 与 Hacker News 采集内容，按关键词、排除词和热度筛选，用 SQLite 做逐群去重，并按计划发送到飞书群或企业微信群。

> “微信群”当前指企业微信群机器人。普通个人微信群没有稳定、合规的官方机器人 Webhook，不建议依赖模拟登录方案。

## 快速开始

需要 Python 3.8+，运行时无第三方依赖。

```bash
cp config.example.json config.json
export FEISHU_WEBHOOK='https://open.feishu.cn/open-apis/bot/v2/hook/...'
export FEISHU_SECRET='...'
export WECOM_WEBHOOK='https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...'

python3 -m venv .venv
. .venv/bin/activate
pip install -e .

# 先预览，不发送、不写入去重记录
hotnews --config config.json once --dry-run

# 真实发送一次
hotnews --config config.json once

# 持续执行定时任务
hotnews --config config.json serve
```

也可以执行 `cp config.example.json config.json && docker compose up -d --build`。

## 配置

- `sources[].type`：`rss`、`atom` 或 `hackernews`。
- `subscriptions[].sources`：订阅使用的数据源名称；省略代表全部。
- `keywords`：标题或摘要命中任意一个即入选；空数组代表全部。
- `exclude_keywords`：命中任意一个即排除。
- `min_score`：最低热度（目前 Hacker News 提供原始分数，RSS 默认为 0）。
- `schedule`：`{"type":"daily","at":"09:00"}` 或 `{"type":"interval","minutes":30}`。
- `channels`：支持 `feishu` 和 `wecom`；每个渠道应设置稳定且唯一的 `name`，用于独立去重。

环境变量占位符可以出现在配置任意字符串中。若飞书机器人没有开启“签名校验”，请直接删除 `secret` 字段，不能把它留成未定义的占位符。

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

生产部署建议把 `data/` 挂到持久化磁盘，并让容器时区与 `schedule.at` 的预期时区一致。任一数据源抓取失败不会阻止其他数据源；任一群发送失败也不会写入该群的去重记录，下次调度会重试。
