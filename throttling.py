import time
from typing import Any, Awaitable, Callable, Dict
from aiogram import BaseMiddleware
from aiogram.types import Message

class ThrottlingMiddleware(BaseMiddleware):
    def __init__(self, limit: float = 1.0):
        self.limit = limit
        self.users = {}

    async def __call__(
        self,
        handler: Callable[[Message, Dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: Dict[str, Any]
    ) -> Any:
        if not isinstance(event, Message):
            return await handler(event, data)
            
        user_id = event.from_user.id
        now = time.time()
        
        if user_id in self.users and (now - self.users[user_id]) < self.limit:
            return 
            
        self.users[user_id] = now
        return await handler(event, data)