# AutoQQ Plugin Agent 工作说明

本文件适用于 `plugin/` 及其子目录。开始修改前先读上层 `../AGENTS.md`，再读本文件、
`CONTRACT.md` 和 `README.md`。当前用户指令优先；`qqBot.md` 是历史需求，不能据此把数据库、
事件判定或 QQ 凭据放回 Plugin。若从独立 Git 仓库克隆本项目而没有上层文档，以本文件和
`CONTRACT.md` 为准。

## 项目定位

Plugin 是 Hermes Gateway 加载的 QQ 业务插件。它在 `pre_gateway_dispatch` 中完成身份提取、
命令处理和聊天授权；在 QQBot adapter 可用后启动 delivery worker，领取 EventServer 的待发送
消息，并经 Hermes adapter 发送到订阅时记录的私聊或群聊（群聊 @ 订阅用户）。Plugin 不连接 MySQL、不采集事件源、不判断事件触发，
也不直接调用腾讯 QQ API。用户、权限、订阅、事件和 delivery 的事实来源是 EventServer。

`plugin/` 与 `eventserver/` 是两个独立 Git 仓库；本目录内的提交、测试和发布只覆盖 Plugin。
跨服务变更应核对双方版本化 HTTP 契约及契约测试，不要通过导入 EventServer 内部模型或共享
数据库实现联动。

## 先定位到这些文件

| 任务 | 主要文件 |
| --- | --- |
| Hermes 注册、生命周期、hook | `plugin.yaml`、`__init__.py`、`autoqq_business_plugin/plugin.py` |
| 可信身份与消息决策 | `identity.py`、`processor.py`、`models.py` |
| 命令策略与执行 | `config/commands.yaml`、`command_policy.py`、`commands.py` |
| 按需查询 | `config/queries.yaml`、`query_catalog.py`、`query_params.py`、`query_client.py` |
| EventServer HTTP 契约 | `eventserver_client.py`；规范见 `CONTRACT.md` |
| 权限缓存、限流、日志 | `permission_cache.py`、`rate_limit.py`、`observability.py` |
| 主动发送与投递轮询 | `hermes_sender.py`、`delivery_worker.py`、`delivery_renderer.py` |
| 当前架构与数据流 | `docs/architecture.md` |
| 配置、安装与验证 | `config.py`、`.env.example`、`README.md`、`tests/` |

上述 Python 模块位于 `autoqq_business_plugin/`。`config/*.yaml` 当前内容为 JSON 兼容的
YAML，由代码使用 JSON 解析；修改时保持可被 `json.loads` 读取。项目版本以
`pyproject.toml`、`plugin.yaml` 和包的 `__version__` 为准，发布时三处保持一致。构建 wheel
还会通过 `pyproject.toml` 将命令和查询目录放入包内；修改目录时同时检查源码运行和打包路径。

## 不得破坏的行为

- 只从 Hermes 可信 QQBot 上下文取得稳定 OpenID；昵称、普通 QQ 号和消息文本不能作为身份。
- 所有斜杠命令，包括未知或格式错误的命令，都由 Plugin 确定性处理并 `skip` LLM。普通消息
  只有账号 `active` 且 `chat=true` 才能进入 LLM。
- `chat` 与 `command` 独立。`authorized` 命令要求 `active + command=true`；`admin` 命令
  还要求 `role=admin`。`blocked` 优先于所有策略，`public` 命令也要完成身份、封禁检查和限流。
- EventServer 故障、响应格式错误或身份不可信时失败关闭。不要以缓存错误、网络错误或命令
  处理异常为理由放行到 LLM。权限变更后清除相应进程内缓存。
- 管理命令不能配置成 `public`。`/grant`、`/revoke` 必须显式指定 `chat`、`command` 或
  `all`；角色变更不隐式授予权限。管理操作携带真实操作者 OpenID，服务端再次校验。
- `/bind` 的关注项只能来自 EventServer 事件目录；Plugin 可以把受控标签/别名映射为规范键，
  不能自行发明事件或订阅值。
- 按需查询只接受 `config/queries.yaml` 声明的主题和查询服务目录声明的参数。用户不能提交
  任意 URL、路径或表达式；`QUERY_SERVICE_TOKEN` 与 `INTERNAL_API_TOKEN` 和 Publisher
  发布 Token 分开。查询失败给固定降级回复，不进入 LLM，也不创建订阅或 delivery。
- delivery worker 只领取、验证目标与内容、经 Hermes adapter 发送到 delivery 固化的绑定会话，
  群聊目标需 @ 订阅用户，再回写 `ack`/`fail`。
  重试次数、时间和 `dead` 状态由 EventServer 决定。发送成功但 `ack` 前崩溃可能重复发送，
  因此只能宣称至少一次投递。
- `delivery_renderer.py` 只解释通用 `subscription_match` 元数据，并只精确替换
  `• <label>` 整行。不得按 `event_key` 或 Warframe 字段分支；异常元数据和超长结果必须
  回退原文。

## 修改时的工作方式

1. 先查看 `git status --short --branch`，保留已有修改；确认任务是否只涉及本仓库。
2. 按变更类型先读对应实现和测试。命令变更同时检查策略白名单、帮助文本、参数校验与
   `tests/test_processor.py` 等相关测试；查询变更检查目录、客户端和降级/图片发送测试；
   投递变更检查 worker、Hermes sender、租约响应解析及测试。
3. HTTP 路径、请求/响应、权限或投递语义变化时，同步更新 `CONTRACT.md`、相关测试，
   并核对 EventServer 的契约和实现。新增通用事件类型通常不应修改 Plugin 核心。
4. 在已准备好依赖的本项目 Python 环境中做本地验证：

   ```bash
   python3 -m pytest
   ruff check .
   ruff format --check .
   ```

   默认使用 fixture/mock，不连接真实 EventServer、Hermes 或 QQ。不要因缺少本地依赖就
   擅自安装到用户环境；安装、真实联调和部署遵从当前用户授权。
5. 若任务包括 Hermes 部署，按 `README.md` 验证 Git clone、manifest doctor、启用、Gateway
   环境变量和 QQBot adapter。`manifest_version: 2` 的当前兼容部署方式是克隆仓库到 Hermes
   持久插件目录；不要假设原生 `hermes plugins install <git-url>` 已兼容。

## 配置与安全

`EVENT_SERVER_URL`、`INTERNAL_API_TOKEN` 是必需项；启用轮询时还需要稳定且每实例唯一的
`DELIVERY_WORKER_ID`。`QUERY_SERVICE_URL` 和独立的 `QUERY_SERVICE_TOKEN` 只在启用按需查询
时配置。完整变量与默认值见 `.env.example` 和 `config.py`。真实值只放在未跟踪的环境文件或
Secret 管理系统，不能提交、打印到命令输出、测试 fixture 或日志。

日志必须脱敏 OpenID、pairing code、租约 Token 和外部 payload；不要记录 QQ Secret、
LLM Key、内部 API Token 或带签名的图片 URL。Plugin 不应持有 `MYSQL_*`/`DATABASE_URL`。
未经当前任务明确要求，不连接真实服务、不改线上权限/订阅、不发送 QQ 消息、不部署 Gateway。
