import logging
import random
import re
import threading
from collections.abc import Callable

from .eventserver_client import EventServerClient, EventServerError
from .models import DeliveryItem, SendOutcome
from .observability import mask_identifier

logger = logging.getLogger("autoqq.delivery")
_ERROR_CODE = re.compile(r"^[A-Z0-9_]{1,64}$")


class DeliveryWorker:
    def __init__(
        self,
        client: EventServerClient,
        sender,
        *,
        worker_id: str,
        platform: str = "qqbot",
        batch_size: int = 10,
        lease_seconds: int = 60,
        poll_interval_seconds: float = 5,
        max_backoff_seconds: float = 30,
        max_message_chars: int = 2000,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self._client = client
        self._sender = sender
        self._worker_id = worker_id
        self._platform = platform
        self._batch_size = batch_size
        self._lease_seconds = lease_seconds
        self._poll_interval = poll_interval_seconds
        self._max_backoff = max_backoff_seconds
        self._max_message_chars = max_message_chars
        self._jitter = jitter
        self._stop = threading.Event()
        self._start_lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        with self._start_lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                name=f"autoqq-delivery-{self._worker_id}",
                daemon=True,
            )
            self._thread.start()
            return True

    def stop(self, timeout: float = 10) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0, timeout))

    def run_once(self) -> int:
        items = self._client.claim_deliveries(
            self._worker_id, self._platform, self._batch_size, self._lease_seconds
        )
        for item in items:
            if self._stop.is_set():
                break
            self._handle(item)
        return len(items)

    def _run(self) -> None:
        backoff = self._poll_interval
        while not self._stop.is_set():
            try:
                count = self.run_once()
                backoff = (
                    self._poll_interval
                    if count
                    else min(self._max_backoff, max(self._poll_interval, backoff * 2))
                )
            except EventServerError:
                logger.warning("autoqq delivery poll could not reach EventServer")
                backoff = min(self._max_backoff, max(self._poll_interval, backoff * 2))
            except Exception:
                logger.exception("autoqq delivery poll failed without stopping the worker")
                backoff = min(self._max_backoff, max(self._poll_interval, backoff * 2))
            delay = backoff * (0.8 + self._jitter() * 0.4)
            self._stop.wait(delay)

    def _handle(self, item: DeliveryItem) -> None:
        outcome = self._validate(item)
        if outcome is None:
            try:
                outcome = self._sender.send_delivery(item)
            except Exception:
                logger.exception("autoqq delivery send raised unexpectedly")
                outcome = SendOutcome(False, error_code="HERMES_SEND_ERROR", retryable=True)
        try:
            if outcome.success:
                self._client.ack_delivery(item.delivery_id, item.lease_token, outcome.message_id)
                result = "acked"
            else:
                error_code = outcome.error_code
                if not _ERROR_CODE.fullmatch(error_code):
                    error_code = "SEND_ERROR"
                self._client.fail_delivery(
                    item.delivery_id, item.lease_token, error_code, outcome.retryable
                )
                result = "failed"
            logger.info(
                "autoqq delivery=%s target=%s result=%s",
                item.delivery_id,
                mask_identifier(item.openid),
                result,
            )
        except EventServerError:
            logger.warning(
                "autoqq delivery result writeback failed delivery=%s; lease will recover",
                item.delivery_id,
            )

    def _validate(self, item: DeliveryItem) -> SendOutcome | None:
        if item.platform != self._platform or not item.openid or len(item.openid) > 128:
            return SendOutcome(False, error_code="INVALID_TARGET", retryable=False)
        text = item.message.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > self._max_message_chars:
            return SendOutcome(False, error_code="INVALID_MESSAGE", retryable=False)
        return None
