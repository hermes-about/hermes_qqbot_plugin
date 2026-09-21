# AutoQQ Hermes Plugin Contract

> 本文定义 Plugin 必须遵守的权限、命令、消息处理和 EventServer 交互契约。
> 安装、配置、升级和故障排查见 `README.md`；实现进度和历史验证记录见根目录
> `.planning/`，不在本文维护。

## 1. 范围与边界

Plugin 运行在 Hermes 内，负责：

- 从 Hermes 可信消息上下文提取平台、发送者 OpenID、会话和原始 payload。
- 在 LLM 或受保护命令执行前完成权限判断。
- 确定性处理斜杠命令，处理完成后始终跳过 LLM。
- 按受控目录向只读查询服务请求按需数据，并把渠道无关结果回复给用户。
- 从 EventServer 领取通用待投递消息，并通过 Hermes 主动私聊发送。
- 向 EventServer 回写发送成功或失败。

Plugin 不得：

- 直连 MySQL，或保存独立的用户、订阅、事件事实数据。
- 轮询领域数据源或判断领域事件是否触发。
- 导入 Warframe 或其他 provider 的领域模型。
- 让普通用户指定查询目标 URL、路径或任意表达式。
- 让 LLM 决定权限、命令、订阅或事件。
- 绕过 Hermes 持有 QQ 凭据或直接调用腾讯 QQ API。

EventServer 是权限、用户、订阅、事件和 delivery 状态的唯一事实来源。双方只通过
版本化 HTTP API 交互。

## 2. 可信身份

- 身份只使用平台提供的稳定 OpenID。
- 昵称、普通 QQ 号和消息文本不得用于身份判断或授权。
- 每个入站请求都必须先取得可信 `platform`、发送者 OpenID 和会话信息。
- 无法取得稳定 OpenID 时不得授权、订阅或执行受保护命令。
- 管理员操作必须携带实际消息发送者 OpenID，EventServer 必须再次校验操作者。

## 3. 消息处理顺序

每条入站 QQ 消息按以下顺序处理：

```text
提取可信发送者 OpenID
  -> 判断是否为已注册斜杠命令
  -> 是命令：按 public / authorized / admin 策略检查
       -> 执行或拒绝
       -> 始终 skip LLM
  -> 不是命令：查询 chat 权限
       -> chat=true：allow，进入 Hermes Agent/LLM
       -> chat=false、未知用户或 blocked：拒绝，不调用 LLM
```

硬性要求：

- 未注册或格式错误的 `/...` 消息返回未知命令和 `/help` 提示，不进入 LLM。
- 已识别的命令无论成功或失败都不得落入 LLM。
- 普通聊天只有在 `chat=true` 且账号未被封禁时才允许进入 LLM。
- EventServer 不可用时失败关闭，不得把服务故障解释为放行。

## 4. 权限契约

用户权限包含两个互不隐含的维度：

| 权限 | 含义 | 不代表 |
| --- | --- | --- |
| `chat` | 普通非命令消息可以进入 Hermes LLM | 不授予命令权限 |
| `command` | 可以执行 `authorized` 命令、管理订阅并接收事件通知 | 不授予普通聊天权限 |

允许的权限组合：

| `chat` | `command` | 结果 |
| --- | --- | --- |
| false | false | 只能执行 `public` 命令 |
| false | true | 可以执行公开和已授权命令，但不能聊天 |
| true | false | 可以聊天和执行公开命令，但不能执行受保护命令 |
| true | true | 可以聊天并执行受保护命令 |

账号状态与角色规则：

- `blocked` 优先于所有权限，被封禁用户不能执行任何命令。
- 不存在用户与显式封禁用户必须可区分。
- `role=admin` 不自动授予 `chat` 或 `command`。
- 管理员命令要求 `active + command=true + role=admin`。
- `/admin` 和 `/unadmin` 只修改角色，不隐式修改权限。
- `/grant` 和 `/revoke` 必须显式指定 `chat`、`command` 或两者。
- 授权和撤销必须幂等。
- 撤销 `command` 后保留订阅但暂停生效，不创建新 delivery；重新授予后不补发停权期间事件。

稳定权限响应结构：

```json
{
  "platform": "qqbot",
  "openid": "CURRENT_USER_OPENID",
  "account_status": "active",
  "role": "user",
  "permissions": {
    "chat": false,
    "command": true
  }
}
```

## 5. 命令契约

每条命令必须声明一种策略：

| 策略 | 访问条件 |
| --- | --- |
| `public` | 需要可信 OpenID 和封禁检查；不要求业务授权 |
| `authorized` | `active + command=true` |
| `admin` | `active + command=true + role=admin` |

命令目录由受版本控制的显式白名单维护。管理类命令不得配置为 `public`。普通用户不能
通过聊天修改策略。

当前命令集合：

| 命令 | 策略 | 用途 |
| --- | --- | --- |
| `/help` | public | 查看帮助 |
| `/events [event_key]` | public | 查看可订阅事件；带参数时列出该事件的可关注项 |
| `/whoami` | public | 查看自己的脱敏身份和权限 |
| `/bind <event_key> [关注项...]` | authorized | 订阅事件，可指定要关注的目标项 |
| `/unbind <event_key>` | authorized | 取消订阅 |
| `/bindings` | authorized | 查看订阅及其关注项 |
| `/grant <目标或 pairing_code> chat\|command\|all` | admin | 授予权限 |
| `/revoke <目标> chat\|command\|all` | admin | 撤销权限 |
| `/admin <目标>` | admin | 设置管理员角色 |
| `/unadmin <目标>` | admin | 移除管理员角色 |
| `/permissions <目标>` | admin | 查看目标权限 |
| `/userlist` | admin | 当前 EventServer v1 未提供列表接口，安全终止 |
| `/wf <主题> [参数...]` | public | 向只读查询服务请求数据；主题、参数与别名来自受控查询目录 |

### 5.1 命令帮助

每个命令都支持 `/<命令> help`（等价写法 `/help <命令>`）查看用法与可选项，例如
`/wf help`、`/bind help`。帮助文本属于命令契约的一部分：

- 参数形式、必填项和取值范围必须与真实校验一致。
- 查询类命令的可选项来自服务端目录，Plugin 不猜测也不硬编码取值。
- 帮助请求同样先经过命令策略校验，因此管理员命令的帮助只对管理员可见。
- 帮助处理与正常执行一样确定性跳过 LLM。

### 5.2 按需查询命令

`/wf` 是查询命令的示例命名空间，固定为四段结构，其中指令可省略：

```text
/<命令> <领域> [<指令>] <参数...>
```

- **领域**：`config/queries.yaml` 声明的别名，解析成查询服务目录里的 `query_key`。
- **指令**：目标声明的动作别名；没有声明动作时全部参数都属于默认动作。
- **参数**：由服务端目录声明名称、类型（`text`/`enum`）、必填性、是否多值、长度上限与取值枚举。

```text
用户输入 /wf 紫卡 托里德 3P1N
  -> Plugin 解析领域别名 -> warframe.riven.summary
  -> 取目录中的默认指令与参数表，按位置或「参数名=值」拆分，校验必填/枚举/长度
  -> GET {QUERY_SERVICE_URL}/v1/queries/{route}?weapon=托里德&shape=3P1N
  -> 返回渠道无关文本与可选 image_url
  -> Plugin 按 reply 形态回复；始终跳过 LLM
```

要求：

- Plugin 只接受目录里声明的主题和参数，不接受用户提供的 URL、路径或表达式。
- 参数只做位置/具名映射与目录校验，Plugin 不解释取值含义，也不做领域归一化（武器名解析等
  由查询服务完成）；带空格的自由文本参数使用引号，解析器必须保持其余命令的空白切分行为。
- 目录新增领域或参数不得修改 Plugin 代码；查询服务目录是参数契约的唯一事实来源。
- 查询失败必须降级为确定性文案，不把上游错误细节、Token 或签名 URL 写入日志。
- 查询结果只回复给发起用户；查询不创建订阅、delivery 或审计以外的状态。
- 未配置 `QUERY_SERVICE_URL` 时命令仍然注册，但明确回复「查询服务未启用」。
- 回复形态由目录声明：`reply=text`（默认）回复文本，`reply=image_url` 时无参数查询通过渠道
  Adapter 的原生图片接口发送；带参数筛选或服务未返回图片链接时回退为文本加链接，避免图片
  内容与筛选条件不一致。
- 原生图片发送失败或 Adapter 不支持图片时回退为图片 URL 文本；回复文本设有长度上限，图片
  链接在截断后仍保留。
- 传给 Adapter 的图片来源只接受带主机名的 `http` 或 `https` URL，不接受本地路径或其它协议。

订阅的「关注项」是事件目录为该事件声明的受控取值。`/events <event_key>` 列出可选项，
`/bind` 一个或多个关注项即只接收命中这些项的投递；不指定关注项表示关注该事件的全部内容，
但事件声明 `match_keys_required` 时必须显式指定。

Plugin 接受关注项的任务键或目录里的中文名（大小写不敏感），并在提交前映射成规范任务键；
EventServer 只接受目录声明的键，因此该映射属于确定性的输入规范化，不是权限或事件判定。
关注项由 EventServer 校验，用户无法写入目录之外的任意值。重复绑定同一事件时，关注项以
最后一次为准。

`public` 只表示无需业务授权，不表示匿名。`/whoami` 只能展示调用者自己的脱敏信息。
无法可靠解析被 @ 用户的 OpenID 时必须使用 pairing code 或明确 OpenID，不得按昵称授权。

## 6. EventServer API 依赖

| Plugin 动作 | EventServer API | 契约要求 |
| --- | --- | --- |
| 查询权限 | `GET /v1/users/{openid}/permission` | 返回账号状态、角色、`chat`、`command` |
| 查看事件 | `GET /v1/events` | 只返回已注册且启用的事件；可关注项来自目录声明 |
| 查看订阅 | `GET /v1/users/{openid}/subscriptions` | 返回稳定 event key 与该订阅的关注项 |
| 新增订阅 | `POST /v1/users/{openid}/subscriptions` | 幂等，可携带 `match_keys`；响应回显 `match_keys` 与 `updated` |
| 删除订阅 | `DELETE /v1/users/{openid}/subscriptions/{event_key}` | 幂等 |
| 授予权限 | `POST /v1/users/{openid}/grant` | 显式权限维度，管理员操作 |
| 撤销权限 | `POST /v1/users/{openid}/revoke` | 显式权限维度，管理员操作 |
| 修改角色 | `POST /v1/users/{openid}/role` | 不隐式修改权限 |
| 生成授权码 | `POST /v1/pairing-codes` | 只能绑定可信 OpenID |
| 批准授权码 | `POST /v1/pairing-codes/{code}/approve` | 管理员操作 |
| 领取消息 | `POST /v1/deliveries/claim` | 原子租约领取 |
| 确认发送 | `POST /v1/deliveries/{delivery_id}/ack` | 校验当前租约 Token |
| 回写失败 | `POST /v1/deliveries/{delivery_id}/fail` | 上报脱敏错误和可重试性 |
| 查询目录 | `GET {QUERY_SERVICE_URL}/v1/queries` | 只读查询服务的主题与参数目录，结果可短期缓存 |
| 按需查询 | `GET {QUERY_SERVICE_URL}/v1/queries/{route}` | 主题由目录声明；参数限定在目录取值内 |

所有请求携带服务间认证信息：

```http
Authorization: Bearer ${INTERNAL_API_TOKEN}
X-Request-ID: <request-id>
```

查询服务使用独立凭证 `QUERY_SERVICE_TOKEN`，与 `INTERNAL_API_TOKEN`、Publisher Token 都不共用；
权限与订阅事实仍然只来自 EventServer。

管理员写操作还必须携带经过验证的操作者 OpenID。接口不兼容变更必须先更新双方契约和
契约测试，再分别升级。

## 7. Delivery 契约

Plugin 周期性领取待投递消息。示意请求：

```http
POST /v1/deliveries/claim
Authorization: Bearer ${INTERNAL_API_TOKEN}
Content-Type: application/json

{
  "worker_id": "hermes-instance-01",
  "platform": "qqbot",
  "limit": 10,
  "lease_seconds": 60
}
```

示意响应：

```json
{
  "items": [
    {
      "delivery_id": "opaque-delivery-id",
      "lease_token": "opaque-one-time-lease-token",
      "event_id": "opaque-event-id",
      "event_key": "warframe.cetus.night",
      "target": {
        "platform": "qqbot",
        "openid": "TARGET_USER_OPENID"
      },
      "message": {
        "text": "希图斯已进入夜晚"
      },
      "attempt": 1,
      "created_at": "ISO-8601 UTC"
    }
  ]
}
```

领取必须在 EventServer 的单个数据库事务内完成，将 `pending/retry` 原子更新为 `leased`，
并写入 `lease_owner`、`lease_token` 和 `lease_until`。

Plugin 对每条记录执行：

1. 校验 delivery、平台、目标 OpenID 和消息结构。
2. 调用已验证的 Hermes 主动私聊接口，只发送到记录指定的目标。
3. 成功后调用 `ack`，携带 `lease_token`、发送时间和可用时的平台消息 ID。
4. 失败后调用 `fail`，携带 `lease_token`、脱敏 `error_code` 和 `retryable`。
5. 单条失败不得终止整个 worker，也不得阻塞 QQ 入站处理。

通知是确定性投递，不进入 LLM，也不创建普通 Agent 对话轮次。

## 8. 可靠性与一致性

- 投递语义为至少一次，不承诺严格 exactly-once。
- 发送成功但在 `ack` 前崩溃，租约过期后可能重复发送。
- Hermes 或 QQ 支持幂等键时，使用稳定的 `delivery_id`。
- `ack` 和 `fail` 必须匹配当前有效租约；过期或不匹配时不得修改结果。
- Plugin 不自行无限重试；重试次数、时间和 `dead` 状态由 EventServer 决定。
- Plugin 不持久化租约。重启恢复依赖 EventServer 的租约超时逻辑。
- EventServer 不可用时不得绕过服务认证或直接访问 MySQL。
- 新事件类型接入不得要求修改 Plugin。

## 9. 缓存与故障处理

- 权限查询可以使用进程内短期缓存，建议 TTL 为 30 至 60 秒。
- 授权、撤销、角色变更或封禁成功后必须立即清除相关缓存。
- 命令策略和稳定事件目录可以在进程内缓存。
- 权限、订阅、事件、delivery 和租约不得只放在缓存中。
- 空队列或 EventServer 暂时不可用时使用带抖动的退避，不阻塞 Hermes 主链路。
- 写请求不得盲目重试；读取重试必须有上限和超时。

## 10. 安全与可观测性

- 日志中的 OpenID、pairing code、租约 Token 和外部 payload 必须脱敏。
- 禁止记录 `INTERNAL_API_TOKEN`、数据库口令、QQ Secret 或 LLM Key。
- Plugin 配置中不得出现 MySQL 连接信息。
- 管理员操作必须记录脱敏审计信息。
- `public` 命令仍需封禁检查、限流和脱敏审计。
- 普通用户不能配置任意 URL、脚本、SQL 或模板表达式。

## 11. 合规要求

实现和变更必须覆盖：

- `chat`/`command` 四种权限组合。
- `public`/`authorized`/`admin` 命令矩阵和 blocked 优先级。
- 未知命令和命令异常不进入 LLM。
- 授权、撤销、订阅和取消订阅幂等。
- EventServer 不可用时失败关闭。
- delivery 领取租约、并发互斥、`ack/fail` 校验和有限重试。
- Plugin 重启后的租约恢复。
- 管理变更后的缓存失效。
- 敏感信息不进入日志。
- 新增通用事件类型时不修改 Plugin。
