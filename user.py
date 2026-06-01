import logging
import asyncio
from aiogram import Router, F, Bot
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from database import db
import aiosqlite
from config import DB_PATH

router = Router()
logger = logging.getLogger(__name__)

@router.message(Command("start"))
async def cmd_start(message: Message, command: CommandObject, bot: Bot):
    await db.add_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    if command.args and command.args.startswith("test_"):
        try:
            test_id = int(command.args.split("_")[1])
            test_data = await db.get_test(test_id)
            questions = await db.get_test_questions(test_id)
            if test_data:
                text = (
                    f"📚 <b>{test_data['name']}</b>\n\n"
                    f"{test_data.get('intro_message', '')}\n\n"
                    f"🖊 {len(questions)} Questions\n\n"
                    f"🔗 External Sharing Link:\n"
                    f"<code>t.me/{(await bot.get_me()).username}?start=test_{test_id}</code>"
                )
                builder = InlineKeyboardBuilder()
                builder.button(text="▶ Start Test", callback_data=f"start_test_{test_id}")
                builder.adjust(1)
                return await message.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")
        except Exception:
            pass
    welcome_text = (
        f"👋 <b>Welcome to Nanatomy Quiz Bot!</b>\n\n"
        f"You can:\n"
        f"📚 Take live and scheduled quizzes\n"
        f"🏆 Compete on the Global Leaderboard\n"
        f"🔗 Share test links with friends\n\n"
        f"Use /help to view all available commands."
    )
    await message.answer(welcome_text, parse_mode="HTML")

@router.message(Command("help"))
async def cmd_help(message: Message):
    help_text = """
📚 <b>Available Commands</b>

<b>🎯 Quiz Taking:</b>
/start_test - Start a live quiz with lobby system
/schedule_test - Schedule a quiz for later
/schedules - View upcoming scheduled quizzes
/cancel_schedule [ID] - Cancel a scheduled quiz

<b>📝 Test Creation:</b>
/create_test - Create a new test
/tests - View your tests
/delete_test - Delete a test

<b>📊 Leaderboards:</b>
/my_stats - View your global statistics
/leaderboard - View global ranking across all quizzes
/resetstats - Delete ALL your global answers (warning: irreversible)

<b>⚙️ Other:</b>
/settings - Configure preferences
/cancel - Cancel current operation
/stop - Emergency stop (admin only)

💡 <i>Every answer you give counts toward your global score, even in repeated quizzes!</i>
"""
    await message.answer(help_text, parse_mode="HTML")

@router.message(Command("my_stats"))
async def cmd_my_stats(message: Message):
    total, correct = await db.get_user_global_stats(message.from_user.id)
    accuracy = int((correct / total) * 100) if total > 0 else 0
    text = (
        f"📊 <b>Your Global Statistics</b>\n\n"
        f"📚 Total answers: {total}\n"
        f"✅ Correct answers: {correct}\n"
        f"📈 Accuracy: {accuracy}%\n\n"
        f"💡 Every answer counts, even in repeated quizzes!"
    )
    await message.answer(text, parse_mode="HTML")

@router.message(Command("leaderboard"))
async def cmd_leaderboard(message: Message):
    try:
        results = await db.get_global_leaderboard(limit=10)
        if not results:
            return await message.answer("🏆 No quiz results yet. Be the first to participate!")
        text = "🌍 <b>Global Leaderboard</b>\n\n"
        text += "💡 <i>All correct answers count, even from repeated quizzes!</i>\n\n"
        for idx, (user_id, total_answered, correct_answers) in enumerate(results, 1):
            async with aiosqlite.connect(DB_PATH) as conn:
                async with conn.execute("SELECT username, first_name FROM users WHERE user_id = ?", (user_id,)) as cur:
                    user = await cur.fetchone()
            name = user[0] or user[1] if user else f"User_{user_id}"
            if name and not name.startswith('@'):
                name = f"@{name}"
            medal = "🥇 " if idx == 1 else "🥈 " if idx == 2 else "🥉 " if idx == 3 else f"{idx}. "
            accuracy = int((correct_answers / total_answered) * 100) if total_answered > 0 else 0
            text += f"{medal}{name} – {correct_answers}/{total_answered} ({accuracy}%)\n"
        await message.answer(text, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Leaderboard error: {e}")
        await message.answer("❌ Error loading leaderboard. Please try again later.")

@router.message(Command("resetstats"))
async def cmd_reset_stats(message: Message):
    user_id = message.from_user.id
    count = await db.get_user_answer_count(user_id)
    if count == 0:
        return await message.answer("ℹ️ You have no stats to reset.")
    confirm_msg = await message.answer(
        f"⚠️ <b>WARNING</b>\n\n"
        f"You have {count} answers in the global leaderboard.\n"
        f"This will delete ALL your answers permanently.\n\n"
        f"Type <code>CONFIRM</code> to proceed.\n"
        f"(You have 30 seconds)",
        parse_mode="HTML"
    )
    try:
        response = await asyncio.wait_for(
            message.chat.wait_for(
                lambda m: m.from_user.id == user_id and m.text == "CONFIRM"
            ),
            timeout=30.0
        )
        deleted = await db.reset_user_stats(user_id)
        await response.delete()
        await confirm_msg.edit_text(f"✅ Your stats have been reset. {deleted} answers deleted.")
    except asyncio.TimeoutError:
        await confirm_msg.edit_text("❌ Reset cancelled (timeout).")

@router.message(Command("settings"))
async def cmd_settings(message: Message):
    settings = await db.get_user_settings(message.from_user.id)
    notif_text = "ON" if settings['notifications_enabled'] else "OFF"
    interval = settings['default_interval']
    builder = InlineKeyboardBuilder()
    builder.button(text=f"🔔 Notifications: {notif_text}", callback_data="toggle_notif")
    builder.button(text=f"⏱ Default Interval: {interval}s", callback_data="edit_interval")
    builder.adjust(1)
    await message.answer("⚙ <b>Settings</b>\n\nConfigure your bot preferences:", reply_markup=builder.as_markup(), parse_mode="HTML")

@router.callback_query(F.data == "toggle_notif")
async def process_toggle_notif(callback: CallbackQuery):
    settings = await db.get_user_settings(callback.from_user.id)
    new_status = 0 if settings['notifications_enabled'] else 1
    await db.update_user_setting(callback.from_user.id, "notifications_enabled", new_status)
    notif_text = "ON" if new_status else "OFF"
    builder = InlineKeyboardBuilder()
    builder.button(text=f"🔔 Notifications: {notif_text}", callback_data="toggle_notif")
    builder.button(text=f"⏱ Default Interval: {settings['default_interval']}s", callback_data="edit_interval")
    builder.adjust(1)
    await callback.message.edit_reply_markup(reply_markup=builder.as_markup())
    await callback.answer("Notifications toggled!")

@router.callback_query(F.data == "edit_interval")
async def process_edit_interval(callback: CallbackQuery):
    settings = await db.get_user_settings(callback.from_user.id)
    new_interval = 30 if settings['default_interval'] == 15 else 45 if settings['default_interval'] == 30 else 60 if settings['default_interval'] == 45 else 15
    await db.update_user_setting(callback.from_user.id, "default_interval", new_interval)
    notif_text = "ON" if settings['notifications_enabled'] else "OFF"
    builder = InlineKeyboardBuilder()
    builder.button(text=f"🔔 Notifications: {notif_text}", callback_data="toggle_notif")
    builder.button(text=f"⏱ Default Interval: {new_interval}s", callback_data="edit_interval")
    builder.adjust(1)
    await callback.message.edit_reply_markup(reply_markup=builder.as_markup())
    await callback.answer(f"Default interval changed to {new_interval}s!")