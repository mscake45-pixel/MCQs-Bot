from aiogram import Router
from aiogram.filters import Command, BaseFilter
from aiogram.types import Message
from config import ADMIN_IDS
from shared.state import active_lobbies  # ✅ FIXED

router = Router()

class IsAdmin(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        return message.from_user.id in ADMIN_IDS

router.message.filter(IsAdmin())

@router.message(Command("admin"))
async def cmd_admin_panel(message: Message):
    from config import DB_PATH
    import aiosqlite
    
    async with aiosqlite.connect(DB_PATH) as conn:
        users = await (await conn.execute("SELECT COUNT(*) FROM users")).fetchone()
        tests = await (await conn.execute("SELECT COUNT(*) FROM tests")).fetchone()
        answers = await (await conn.execute("SELECT COUNT(*) FROM user_answers")).fetchone()
        live_answers = await (await conn.execute("SELECT COUNT(*) FROM user_answers WHERE is_live=1")).fetchone()
        active_lobbies_count = len(active_lobbies)  # ✅ FIXED

    stats_text = (
        "👑 <b>Administrator Control Panel</b>\n\n"
        f"👥 Total Users: {users[0]}\n"
        f"📚 Total Tests Created: {tests[0]}\n"
        f"🎯 Total Answers Submitted: {answers[0]}\n"
        f"🔥 Live Session Answers: {live_answers[0]}\n"
        f"🎮 Active Lobbies: {active_lobbies_count}\n"
    )
    await message.answer(stats_text, parse_mode="HTML")