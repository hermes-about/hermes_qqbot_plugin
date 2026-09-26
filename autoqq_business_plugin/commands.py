import re
from collections.abc import Callable
from functools import partial

from .command_policy import CommandPolicyRegistry
from .eventserver_client import EventServerClient, EventServerError, EventServerResponseError
from .models import (
    AccessPolicy,
    BindingContext,
    CommandReply,
    EventInfo,
    MessageIdentity,
    ParsedCommand,
    PermissionSnapshot,
    QueryAction,
    QueryInfo,
    QueryNamespace,
    QueryParam,
    QueryResult,
    QueryTarget,
)
from .observability import mask_identifier
from .permission_cache import PermissionCache
from .query_catalog import QueryCatalog
from .query_client import (
    QueryServiceClient,
    QueryServiceError,
    QueryServiceResponseError,
)
from .query_params import assign as assign_query_params
from .query_params import normalise_tokens
from .query_params import usage as query_usage
from .query_params import validate as validate_query_params

_PAIRING_CODE = re.compile(r"^[ABCDEFGHJKLMNPQRSTUVWXYZ23456789]{8}$")
_EXPLICIT_OPENID = re.compile(r"^[A-Za-z0-9._:-]{6,128}$")
_BIND_SCOPE = re.compile(r"^(?:\*|[a-z0-9]+(?:\.[a-z0-9_]+)*\.\*)$")
_DIMENSIONS = {"chat": ["chat"], "command": ["command"], "all": ["chat", "command"]}
_MAX_MATCH_KEYS = 32
_MAX_BIND_SCOPES = 32
_DEFAULT_QUERY_REPLY_MAX_CHARS = 1200

_COMMAND_HELP: dict[str, str] = {
    "/help": "用法：/help 或 /help <命令>\n列出可用命令，或查看某个命令的用法与可选项。",
    "/events": (
        "用法：/events 或 /events <event_key>\n"
        "不带参数列出可订阅事件；带事件键时列出该事件的可关注项。"
    ),
    "/whoami": "用法：/whoami\n显示自己的脱敏 OpenID、账号状态、角色和权限。",
    "/bind": (
        "用法：/bind <event_key> [关注项...]\n"
        "订阅事件，可指定关注项；关注项可写任务键或中文名，多个用空格或逗号分隔。\n"
        "发送 /events <event_key> 查看该事件的可关注项。"
    ),
    "/unbind": "用法：/unbind <event_key>\n取消订阅；重复执行是幂等的。",
    "/bindings": "用法：/bindings\n列出当前订阅及其关注项。",
    "/grant": (
        "用法：/grant <目标|pairing_code> chat|command|all\n"
        "   或：/grant <目标> bind <scope...>\n"
        "布尔权限必须显式指定；bind scope 形如 *、warframe.*、warframe.cetus.*。"
    ),
    "/revoke": (
        "用法：/revoke <目标> chat|command|all\n"
        "   或：/revoke <目标> bind <scope...|all>\n"
        "撤销 command 或 bind 后订阅保留但暂停投递。"
    ),
    "/admin": "用法：/admin <目标>\n把目标设为管理员，不会自动授予 chat、command 或 bind。",
    "/unadmin": "用法：/unadmin <目标>\n移除目标的管理员角色，不会改动权限。",
    "/permissions": "用法：/permissions <目标>\n查看目标的账号状态、角色和权限。",
    "/userlist": "用法：/userlist\n当前 EventServer v1 未提供用户列表接口，命令会安全终止。",
    "/wf": (
        "用法：/wf <领域> [指令] [参数...]\n"
        "查询 Warframe 实时数据；领域、指令和参数由受控查询目录定义，"
        "发送 /wf help 查看当前可用的领域。"
    ),
}

_POLICY_LABELS: tuple[tuple[AccessPolicy, str], ...] = (
    (AccessPolicy.PUBLIC, "公开"),
    (AccessPolicy.AUTHORIZED, "需要 command 权限"),
    (AccessPolicy.ADMIN, "管理员"),
)


class CommandUsageError(ValueError):
    pass


def _split_action(
    info: QueryInfo | None, tokens: tuple[str, ...]
) -> tuple[QueryAction | None, tuple[str, ...]]:
    """Consume an optional action token; the default action absorbs everything else."""
    if info is None:
        return None, tokens
    if not info.actions or not tokens:
        return info.default_action(), tokens
    action = info.action_for(tokens[0])
    if action is None:
        return info.default_action(), tokens
    return action, tokens[1:]


def _param_summary(info: QueryInfo) -> str:
    params = info.action_params(info.default_action())
    return "、".join(
        f"{param.label}{'（必填）' if param.required else '（可选）'}" for param in params
    )


def _param_line(param: QueryParam) -> str:
    details = ["必填" if param.required else "可选"]
    if param.default:
        details.append(f"默认 {param.default}")
    if param.multiple:
        details.append(f"可多选，最多 {param.max_items} 项" if param.max_items else "可多选")
    if param.type == "enum":
        if len(param.options) <= 6:
            details.append("取值：" + "、".join(_option_text(option) for option in param.options))
        else:
            details.append(f"{len(param.options)} 项可选")
    return f"{param.label}（{'，'.join(details)}）"


def _option_text(option) -> str:
    return option.label if option.label == option.key else f"{option.label}（{option.key}）"


def _action_summary(info: QueryInfo) -> str:
    names = [item.label or item.key for item in info.actions if not item.default]
    return "、".join(names)


class CommandService:
    def __init__(
        self,
        client: EventServerClient,
        cache: PermissionCache,
        policies: CommandPolicyRegistry,
        mention_resolver: Callable[[str, MessageIdentity], str | None] | None = None,
        query_catalog: QueryCatalog | None = None,
        query_service: QueryServiceClient | None = None,
        query_reply_max_chars: int = _DEFAULT_QUERY_REPLY_MAX_CHARS,
    ) -> None:
        self._client = client
        self._cache = cache
        self._policies = policies
        self._mention_resolver = mention_resolver or (lambda _token, _identity: None)
        self._queries = query_catalog or QueryCatalog.empty()
        self._query_service = query_service
        self._query_reply_max_chars = query_reply_max_chars

    def execute(
        self,
        command: ParsedCommand,
        identity: MessageIdentity,
        actor: PermissionSnapshot,
    ) -> str | CommandReply:
        if self._wants_help(command):
            return self.help_text(command.name)
        handlers: dict[str, Callable[..., str | CommandReply]] = {
            "/help": self._help,
            "/events": self._events,
            "/whoami": self._whoami,
            "/bind": self._bind,
            "/unbind": self._unbind,
            "/bindings": self._bindings,
            "/grant": self._grant,
            "/revoke": self._revoke,
            "/admin": self._admin,
            "/unadmin": self._unadmin,
            "/permissions": self._permissions,
            "/userlist": self._userlist,
        }
        for namespace in self._queries.namespaces():
            handlers[namespace.command] = partial(self._run_query, namespace)
        handler = handlers.get(command.name)
        if handler is None:
            raise CommandUsageError("未知命令。发送 /help 查看可用命令。")
        return handler(command.args, identity, actor)

    def _help(self, args: tuple[str, ...], *_: object) -> str:
        if len(args) > 1:
            raise CommandUsageError("用法：/help 或 /help <命令>")
        if args:
            return self.help_text(self._command_name(args[0]))
        return self._command_overview()

    def _events(self, args: tuple[str, ...], *_: object) -> str:
        if len(args) > 1:
            raise CommandUsageError("用法：/events 或 /events <event_key>")
        events = self._client.list_events()
        if args:
            event = self._find_event(events, args[0])
            if event is None:
                return f"没有找到事件 {args[0]}。发送 /events 查看可用事件。"
            return self._format_options(event)
        visible = [item for item in events if not item.deprecated]
        if not visible:
            return "当前没有可订阅事件。"
        return "可订阅事件：\n" + "\n".join(
            f"- {item.event_key}：{item.display_name}{self._option_hint(item)}" for item in visible
        )

    def _whoami(
        self, args: tuple[str, ...], identity: MessageIdentity, actor: PermissionSnapshot
    ) -> str:
        self._expect(args, 0, "/whoami")
        return self._format_permission(actor, label=mask_identifier(identity.openid))

    def _bind(
        self, args: tuple[str, ...], identity: MessageIdentity, _actor: PermissionSnapshot
    ) -> str:
        if not args:
            raise CommandUsageError("用法：/bind <event_key> [关注项...]")
        event_key = args[0]
        tokens = self._match_key_tokens(args[1:])
        event = self._find_event(self._client.list_events(), event_key)
        if event is not None:
            requested, unknown = self._resolve_match_keys(event, tokens)
            if unknown:
                return (
                    f"关注项不在该事件的可选范围内：{unknown[0]}。"
                    f"发送 /events {event.event_key} 查看可选值（可写任务键或中文名）。"
                )
            if event.match_keys_required and not requested:
                raise CommandUsageError(
                    f"该事件必须指定至少一个关注项。用法：/bind {event.event_key} <关注项[,关注项]>"
                )
        else:
            requested = tokens
        try:
            result = self._client.subscribe(
                identity.platform,
                identity.openid,
                event_key,
                requested,
                BindingContext(identity.chat_type, identity.chat_id),
            )
        except EventServerResponseError as exc:
            if exc.status_code == 404:
                return f"没有找到事件 {event_key}。发送 /events 查看可用事件。"
            if exc.status_code == 400:
                return f"关注项未被接受。发送 /events {event_key} 查看可选值。"
            if exc.status_code == 403:
                return f"没有绑定 {event_key} 的 bind 权限，请联系管理员授予对应事件前缀。"
            raise
        return self._format_bind_result(result, event, requested)

    def _unbind(
        self, args: tuple[str, ...], identity: MessageIdentity, _actor: PermissionSnapshot
    ) -> str:
        self._expect(args, 1, "/unbind <event_key>")
        result = self._client.unsubscribe(identity.platform, identity.openid, args[0])
        return (
            f"已取消订阅 {result.event_key}。"
            if result.changed
            else f"当前未订阅 {result.event_key}。"
        )

    def _bindings(
        self, args: tuple[str, ...], identity: MessageIdentity, _actor: PermissionSnapshot
    ) -> str:
        self._expect(args, 0, "/bindings")
        items = self._client.list_subscriptions(identity.platform, identity.openid)
        if not items:
            return "当前没有订阅。"
        try:
            events = self._client.list_events()
        except EventServerError:
            events = []
        lines = []
        for item in items:
            event = self._find_event(events, item.event_key)
            source = self._binding_source_label(item.binding_context)
            if item.match_keys:
                names = "、".join(
                    (event.option_label(key) or key) if event is not None else key
                    for key in item.match_keys
                )
                lines.append(f"- {item.event_key}：关注 {names}；绑定于{source}")
            else:
                lines.append(f"- {item.event_key}：关注全部；绑定于{source}")
        return "当前订阅：\n" + "\n".join(lines)

    @staticmethod
    def _binding_source_label(binding: BindingContext | None) -> str:
        if binding is None:
            return "未知会话（旧订阅）"
        return "群聊" if binding.chat_type == "group" else "私聊"

    def _grant(
        self, args: tuple[str, ...], identity: MessageIdentity, _actor: PermissionSnapshot
    ) -> str:
        if len(args) >= 2 and args[1].lower() == "bind":
            if len(args) < 3:
                raise CommandUsageError("用法：/grant <目标> bind <scope...>")
            if _PAIRING_CODE.fullmatch(args[0].upper()):
                raise CommandUsageError(
                    "bind scope 不能授予 pairing code，请使用目标 OpenID 或 @。"
                )
            target = self._target(args[0], identity)
            scopes = self._bind_scope_tokens(args[2:])
            result = self._client.grant_bind(
                identity.platform, target, list(scopes), identity.openid
            )
            self._cache.invalidate(result.platform, result.openid)
            return "授权成功：" + self._format_permission(
                result, label=mask_identifier(result.openid)
            )
        self._expect(args, 2, "/grant <目标|pairing_code> chat|command|all")
        permissions = self._permissions_arg(args[1])
        raw_target = args[0]
        if _PAIRING_CODE.fullmatch(raw_target.upper()):
            result = self._client.approve_pairing(
                identity.platform, raw_target.upper(), permissions, identity.openid
            )
        else:
            target = self._target(raw_target, identity)
            result = self._client.grant(identity.platform, target, permissions, identity.openid)
        self._cache.invalidate(result.platform, result.openid)
        return "授权成功：" + self._format_permission(result, label=mask_identifier(result.openid))

    def _revoke(
        self, args: tuple[str, ...], identity: MessageIdentity, _actor: PermissionSnapshot
    ) -> str:
        if len(args) >= 2 and args[1].lower() == "bind":
            if len(args) < 3:
                raise CommandUsageError("用法：/revoke <目标> bind <scope...|all>")
            target = self._target(args[0], identity)
            raw = args[2:]
            clear_all = len(raw) == 1 and raw[0].lower() == "all"
            if not clear_all and any(item.lower() == "all" for item in raw):
                raise CommandUsageError("bind 的 all 必须单独使用。")
            scopes = () if clear_all else self._bind_scope_tokens(raw)
            result = self._client.revoke_bind(
                identity.platform,
                target,
                list(scopes),
                identity.openid,
                clear_all=clear_all,
            )
            self._cache.invalidate(result.platform, result.openid)
            return "撤权成功：" + self._format_permission(
                result, label=mask_identifier(result.openid)
            )
        self._expect(args, 2, "/revoke <目标> chat|command|all")
        target = self._target(args[0], identity)
        result = self._client.revoke(
            identity.platform, target, self._permissions_arg(args[1]), identity.openid
        )
        self._cache.invalidate(result.platform, result.openid)
        return "撤权成功：" + self._format_permission(result, label=mask_identifier(result.openid))

    def _admin(
        self, args: tuple[str, ...], identity: MessageIdentity, _actor: PermissionSnapshot
    ) -> str:
        return self._set_role(args, identity, "admin", "/admin <目标>")

    def _unadmin(
        self, args: tuple[str, ...], identity: MessageIdentity, _actor: PermissionSnapshot
    ) -> str:
        return self._set_role(args, identity, "user", "/unadmin <目标>")

    def _set_role(
        self, args: tuple[str, ...], identity: MessageIdentity, role: str, usage: str
    ) -> str:
        self._expect(args, 1, usage)
        target = self._target(args[0], identity)
        result = self._client.change_role(identity.platform, target, role, identity.openid)
        self._cache.invalidate(result.platform, result.openid)
        return "角色修改成功：" + self._format_permission(
            result, label=mask_identifier(result.openid)
        )

    def _permissions(
        self, args: tuple[str, ...], identity: MessageIdentity, _actor: PermissionSnapshot
    ) -> str:
        self._expect(args, 1, "/permissions <目标>")
        target = self._target(args[0], identity)
        snapshot = self._cache.get_or_load(
            identity.platform,
            target,
            lambda: self._client.get_permission(identity.platform, target),
        )
        return self._format_permission(snapshot, label=mask_identifier(target))

    def _userlist(self, args: tuple[str, ...], *_: object) -> str:
        self._expect(args, 0, "/userlist")
        return "当前 EventServer v1 尚未提供用户列表接口；命令已安全终止，未进入 LLM。"

    def _command_overview(self) -> str:
        lines = ["可用命令："]
        for policy, label in _POLICY_LABELS:
            names = [
                name for name in self._policies.names() if self._policies.policy_for(name) is policy
            ]
            if not names:
                continue
            if policy is AccessPolicy.ADMIN:
                lines.append(f"{label}：" + "、".join(names) + "（发送 /<命令> help 查看用法）")
            else:
                lines.append(f"{label}：" + "、".join(self._command_usage(name) for name in names))
        lines.append("发送 /<命令> help 或 /help <命令> 查看用法与可选项。")
        return "\n".join(lines)

    @staticmethod
    def _command_usage(name: str) -> str:
        text = _COMMAND_HELP.get(name, "")
        first = text.splitlines()[0] if text else ""
        if first.startswith("用法："):
            return first[len("用法：") :].strip()
        return name

    @staticmethod
    def _wants_help(command: ParsedCommand) -> bool:
        return len(command.args) == 1 and command.args[0].lower() == "help"

    @staticmethod
    def _command_name(token: str) -> str:
        value = token.strip().lower()
        return value if value.startswith("/") else f"/{value}"

    def help_text(self, name: str) -> str:
        namespace = self._queries.for_command(name)
        if namespace is not None:
            return self._query_help(namespace)
        text = _COMMAND_HELP.get(name)
        if text is None:
            return f"没有找到命令 {name}。发送 /help 查看可用命令。"
        return text

    def _run_query(
        self,
        namespace: QueryNamespace,
        args: tuple[str, ...],
        _identity: MessageIdentity,
        _actor: PermissionSnapshot,
    ) -> str | CommandReply:
        if not args:
            raise CommandUsageError(
                f"用法：{namespace.command} <领域> [指令] [参数...]；"
                f"发送 {namespace.command} help 查看可用领域。"
            )
        target = namespace.resolve(args[0])
        if target is None:
            return f"没有找到领域 {args[0]}。发送 {namespace.command} help 查看可用领域。"
        rest = args[1:]
        if len(rest) == 1 and rest[0].lower() == "help":
            return self._query_target_help(namespace, target)
        if self._query_service is None:
            return "查询服务未启用，请联系管理员。"
        info = self._query_info(target)
        action, tokens = _split_action(info, rest)
        params = info.action_params(action) if info is not None else ()
        hint = f"发送 {namespace.command} {target.aliases[0]} help 查看用法与参数。"
        try:
            normalised = normalise_tokens(tokens)
        except ValueError:
            return f"参数过多。{hint}"
        values, error = validate_query_params(params, assign_query_params(params, normalised))
        if error:
            return f"{error}。{hint}"
        try:
            result = self._query_service.fetch(target.query_key, values)
        except QueryServiceResponseError as exc:
            return self._query_error_reply(exc, hint)
        except QueryServiceError:
            return "查询服务暂时不可用，请稍后再试。"
        return self._format_query_reply(result, target, filtered=any(values.values()))

    def _query_help(self, namespace: QueryNamespace) -> str:
        lines = [f"{namespace.command}：{namespace.display_name}"]
        if namespace.description:
            lines.append(namespace.description)
        lines.append(f"用法：{namespace.command} <领域> [指令] [参数...]")
        catalog = self._query_catalog_entries()
        lines.append("领域：")
        for target in namespace.targets:
            info = catalog.get(target.query_key) if catalog else None
            title = info.display_name if info is not None else target.query_key
            lines.append(f"- {'、'.join(target.aliases)}：{title}")
            if info is not None:
                summary = _param_summary(info)
                if summary:
                    lines.append(f"  参数：{summary}")
        if self._query_service is None:
            lines.append("（本部署未配置查询服务，查询请求会被拒绝）")
        elif catalog is None:
            lines.append("（查询服务暂时不可用，未能列出参数可选项）")
        example = namespace.targets[0].aliases[0] if namespace.targets[0].aliases else ""
        lines.append(f"示例：{namespace.command} {example}".rstrip())
        return "\n".join(lines)

    def _query_target_help(self, namespace: QueryNamespace, target: QueryTarget) -> str:
        info = self._query_info(target)
        alias = target.aliases[0] if target.aliases else target.query_key
        lines = [f"{namespace.command} {alias}：{info.display_name if info else target.query_key}"]
        if info is not None and info.description:
            lines.append(info.description)
        action = info.default_action() if info is not None else None
        params = info.action_params(action) if info is not None else ()
        label = (action.label or action.key) if action is not None else ""
        if label and info is not None and len(info.actions) > 1:
            lines.append(f"指令：{label}（默认），可选 {_action_summary(info)}")
        elif label:
            lines.append(f"指令：{label}（默认，可省略）")
        if params:
            lines.append(f"用法：{namespace.command} {alias} {query_usage(params)}")
            lines.append("参数：")
            for param in params:
                lines.append(f"- {_param_line(param)}")
                if param.type == "enum" and len(param.options) > 6:
                    lines.extend(f"  · {option.key}：{option.label}" for option in param.options)
            lines.append("也可写成「参数名=值」，例如 weapon=Dual Toxocyst。")
        else:
            lines.append("当前没有可筛选的参数，直接查询即可。")
            lines.append(f"用法：{namespace.command} {alias}")
        return "\n".join(lines)

    def _query_info(self, target: QueryTarget) -> QueryInfo | None:
        catalog = self._query_catalog_entries()
        return catalog.get(target.query_key) if catalog else None

    def _query_catalog_entries(self) -> dict[str, QueryInfo] | None:
        if self._query_service is None:
            return None
        try:
            return self._query_service.catalog()
        except QueryServiceError:
            return None

    @staticmethod
    def _query_error_reply(exc: QueryServiceResponseError, hint: str = "") -> str:
        if exc.status_code == 400:
            labels = "、".join(option.label for option in exc.options)
            detail = exc.message or "参数未被接受"
            base = f"{detail}。可选：{labels}。" if labels else f"{detail}。"
            return f"{base} {hint}".strip()
        if exc.status_code == 404 and exc.message:
            return f"{exc.message} {hint}".strip()
        if exc.status_code in {502, 503, 504}:
            return "上游数据源暂时不可用，请稍后再试。"
        if exc.status_code == 404:
            return "查询目标不存在，请联系管理员。"
        return "查询失败，请联系管理员。"

    def _format_query_reply(
        self, result: QueryResult, target: QueryTarget, *, filtered: bool
    ) -> str | CommandReply:
        """Render the answer for the current channel.

        A target configured with `reply: image_url` answers a plain query with a
        native image reply. A filtered query keeps the text form, because the
        image cannot show which tasks were selected, and a missing URL falls
        back to text instead of sending an empty message.
        """
        prefer_image = target.reply == "image_url_always" or (
            target.reply == "image_url" and not filtered
        )
        if prefer_image and result.image_url:
            return CommandReply(image_url=result.image_url)
        text = result.text.strip()
        if len(text) > self._query_reply_max_chars:
            text = text[: self._query_reply_max_chars] + "…"
        if not result.image_url:
            return text
        return f"{text}\n图片：{result.image_url}"

    def _target(self, token: str, identity: MessageIdentity) -> str:
        if token.startswith("@"):
            resolved = self._mention_resolver(token, identity)
            if resolved:
                return resolved
            raise CommandUsageError(
                "当前稳定消息契约无法解析该 @ 用户，请改用 pairing code 或 OpenID。"
            )
        if not _EXPLICIT_OPENID.fullmatch(token):
            raise CommandUsageError("目标 OpenID 格式无效。")
        return token

    @staticmethod
    def _permissions_arg(value: str) -> list[str]:
        try:
            return _DIMENSIONS[value.lower()]
        except KeyError as exc:
            raise CommandUsageError("权限维度必须明确指定 chat、command 或 all。") from exc

    @staticmethod
    def _expect(args: tuple[str, ...], count: int, usage: str) -> None:
        if len(args) != count:
            raise CommandUsageError(f"用法：{usage}")

    @staticmethod
    def _find_event(events: list[EventInfo], event_key: str) -> EventInfo | None:
        for item in events:
            if item.event_key == event_key:
                return item
        return None

    @staticmethod
    def _option_hint(event: EventInfo) -> str:
        if not event.match_key_options:
            return ""
        requirement = "必选" if event.match_keys_required else "可选"
        return f"（可关注 {len(event.match_key_options)} 项，{requirement}）"

    @staticmethod
    def _format_options(event: EventInfo) -> str:
        if not event.match_key_options:
            return f"{event.event_key}（{event.display_name}）没有可关注的子项，直接 /bind 即可。"
        requirement = "绑定时必须指定至少一项" if event.match_keys_required else "不指定表示全部"
        lines = [f"{event.event_key}（{event.display_name}）可关注任务，{requirement}："]
        lines.extend(f"- {option.key}：{option.label}" for option in event.match_key_options)
        lines.append(f"用法：/bind {event.event_key} <关注项[,关注项]>（任务键或中文名）")
        return "\n".join(lines)

    @staticmethod
    def _format_bind_result(result, event: EventInfo | None, requested: tuple[str, ...]) -> str:
        if event is None or (not event.match_key_options and not requested):
            return (
                f"已订阅 {result.event_key}。"
                if result.changed
                else f"已经订阅 {result.event_key}，无需重复操作。"
            )
        if requested:
            watched = "、".join(event.option_label(key) or key for key in requested)
        else:
            watched = "全部内容"
        if result.updated:
            return f"已更新 {result.event_key} 的关注项：{watched}。"
        if result.changed:
            return f"已订阅 {result.event_key}，关注 {watched}。"
        return f"已经订阅 {result.event_key}，关注项未变化：{watched}。"

    @staticmethod
    def _resolve_match_keys(
        event: EventInfo, tokens: tuple[str, ...]
    ) -> tuple[tuple[str, ...], list[str]]:
        """Map user tokens (catalogue key or label) to canonical keys."""
        resolved: list[str] = []
        unknown: list[str] = []
        for token in tokens:
            key = event.match_key_for(token)
            if key is None:
                unknown.append(token)
            elif key not in resolved:
                resolved.append(key)
        return tuple(resolved), unknown

    @staticmethod
    def _match_key_tokens(args: tuple[str, ...]) -> tuple[str, ...]:
        tokens: list[str] = []
        for raw in args:
            for part in raw.split(","):
                token = part.strip()
                if not token:
                    raise CommandUsageError("关注项不能为空，多个关注项用逗号分隔。")
                if token not in tokens:
                    tokens.append(token)
        if len(tokens) > _MAX_MATCH_KEYS:
            raise CommandUsageError(f"一次最多绑定 {_MAX_MATCH_KEYS} 个关注项。")
        return tuple(tokens)

    @staticmethod
    def _bind_scope_tokens(args: tuple[str, ...]) -> tuple[str, ...]:
        scopes: list[str] = []
        for raw in args:
            for part in raw.split(","):
                scope = part.strip()
                if not scope or len(scope) > 128 or not _BIND_SCOPE.fullmatch(scope):
                    raise CommandUsageError(
                        "bind scope 必须是 * 或小写命名空间前缀，例如 warframe.*。"
                    )
                if scope not in scopes:
                    scopes.append(scope)
        if len(scopes) > _MAX_BIND_SCOPES:
            raise CommandUsageError(f"一次最多处理 {_MAX_BIND_SCOPES} 个 bind scope。")
        return tuple(scopes)

    @staticmethod
    def _format_permission(snapshot: PermissionSnapshot, label: str) -> str:
        bind = ",".join(snapshot.bind) if snapshot.bind else "none"
        return (
            f"{label} status={snapshot.account_status} role={snapshot.role} "
            f"chat={str(snapshot.chat).lower()} command={str(snapshot.command).lower()} bind={bind}"
        )
