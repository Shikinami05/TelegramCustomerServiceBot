from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TelegramSendResult:
    ok: bool
    message_id: int | None = None
    message_thread_id: int | None = None
    status_code: int | None = None
    description: str = ""
    retry_after: int | None = None

    def __bool__(self) -> bool:
        return self.ok


@dataclass(frozen=True, slots=True)
class ConversationClaimResult:
    status: str
    owner_admin_id: int | None

    def __bool__(self) -> bool:
        return self.status == "acquired"
