import re
from collections.abc import Callable

from .command_policy import CommandPolicyRegistry
from .eventserver_client import EventServerClient, EventServerResponseError
from .models import EventInfo, MessageIdentity, ParsedCommand, PermissionSnapshot
from .observability import mask_identifier
from .permission_cache import PermissionCache

_PAIRING_CODE = re.compile(r"^[ABCDEFGHJKLMNPQRSTUVWXYZ23456789]{8}$")
_EXPLICIT_OPENID = re.compile(r"^[A-Za-z0-9._:-]{6,128}$")
_DIMENSIONS = {"chat": ["chat"], "command": ["command"], "all": ["chat", "command"]}
_MAX_MATCH_KEYS = 32


class CommandUsageError(ValueError):
    pass


class CommandService:
    def __init__(
        self,
        client: EventServerClient,
        cache: PermissionCache,
        policies: CommandPolicyRegistry,
        mention_resolver: Callable[[str, MessageIdentity], str | None] | None = None,
    ) -> None:
        self._client = client
        self._cache = cache
        self._policies = policies
        self._mention_resolver = mention_resolver or (lambda _token, _identity: None)

    def execute(
        self,
        command: ParsedCommand,
        identity: MessageIdentity,
        actor: PermissionSnapshot,
    ) -> str:
        handlers = {
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
        return handlers[command.name](command.args, identity, actor)

    def _help(self, args: tuple[str, ...], *_: object) -> str:
        self._expect(args, 0, "/help")
        return (
            "可用命令：\n"
            "/events\n/events <event_key>\n/bind <event_key> [关注项...]\n"
            "/unbind <event_key>\n/bindings\n"
            "/whoami\n/help\n"
            "管理员：/grant <目标|pairing_code> chat|command|all、/revoke、"
            "/admin、/unadmin、/permissions、/userlist"
        )

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
        requested = self._match_key_tokens(args[1:])
        event = self._find_event(self._client.list_events(), event_key)
        if event is not None:
            unknown = [key for key in requested if event.option_label(key) is None]
            if unknown:
                return (
                    f"关注项不在该事件的可选范围内：{unknown[0]}。"
                    f"发送 /events {event.event_key} 查看可选值。"
                )
            if event.match_keys_required and not requested:
                raise CommandUsageError(
                    f"该事件必须指定至少一个关注项。用法：/bind {event.event_key} <关注项[,关注项]>"
                )
        try:
            result = self._client.subscribe(
                identity.platform, identity.openid, event_key, requested
            )
        except EventServerResponseError as exc:
            if exc.status_code == 404:
                return f"没有找到事件 {event_key}。发送 /events 查看可用事件。"
            if exc.status_code == 400:
                return f"关注项未被接受。发送 /events {event_key} 查看可选值。"
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
        lines = []
        for item in items:
            if item.match_keys:
                lines.append(f"- {item.event_key}：关注 {'、'.join(item.match_keys)}")
            else:
                lines.append(f"- {item.event_key}：关注全部")
        return "当前订阅：\n" + "\n".join(lines)

    def _grant(
        self, args: tuple[str, ...], identity: MessageIdentity, _actor: PermissionSnapshot
    ) -> str:
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
        lines.append(f"用法：/bind {event.event_key} <关注项[,关注项]>")
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
    def _format_permission(snapshot: PermissionSnapshot, label: str) -> str:
        return (
            f"{label} status={snapshot.account_status} role={snapshot.role} "
            f"chat={str(snapshot.chat).lower()} command={str(snapshot.command).lower()}"
        )
