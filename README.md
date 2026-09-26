# AutoQQ Hermes Plugin

AutoQQ Plugin 是运行在 Hermes Gateway 内的 QQ 业务接入插件。它在消息进入 LLM 前完成权限与
命令处理，并从 AutoQQ EventServer 领取通知，通过 Hermes 已连接的 QQBot 发送到订阅绑定会话。

Plugin 不连接 MySQL、不采集事件源，也不持有 QQ App 凭据。用户、权限、订阅、事件和待投递
消息均由 EventServer 管理。

## 从哪里开始

Plugin 没有独立的 Docker 服务：它由已经运行的 Hermes Gateway 加载。首次部署按顺序完成：

1. 按 [EventServer 部署指南](https://github.com/fkYang/hermes_event_server/blob/main/README.md)
   启动 API，确认 `/ready` 可用；如需事件通知，再部署至少一个 Publisher 并注册事件目录。
2. 在 Hermes 宿主机准备 Plugin 环境文件，把它注入 Gateway 的 Compose service，并确认双方共用
   Docker 网络。
3. 将本仓库克隆到 Hermes 持久插件目录，运行 doctor，启用后重启 Gateway。
4. 用 `/help`、`/whoami`、授权和订阅命令验证功能；通知链路按本文后面的步骤验收。

下文命令是部署示例，需把容器名、Compose service、网络和路径替换为实际值。

## 服务能力

- 从 Hermes 可信消息上下文识别 QQ OpenID。
- 分别控制普通聊天权限 `chat`、业务命令权限 `command` 和事件前缀权限 `bind`。
- 支持 `public`、`authorized`、`admin` 三类命令策略。
- 在 LLM 前确定性处理命令；已处理命令不会进入 LLM。
- 为未知用户申请一次性 pairing code。
- 管理用户授权、角色与事件订阅，并记录每个订阅来自私聊还是群聊。
- 通过受控目录向只读查询服务请求按需数据（如 `/wf 地球`），回复后同样跳过 LLM。
- 轮询 EventServer delivery，向固化的私聊或群聊目标发送并回写 `ack` 或 `fail`；群聊会 @ 用户。
- 根据 EventServer 提供的收件人订阅交集，通用高亮通知正文中的精确命中项。
- EventServer 不可用、响应异常或用户被封禁时默认拒绝，不降级为放行。

消息与通知链路：

```text
QQ 消息 -> Hermes QQBot -> AutoQQ Plugin
                         -> 权限/命令请求 -> EventServer -> MySQL

EventServer delivery -> Plugin 领取 -> Hermes QQBot 发送到绑定会话（群聊 @ 用户） -> ack/fail
```

## 部署前提

- Hermes Gateway 已配置并连接 QQBot。
- AutoQQ EventServer 已完成迁移、启动并处于 `ready` 状态。
- Hermes 与 EventServer 加入同一个 Docker 网络。
- 需要按需查询命令时，Hermes 还要能访问查询服务（默认 `autoqq-wfdata-publisher:8081`）。
- Hermes 持久数据目录可写；本文以容器内 `/opt/data` 为例。
- Hermes Python 环境包含 `httpx>=0.28,<1`。
- EventServer 与 Plugin 使用同一个 `INTERNAL_API_TOKEN`。

当前 Plugin manifest 为版本 2。Hermes Agent `v0.21.2` 的原生 Git installer 只接受 manifest
版本 1，因此已验证的部署方式是直接把 Git 仓库克隆到 Hermes 持久插件目录，而不是执行
`hermes plugins install <git-url>`。后续 Hermes 升级后可重新验证原生安装能力。

## 在 Docker 化 Hermes 中部署

以下示例假定：

- Hermes 容器名为 `hermes`；
- Hermes Compose service 为 `hermes-gateway`；
- 持久插件目录为 `/opt/data/plugins`。

实际名称不同时，应先用 `docker inspect` 和 Hermes 命令确认，不要直接照搬。`/opt/data/plugins`
必须位于持久卷；如果只是容器临时文件系统，重建 Gateway 后插件会消失。

### 1. 配置 Hermes Gateway

在宿主机安全目录创建 `plugin.env`：

```dotenv
EVENT_SERVER_URL=http://autoqq-eventserver-api:8080
INTERNAL_API_TOKEN=<与EventServer完全相同的随机Token>

DELIVERY_WORKER_ID=hermes-main
DELIVERY_POLL_ENABLED=true
DELIVERY_POLL_INTERVAL_SECONDS=5
DELIVERY_CLAIM_BATCH_SIZE=10
DELIVERY_LEASE_SECONDS=60

PERMISSION_CACHE_TTL_SECONDS=30
COMMAND_RATE_LIMIT_COUNT=10
COMMAND_RATE_LIMIT_WINDOW_SECONDS=60
DEFAULT_TIMEZONE=Asia/Shanghai

# 可选：只读查询服务（wf-data Publisher 的查询端点），用于 /wf 等按需查询命令。
# 不配置时 /wf 仍会注册，但会回复「查询服务未启用」。Token 必须与部署侧的
# WFDATA_QUERY_API_TOKEN 完全一致。
#QUERY_SERVICE_URL=http://autoqq-wfdata-publisher:8081
#QUERY_SERVICE_TOKEN=<至少32位随机字符>
QUERY_CONNECT_TIMEOUT_SECONDS=2
QUERY_READ_TIMEOUT_SECONDS=4
QUERY_MAX_RESPONSE_BYTES=65536
QUERY_CATALOG_TTL_SECONDS=300
QUERY_REPLY_MAX_CHARS=1200
```

限制文件权限：

```bash
chmod 600 /absolute/secure/path/plugin.env
```

将环境文件注入 Hermes Gateway，并让 Hermes 与 EventServer 共用网络：

```yaml
services:
  hermes-gateway:
    env_file:
      - /absolute/secure/path/plugin.env
    networks:
      - hermes_net

networks:
  hermes_net:
    external: true
    name: my_web_net
```

如果 Hermes Compose 已经有 `env_file`、`volumes` 或 `networks`，合并条目，不要覆盖原配置。
仅修改容器持久目录中的 `.env` 不一定能让 Gateway 子进程继承变量。需要按需查询时，还要确保
Gateway 与查询服务处于同一可通信网络；未部署查询服务可保持 `QUERY_SERVICE_*` 注释状态。
不要给 Plugin 配置 `MYSQL_*`、`DATABASE_URL`、QQ Secret 或腾讯 API 凭据。

为了让事件通知中的匹配项加粗显示，在 Hermes `config.yaml` 中显式启用 QQBot
Markdown（应与现有 `platforms.qqbot` 配置合并）：

```yaml
platforms:
  qqbot:
    enabled: true
    extra:
      markdown_support: true
```

这是 Hermes 平台配置，不是 `plugin.env` 环境变量。如果关闭，Plugin 仍会生成相同的
Markdown 文本，但 QQBot Adapter 会按其非 Markdown 路径处理。

### 2. 克隆 Plugin

首次安装：

```bash
docker exec -u 0 hermes mkdir -p /opt/data/plugins
docker exec -u 0 hermes git clone --branch main --single-branch \
  https://github.com/fkYang/hermes_qqbot_plugin.git \
  /opt/data/plugins/autoqq-business
```

`main` 表示取得当前最新版本。生产环境建议在克隆后固定到发布 tag 或完整 commit SHA：

```bash
docker exec -u 0 hermes git -C /opt/data/plugins/autoqq-business \
  checkout --detach <release-tag-or-full-commit-sha>
```

确保 Hermes Gateway 用户可以读取该目录。先检查当前镜像和 Gateway 进程用户，再按实际
UID/GID 修正所有权；不要固定使用示例 UID：

```bash
docker exec hermes id
docker exec -u 0 hermes chown -R <gateway-uid>:<gateway-gid> /opt/data/plugins/autoqq-business
```

如果 Hermes 容器不包含 Git，可在宿主机克隆后放入 Hermes 的持久 volume 对应目录，最终
容器内路径仍应为 `/opt/data/plugins/autoqq-business`，`plugin.yaml` 必须位于该目录。
真实 `plugin.env` 应保留在 Git 克隆目录之外。

### 3. 重建、检查并启用 Gateway

先在 Hermes Compose 项目目录重新创建 Gateway，使新 `env_file` 生效。然后再运行 doctor；
doctor 和 Gateway 必须读取同一组 `EVENT_SERVER_URL`、`INTERNAL_API_TOKEN` 配置：

```bash
docker compose up -d --force-recreate hermes-gateway

docker exec hermes hermes plugins doctor \
  /opt/data/plugins/autoqq-business --ci

docker exec hermes hermes plugins enable \
  autoqq-business --no-allow-tool-override
```

启用后重启 Gateway 以加载 Plugin：

```bash
docker compose restart hermes-gateway
```

验证：

```bash
docker exec hermes hermes gateway status --deep
docker exec hermes hermes plugins doctor \
  /opt/data/plugins/autoqq-business --ci
docker logs --since 10m hermes
```

期望结果：

- Gateway 为 running；
- `autoqq-business` 为 enabled；
- Plugin doctor 的 manifest、导入、注册和 hook 检查通过；
- EventServer 日志持续出现成功的 delivery claim 请求；
- Hermes 日志没有 AutoQQ 加载或认证错误。

## 首管理员与用户授权

### 首管理员

已知管理员稳定 QQ OpenID 时，可由 EventServer 的 `INITIAL_ADMIN_OPENIDS` 完成初始化。

不知道完整 OpenID 时：

1. 保持 EventServer 的 `INITIAL_ADMIN_OPENIDS=`。
2. 预定管理员向机器人发送一条普通消息。
3. Plugin 返回一个约 10 分钟有效的 pairing code。
4. 运维人员在 EventServer 部署主机执行本地一次性首管理员引导命令。
5. 引导成功后，该账号获得 `active + admin + chat=true + command=true + bind=*`。

一次性首管理员引导由 EventServer 完成，Plugin 不直接修改数据库。

### 后续用户

未知用户向机器人发送消息后会收到 pairing code。管理员在过期前执行：

```text
/grant <pairing_code> chat
/grant <pairing_code> command
/grant <pairing_code> all
```

`chat` 和 `command` 相互独立：

| 权限 | 能力 |
| --- | --- |
| `chat=true` | 普通消息可以进入 Hermes LLM |
| `command=true` | 可以执行受保护命令 |
| `bind=<scope...>` | 可以绑定并接收命中事件前缀的通知 |

管理员角色不会自动授予权限。管理员命令要求账号为 `active`、`role=admin` 且
`command=true`。

## 用户命令

| 命令 | 访问策略 | 用途 |
| --- | --- | --- |
| `/help` | public | 查看命令帮助 |
| `/events [event_key]` | public | 查看可订阅事件；带事件键时列出该事件的可关注任务 |
| `/whoami` | public | 查看自己的脱敏身份、状态和权限 |
| `/bind <event_key> [关注项...]` | authorized | 订阅事件，可指定关注任务并记录当前会话 |
| `/unbind <event_key>` | authorized | 取消订阅 |
| `/bindings` | authorized | 查看当前订阅、关注项和绑定会话类型 |
| `/grant <目标或配对码> chat\|command\|all` | admin | 授予权限 |
| `/grant <目标> bind <scope...>` | admin | 增加事件绑定前缀权限 |
| `/revoke <目标> chat\|command\|all` | admin | 撤销权限 |
| `/revoke <目标> bind <scope...\|all>` | admin | 删除指定或全部事件绑定前缀权限 |
| `/admin <目标>` | admin | 设置管理员角色，不自动授予权限 |
| `/unadmin <目标>` | admin | 移除管理员角色 |
| `/permissions <目标>` | admin | 查看目标权限 |
| `/userlist` | admin | 当前 EventServer v1 暂未提供列表接口，命令会安全终止 |
| `/wf <主题> [参数...]` | public | 按需查询 Warframe 实时数据，如 `/wf 地球`；主题与参数来自受控查询目录 |

所有命令都支持 `/<命令> help`（等价写法 `/help <命令>`）查看用法与可选项，例如
`/wf help`、`/wf 地球 help`、`/bind help`。帮助与正常执行一样先经过命令策略校验，
管理员命令的帮助只对管理员可见，并且确定性跳过 LLM。

`public` 表示无需预先授权，不表示绕过身份检查。显式封禁用户不能使用 public 命令；
EventServer 不可用时 public 命令也会失败关闭。无法可靠解析 `@昵称` 时，应使用 pairing code
或明确 OpenID，不能按昵称授权。

### 事件绑定权限

`bind` scope 只接受 `*` 或以 `.*` 结尾的小写点分隔前缀。例如：

```text
/grant <用户OpenID> bind *
/grant <用户OpenID> bind warframe.*
/grant <用户OpenID> bind warframe.cetus.*
/revoke <用户OpenID> bind warframe.cetus.*
/revoke <用户OpenID> bind all
```

匹配按点分隔边界进行：`warframe.*` 可以授权 `warframe.cetus.bounty_current`，但不会授权
`warframe2.cetus.bounty_current`。`/grant ... all` 仍只表示 `chat+command`，不会隐式授予
`bind=*`；pairing code 也不能直接接收 scope，用户配对后应按 OpenID 另行授权。

EventServer 首次升级到 `e91c7a4d6b20` 时，会为当时已经存在的非管理员用户回填
`warframe.cetus.*`。迁移完成后新建的用户仍然默认没有 bind scope，需要管理员显式授权。

创建或更新订阅要求 `active + command=true` 且事件键命中 scope。撤销或收窄 scope 后旧订阅
不会删除，用户仍可 `/unbind`；EventServer 停止为不再授权的订阅创建新 delivery，重新授权只
恢复未来事件。

### 关注项

有些事件支持「只关注其中一部分内容」，例如 Cetus 赏金轮换事件支持关注具体任务。关注项的
取值由 EventServer 事件目录声明，Plugin 不做任何猜测：`/events <event_key>` 列出全部可选项，
`/bind <event_key> 关注项A,关注项B` 只接收命中这些项的投递。事件声明关注项必填时，绑定必须
带至少一项；不指定且事件允许时表示关注该事件的全部内容。关注项可以写成逗号分隔的一串，也
可以写成多个空格分隔的参数。

关注项既可以写目录里的任务键（如 `RescueBountyResc`），也可以写中文名（如 `搜索并救援`），
大小写不敏感；Plugin 把它们确定性映射成任务键后再提交给 EventServer，服务端只接受目录里
声明的键。`/bindings` 会显示中文名（目录不可用时回退为任务键）。

绑定来源随订阅保存。私聊执行 `/bind` 时确认消息回到当前私聊；群聊执行时确认消息回到原群，
并 @ 发起绑定的用户。再次从另一个会话绑定同一事件会更新关注项和绑定来源。旧订阅在再次绑定前
显示为「未知会话（旧订阅）」，其通知回退到用户私聊。EventServer 创建 delivery 时会固化当前
来源，因此私聊绑定的通知发私聊，群聊绑定的通知发到原群并 @ 用户；之后重新绑定不会改写已经
创建的 delivery。

### 按需查询

`/wf` 是查询命令的命名空间，可用于「现在就想看一眼」而不必先订阅。它按
「命令 / 领域 / 指令 / 参数」四段组织，其中指令可以省略：

```text
/<命令> <领域> [<指令>] <参数...>
/wf     地球                     -> 回复原生赏金图片
/wf     地球 搜索并救援           -> 筛选后的文本 + 图片链接
/wf     紫卡 托里德               -> 该武器 3P1N 结构的紫卡图片
/wf     紫卡 托里德 3P            -> 指定紫卡结构并回复对应图片
/wf     紫卡 词条 托里德 3+1       -> 显式指令（词条）+ 结构别名
/wf     紫卡 "Dual Toxocyst" 3P   -> 带空格的武器名用引号包起来
```

主题别名写在 `config/queries.yaml`（受控文件，随部署维护），每个主题指向只读查询服务声明
的稳定 `query_key`；参数名称、必填/可选、取值枚举都由服务端目录给出，Plugin 不做任何猜测：

```text
/wf help        # 列出领域与每个领域的参数
/wf 紫卡 help    # 列出指令、参数、必填项与取值枚举
```

链路：

```text
/wf 地球 -> Plugin 解析别名 -> GET {QUERY_SERVICE_URL}/v1/queries/cetus-bounties
         -> 渠道无关文本 + image_url -> QQBot Adapter.send_image -> QQ 原生图片
```

主题可以在 `config/queries.yaml` 里声明 `"reply": "image_url"`：无参数查询通过 QQBot Adapter
的 `send_image` 发送原生图片，带筛选条件时仍回复文本加图片链接。若图片与参数化查询结果一一
对应，可声明 `"reply": "image_url_always"`，紫卡查询使用这一模式。Adapter 不支持图片或上传
失败时回退成图片 URL 文本；服务没返回图片链接时回退成文本。图片来源只接受 `http` 或 `https`
URL。

参数可以按位置书写，也可以写成 `参数名=值`（值含空格时用引号）；枚举参数接受目录里的键、
中文名或别名（例如 `3+1` 等价于 `3P1N`）。参数不在目录范围内、必填项缺失、参数过多都会在
Plugin 侧直接拒绝，不会打到查询服务。

查询不创建订阅或 delivery，也不会进入 LLM。上游不可用时回复固定降级文案；未配置
`QUERY_SERVICE_URL` 时命令仍然注册，但会明确回复「查询服务未启用」。查询请求同样受
`COMMAND_RATE_LIMIT_*` 限流，查询服务侧对 wf-data 的响应有 30 秒缓存，用户请求不会直接
放大成上游压力。

## 事件通知

用户具有 `command=true`、匹配事件键的 `bind` scope 并完成 `/bind <event_key>` 后，
EventServer 在事件发生时创建
delivery。Plugin 自动完成：

```text
claim -> Hermes QQBot 私聊 -> ack/fail
```

通知不会进入 LLM，也不会创建普通 Agent 对话。投递语义为至少一次：如果 QQ 已发送成功，
但 Plugin 在回写 `ack` 前退出，租约过期后可能再次发送同一通知。

对于使用订阅关注项的事件，EventServer 会在每个收件人的消息中附带实际命中标签。
Plugin 只对正文中完全相等的项目符号行做通用渲染：

```markdown
【帐篷 A】
• **搜索并救援** 🔴
```

不匹配的行保持原样；同一标签出现在多个来源分组时会逐行加粗。该逻辑不依赖
wfdata 或具体 `event_key`，其他 Publisher 只要遵守同一通用元数据和文本格式即可复用。

## 更新与回退

更新前先记录当前 commit 并确认工作树干净：

```bash
docker exec hermes git -C /opt/data/plugins/autoqq-business rev-parse HEAD
docker exec hermes git -C /opt/data/plugins/autoqq-business status --short
```

更新到远端 `main`。以下 `chown` 中的 UID/GID 应与首次部署时核实的一致；命令在 Hermes
Compose 项目目录执行：

```bash
docker exec -u 0 hermes git -C /opt/data/plugins/autoqq-business fetch origin main
docker exec -u 0 hermes git -C /opt/data/plugins/autoqq-business \
  checkout --detach origin/main
docker exec -u 0 hermes chown -R <gateway-uid>:<gateway-gid> /opt/data/plugins/autoqq-business
docker exec hermes hermes plugins doctor \
  /opt/data/plugins/autoqq-business --ci
docker compose up -d --force-recreate hermes-gateway
```

回退时 checkout 到之前记录的完整 commit，再重新执行 doctor 并重建 Gateway。不要在运行目录
保留手工修改；环境配置应继续放在独立的 `plugin.env` 中。

## 常见问题

| 现象 | 检查项 |
| --- | --- |
| Plugin doctor 报 URL 或 Token 配置错误 | 确认环境变量已经注入 doctor 和 Gateway 进程，Token 至少 32 个字符 |
| Gateway 启动后未加载 Plugin | 检查目录、所有权、enable 状态，并重新创建 Gateway 容器 |
| `/help` 也被拒绝 | 检查 EventServer `/ready`、Docker 网络和两端 Token；Plugin 按失败关闭处理 |
| 一直领取不到通知 | 检查 `DELIVERY_POLL_ENABLED`、稳定 worker ID、用户 `command`、事件 `bind` scope 和订阅 |
| delivery 持续 retry | 检查 Hermes QQBot 连接、发送限制和 EventServer 的脱敏错误码 |
| 通知偶尔重复 | 这是发送成功但 `ack` 前退出时的至少一次投递边界 |
| `/wf` 回复「查询服务未启用」 | 检查 Plugin 的 `QUERY_SERVICE_URL`、`QUERY_SERVICE_TOKEN`，以及 Publisher 的 `QUERY_API_TOKEN` 是否一致 |
| `/wf` 回复「查询服务暂时不可用」 | 检查 wf-data Publisher 是否加入 `hermes_net`、`8081` 是否监听、能否访问 wf-data |

## 参考文档

- [完整环境变量模板](.env.example)
- [当前架构与关键数据流](docs/architecture.md)
- [权限、命令和投递契约](CONTRACT.md)
- [QQ/Hermes payload 核验清单](docs/qq-payload-probe.md)
- [AutoQQ EventServer](https://github.com/fkYang/hermes_event_server)
- [独立 Publisher 仓库](https://github.com/hermes-about/hermes_providers)
