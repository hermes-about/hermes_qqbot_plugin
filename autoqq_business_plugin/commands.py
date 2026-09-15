import re
from collections.abc import Callable

from .command_policy import CommandPolicyRegistry
from .eventserver_client import EventServerClient
from .models import MessageIdentity, ParsedCommand, PermissionSnapshot
from .observability import mask_identifier
from .permission_cache import PermissionCache

_PAIRING_CODE = re.compile(r"^[ABCDEFGHJKLMNPQRSTUVWXYZ23456789]{8}$")
_EXPLICIT_OPENID = re.compile(r"^[A-Za-z0-9._:-]{6,128}$")
_DIMENSIONS = {"chat": ["chat"], "command": ["command"], "all": ["chat", "command"]}


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
            "/events\n/bind <event_key>\n/unbind <event_key>\n/bindings\n"
            "/whoami\n/help\n"
            "管理员：/grant <目标|pairing_code> chat|command|all、/revoke、"
            "/admin、/unadmin、/permissions、/userlist"
        )

    def _events(self, args: tuple[str, ...], *_: object) -> str:
        self._expect(args, 0, "/events")
        events = self._client.list_events()
        if not events:
            return "当前没有可订阅事件。"
        return "可订阅事件：\n" + "\n".join(
            f"- {item.event_key}：{item.display_name}" for item in events if not item.deprecated
        )

    def _whoami(
        self, args: tuple[str, ...], identity: MessageIdentity, actor: PermissionSnapshot
    ) -> str:
        self._expect(args, 0, "/whoami")
        return self._format_permission(actor, label=mask_identifier(identity.openid))

    def _bind(
        self, args: tuple[str, ...], identity: MessageIdentity, _actor: PermissionSnapshot
    ) -> str:
        self._expect(args, 1, "/bind <event_key>")
        result = self._client.subscribe(identity.platform, identity.openid, args[0])
        return (
            f"已订阅 {result.event_key}。"
            if result.changed
            else f"已经订阅 {result.event_key}，无需重复操作。"
        )

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
        return (
            "当前没有订阅。"
            if not items
            else "当前订阅：\n" + "\n".join(f"- {item.event_key}" for item in items)
        )

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
    def _format_permission(snapshot: PermissionSnapshot, label: str) -> str:
        return (
            f"{label} status={snapshot.account_status} role={snapshot.role} "
            f"chat={str(snapshot.chat).lower()} command={str(snapshot.command).lower()}"
        )
