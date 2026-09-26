import logging

from .command_policy import CommandPolicyRegistry, parse_command
from .commands import CommandService, CommandUsageError
from .eventserver_client import EventServerError
from .identity import IdentityError, extract_identity
from .models import CommandReply, DispatchDecision, MessageIdentity, PermissionSnapshot
from .observability import audit
from .permission_cache import PermissionCache
from .rate_limit import SlidingWindowRateLimiter

logger = logging.getLogger("autoqq.plugin")


class MessageProcessor:
    def __init__(
        self,
        client,
        policies: CommandPolicyRegistry,
        cache: PermissionCache,
        limiter: SlidingWindowRateLimiter,
        command_service: CommandService,
    ) -> None:
        self._client = client
        self._policies = policies
        self._cache = cache
        self._limiter = limiter
        self._commands = command_service

    def process(self, event: object) -> DispatchDecision:
        try:
            identity = extract_identity(event)
        except IdentityError:
            logger.warning("autoqq rejected message with missing trusted identity")
            return DispatchDecision("skip", "identity-invalid")
        if identity.platform != "qqbot":
            return DispatchDecision("allow", "platform-out-of-scope")
        command = parse_command(identity.text)
        if command is not None and (
            not command.name or self._policies.policy_for(command.name) is None
        ):
            audit(logger, "unknown-command", identity.platform, identity.openid, "denied")
            return DispatchDecision(
                "skip", "unknown-command", "未知或格式错误的命令。发送 /help 查看可用命令。"
            )
        try:
            actor = self._permission(identity)
            if command is not None:
                return self._process_command(identity, command, actor)
            return self._process_chat(identity, actor)
        except EventServerError as exc:
            audit(logger, "eventserver", identity.platform, identity.openid, exc.code)
            return DispatchDecision(
                "skip", "eventserver-unavailable", "权限服务暂时不可用，本次请求已安全拒绝。"
            )
        except Exception:
            logger.exception("autoqq fail-closed after unexpected processing error")
            return DispatchDecision(
                "skip", "internal-error", "服务暂时不可用，本次请求已安全拒绝。"
            )

    def _permission(self, identity: MessageIdentity) -> PermissionSnapshot:
        return self._cache.get_or_load(
            identity.platform,
            identity.openid,
            lambda: self._client.get_permission(identity.platform, identity.openid),
        )

    def _process_command(self, identity, command, actor) -> DispatchDecision:
        policy = self._policies.policy_for(command.name)
        if policy is None or not actor.allows(policy):
            audit(logger, command.name, identity.platform, identity.openid, "denied")
            return DispatchDecision("skip", "command-denied", "没有执行该命令的权限。")
        if not self._limiter.allow((identity.platform, identity.openid)):
            audit(logger, command.name, identity.platform, identity.openid, "rate-limited")
            return DispatchDecision("skip", "rate-limited", "操作过于频繁，请稍后再试。")
        try:
            result = self._commands.execute(command, identity, actor)
        except CommandUsageError as exc:
            result = str(exc)
        except EventServerError:
            raise
        audit(logger, command.name, identity.platform, identity.openid, "handled")
        mention_sender = command.name == "/bind" and identity.chat_type == "group"
        if isinstance(result, CommandReply):
            return DispatchDecision(
                "skip",
                "command-handled",
                result.text,
                image_url=result.image_url,
                mention_sender=mention_sender,
            )
        return DispatchDecision("skip", "command-handled", result, mention_sender=mention_sender)

    def _process_chat(
        self, identity: MessageIdentity, actor: PermissionSnapshot
    ) -> DispatchDecision:
        if actor.account_status == "active" and actor.chat:
            audit(logger, "chat", identity.platform, identity.openid, "allowed")
            return DispatchDecision("allow", "chat-authorized")
        if actor.account_status == "unknown":
            if not self._limiter.allow((f"pair:{identity.platform}", identity.openid)):
                return DispatchDecision(
                    "skip", "pairing-rate-limited", "申请过于频繁，请稍后再试。"
                )
            code, _expires = self._client.create_pairing_code(identity.platform, identity.openid)
            audit(logger, "pairing-code", identity.platform, identity.openid, "created")
            return DispatchDecision(
                "skip",
                "chat-unauthorized",
                f"尚未授权。请将一次性授权码 {code} 交给管理员；授权码约 10 分钟内有效。",
            )
        audit(logger, "chat", identity.platform, identity.openid, "denied")
        return DispatchDecision("skip", "chat-denied", "当前账号没有聊天权限。")
