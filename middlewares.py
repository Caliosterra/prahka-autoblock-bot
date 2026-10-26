from typing import Callable, Dict, Any, Awaitable
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject


class AccessMiddleware(BaseMiddleware):
    def __init__(self, allowed_user_ids: list[int]):
        super().__init__()
        self.allowed_user_ids = allowed_user_ids

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user and (not self.allowed_user_ids or user.id in self.allowed_user_ids):
            return await handler(event, data)
        return None