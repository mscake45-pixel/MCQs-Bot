import asyncio
import logging
import random
import time
from datetime import datetime
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from database import db
from shared.state import active_lobbies

LOBBY_TIMEOUT_SECONDS = 120
MIN_INTERVAL_SECONDS = 5
MAX_INTERVAL_SECONDS = 120

router = Router()
bot_instance = None

class StartTestStates(StatesGroup):
    selecting_test = State()
    setting_interval = State()
    confirming = State()

class ScheduleTestStates(StatesGroup):
    selecting_test = State()
    setting_datetime = State()
    setting_interval = State()
    setting_shuffle = State()

# ==================== 1. TESTS COMMAND ====================
@router.message(Command("tests"))
async def cmd_tests(message: Message):
    tests = await db.get_user_tests(message.from_user.id)
    if not tests:
        return await message.answer("📚 No tests found. Create one using /create_test")
    builder = InlineKeyboardBuilder()
    text = "📚 <b>Your Tests</b>\n\n"
    for test_id, name in tests:
        text += f"• <b>{name}</b> (ID: {test_id})\n"
        builder.button(text=f"▶️ {name}", callback_data=f"start_test_{test_id}")
    builder.adjust(1)
    text += "\nClick a button to start the quiz:"
    await message.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")

# ==================== 2. START TEST ====================
@router.message(Command("start_test"))
async def cmd_start_test(message: Message, state: FSMContext):
    tests = await db.get_user_tests(message.from_user.id)
    if not tests:
        return await message.answer("📚 No tests found. Create one using /create_test")
    builder = InlineKeyboardBuilder()
    for test_id, name in tests:
        builder.button(text=name, callback_data=f"start_test_{test_id}")
    builder.adjust(1)
    await message.answer("📚 <b>Select a test:</b>", reply_markup=builder.as_markup(), parse_mode="HTML")
    await state.set_state(StartTestStates.selecting_test)

@router.callback_query(StartTestStates.selecting_test, F.data.startswith("start_test_"))
async def process_test_selection(callback: CallbackQuery, state: FSMContext):
    test_id = int(callback.data.split("_")[2])
    test_data = await db.get_test(test_id)
    questions = await db.get_test_questions(test_id)
    settings = await db.get_user_settings(callback.from_user.id)
    default_interval = settings.get('default_interval', 30)
    await state.update_data(
        test_id=test_id,
        test_name=test_data['name'],
        q_count=len(questions),
        default_interval=default_interval
    )
    await callback.message.edit_text(
        f"📚 <b>{test_data['name']}</b>\n"
        f"📊 {len(questions)} questions\n\n"
        f"⏱ <b>Time per question</b> (seconds):\n"
        f"<i>Send a number ({MIN_INTERVAL_SECONDS}-{MAX_INTERVAL_SECONDS}) or /default for {default_interval}s</i>",
        parse_mode="HTML"
    )
    await state.set_state(StartTestStates.setting_interval)
    await callback.answer()

@router.message(StartTestStates.setting_interval)
async def process_interval(message: Message, state: FSMContext):
    data = await state.get_data()
    if message.text.lower() == "/default":
        interval = data['default_interval']
    elif message.text.isdigit():
        interval = int(message.text)
        if interval < MIN_INTERVAL_SECONDS:
            return await message.answer(f"❌ Minimum is {MIN_INTERVAL_SECONDS} seconds.")
        if interval > MAX_INTERVAL_SECONDS:
            return await message.answer(f"❌ Maximum is {MAX_INTERVAL_SECONDS} seconds.")
    else:
        return await message.answer("❌ Send a number or /default")
    await state.update_data(interval=interval)
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Start Quiz", callback_data="confirm_start")
    builder.button(text="❌ Cancel", callback_data="cancel_start")
    await message.answer(
        f"📚 <b>{data['test_name']}</b>\n"
        f"📊 {data['q_count']} questions\n"
        f"⏱ {interval} seconds per question\n\n"
        f"<i>Click Start Quiz to begin. Players must press 'I'm Ready' to join!</i>",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await state.set_state(StartTestStates.confirming)

@router.callback_query(StartTestStates.confirming, F.data == "cancel_start")
async def cancel_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("❌ Quiz cancelled.")
    await state.clear()
    await callback.answer()

@router.callback_query(StartTestStates.confirming, F.data == "confirm_start")
async def confirm_start(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    chat_id = callback.message.chat.id
    is_group = callback.message.chat.type in ['group', 'supergroup']
    min_players = 2 if is_group else 1
    run_id = await db.create_quiz_run(data['test_id'], chat_id)
    active_lobbies[chat_id] = {
        "run_id": run_id,
        "test_id": data['test_id'],
        "test_name": data['test_name'],
        "q_count": data['q_count'],
        "interval": data['interval'],
        "ready_users": set(),
        "min_players": min_players,
        "status": "waiting",
        "scores": {},
        "current_idx": -1,
        "question_active": False,
        "lock": asyncio.Lock(),
        "created_at": time.time(),
        "answered": {}
    }
    lobby_text = (
        f"🎲 <b>Quiz Lobby: {data['test_name']}</b>\n\n"
        f"📊 {data['q_count']} questions\n"
        f"⏱ {data['interval']} seconds per question\n\n"
        f"👥 <b>Players Ready: 0/{min_players}</b>\n\n"
        f"Click 'I'm Ready' to join!"
    )
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ I'm Ready", callback_data="lobby_ready")
    await callback.message.edit_text(lobby_text, reply_markup=builder.as_markup(), parse_mode="HTML")
    active_lobbies[chat_id]["timeout_task"] = asyncio.create_task(lobby_timeout(chat_id, callback.bot))
    await callback.answer("Lobby created! Waiting for players...")

# ==================== 3. LOBBY READY ====================
@router.callback_query(F.data == "lobby_ready")
async def handle_lobby_ready(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    lobby = active_lobbies.get(chat_id)
    if not lobby:
        await callback.message.delete()
        return await callback.answer("❌ Lobby expired. Start a new quiz.", show_alert=True)
    if lobby["status"] != "waiting":
        return await callback.answer("❌ Quiz already started!", show_alert=True)
    user_id = callback.from_user.id
    username = callback.from_user.username or callback.from_user.first_name
    async with lobby['lock']:
        if user_id in lobby["ready_users"]:
            return await callback.answer("You're already ready!", show_alert=True)
        lobby["ready_users"].add(user_id)
        ready_count = len(lobby["ready_users"])
        min_players = lobby["min_players"]
        lobby_text = (
            f"🎲 <b>Quiz Lobby: {lobby['test_name']}</b>\n\n"
            f"📊 {lobby['q_count']} questions\n"
            f"⏱ {lobby['interval']} seconds per question\n\n"
            f"👥 <b>Players Ready: {ready_count}/{min_players}</b>\n\n"
            f"Click 'I'm Ready' to join!"
        )
        builder = InlineKeyboardBuilder()
        builder.button(text="✅ I'm Ready", callback_data="lobby_ready")
        await callback.message.edit_text(lobby_text, reply_markup=builder.as_markup(), parse_mode="HTML")
        if ready_count >= min_players:
            lobby["status"] = "starting"
            if "timeout_task" in lobby:
                lobby["timeout_task"].cancel()
            await callback.message.answer("🎉 Enough players! Starting quiz...")
            asyncio.create_task(start_countdown(chat_id, callback.bot))
    await callback.answer(f"Ready! ({ready_count}/{min_players})")

async def lobby_timeout(chat_id: int, bot: Bot):
    await asyncio.sleep(LOBBY_TIMEOUT_SECONDS)
    lobby = active_lobbies.get(chat_id)
    if lobby and lobby["status"] == "waiting":
        del active_lobbies[chat_id]
        await bot.send_message(chat_id, "⌛ Quiz cancelled. Not enough players joined.")

async def start_countdown(chat_id: int, bot: Bot):
    lobby = active_lobbies.get(chat_id)
    if not lobby:
        return
    msg = await bot.send_message(chat_id, "🎮 Starting in...")
    steps = ["5️⃣.....", "4️⃣....", "3️⃣...", "2️⃣ READY?", "1️⃣ SET", "🚀 GO!"]
    for step in steps:
        await msg.edit_text(step)
        await asyncio.sleep(1)
    lobby["status"] = "running"
    asyncio.create_task(run_quiz_loop(chat_id, bot))

# ==================== 4. QUIZ LOOP ====================
async def run_quiz_loop(chat_id: int, bot: Bot):
    lobby = active_lobbies.get(chat_id)
    if not lobby:
        return
    questions = await db.get_test_questions(lobby['test_id'])
    option_map = {'A': 0, 'B': 1, 'C': 2, 'D': 3, 'E': 4}
    for idx, q in enumerate(questions):
        lobby = active_lobbies.get(chat_id)
        if not lobby or lobby["status"] != "running":
            break
        options = [
            ('A', q['option_a']), ('B', q['option_b']),
            ('C', q['option_c']), ('D', q['option_d']),
            ('E', q['option_e'])
        ]
        valid_options = [(l, t) for l, t in options if t and str(t).strip()]
        options_text = "\n\n<b>Options:</b>\n" + "\n".join([f"{l}) {t}" for l, t in valid_options])
        question_text = f"❓ <b>Question {idx + 1} of {len(questions)}</b>\n\n{q['question']}{options_text}"
        if len(question_text) > 4000:
            question_text = question_text[:3997] + "..."
        builder = InlineKeyboardBuilder()
        for letter, text in valid_options:
            builder.button(text=letter, callback_data=f"quiz_ans_{lobby['test_id']}_{idx}_{option_map[letter]}")
        builder.adjust(len(valid_options))
        await bot.send_message(chat_id, question_text, reply_markup=builder.as_markup(), parse_mode="HTML")
        async with lobby['lock']:
            lobby['current_idx'] = idx
            lobby['question_active'] = True
            lobby['correct_idx'] = option_map.get(q['correct_option'].upper(), 0)
            lobby['correct_letter'] = q['correct_option'].upper()
            lobby['correct_answer'] = q[f'option_{q["correct_option"].lower()}']
            lobby['question_text'] = q['question']
            lobby['explanation'] = q.get('explanation', 'No explanation')
        await asyncio.sleep(lobby['interval'])
        lobby = active_lobbies.get(chat_id)
        if lobby and lobby.get('question_active', False):
            async with lobby['lock']:
                lobby['question_active'] = False
                reveal = (
                    f"⏰ <b>Time's up for question {idx + 1}!</b>\n\n"
                    f"✅ <b>Correct:</b> "
                    f"<blockquote expandable><tg-spoiler>{lobby['correct_letter']}. {lobby['correct_answer']}</tg-spoiler></blockquote>\n\n"
                    f"📚 <b>Explanation:</b>\n"
                    f"<blockquote expandable><tg-spoiler>{lobby['explanation']}</tg-spoiler></blockquote>"
                )
                await bot.send_message(chat_id, reveal, parse_mode="HTML")
                await asyncio.sleep(2)
    lobby = active_lobbies.get(chat_id)
    if lobby:
        await show_leaderboard(chat_id, bot)

# ==================== 5. ANSWER HANDLER – ALL LIVE ANSWERS COUNT (FIXED) ====================
@router.callback_query(F.data.startswith("quiz_ans_"))
async def handle_answer(callback: CallbackQuery):
    parts = callback.data.split("_")
    if len(parts) != 5:
        await callback.answer("Invalid answer format.", show_alert=True)
        return
    test_id = int(parts[2])
    q_idx = int(parts[3])
    opt_idx = int(parts[4])
    user_id = callback.from_user.id
    chat_id = callback.message.chat.id
    username = callback.from_user.username or callback.from_user.first_name

    lobby = active_lobbies.get(chat_id)
    is_live = 0
    is_correct = False
    correct_letter = "?"
    correct_answer_text = ""
    question_text = ""

    # --- Live quiz answer: ANY answer while the quiz is running (any question) ---
    if lobby and lobby.get("status") == "running":
        async with lobby['lock']:
            is_live = 1   # This answer counts for live session leaderboard

            # Prevent double answering the same question in this session
            if 'answered' not in lobby:
                lobby['answered'] = {}
            if user_id not in lobby['answered']:
                lobby['answered'][user_id] = set()
            if q_idx in lobby['answered'][user_id]:
                await callback.answer("You already answered this question!", show_alert=True)
                return
            lobby['answered'][user_id].add(q_idx)

            # Determine correctness – use lobby data if it's the current question,
            # otherwise fetch from database.
            if lobby.get('current_idx') == q_idx:
                # Current question: fast data from lobby
                correct_letter = lobby.get('correct_letter', '?')
                correct_answer_text = lobby.get('correct_answer', 'Unknown')
                question_text = lobby.get('question_text', 'Question')
                is_correct = (opt_idx == lobby.get('correct_idx', -1))
            else:
                # Old question (user is catching up) – fetch from DB
                questions = await db.get_test_questions(test_id)
                if q_idx < len(questions):
                    q = questions[q_idx]
                    option_map = {'A': 0, 'B': 1, 'C': 2, 'D': 3, 'E': 4}
                    correct_idx = option_map.get(q['correct_option'].upper(), 0)
                    is_correct = (opt_idx == correct_idx)
                    correct_letter = q['correct_option'].upper()
                    correct_answer_text = q[f'option_{correct_letter.lower()}']
                    question_text = q['question']
                else:
                    await callback.answer("Question not found.", show_alert=True)
                    return

            # Update live session scores (for BOTH current and old questions)
            if 'scores' not in lobby:
                lobby['scores'] = {}
            if user_id not in lobby['scores']:
                lobby['scores'][user_id] = {"name": username[:50], "score": 0}
            if is_correct:
                lobby['scores'][user_id]['score'] += 1

    else:
        # --- Late answer (quiz finished) – only global, no live scoring ---
        questions = await db.get_test_questions(test_id)
        if q_idx < len(questions):
            q = questions[q_idx]
            option_map = {'A': 0, 'B': 1, 'C': 2, 'D': 3, 'E': 4}
            correct_idx = option_map.get(q['correct_option'].upper(), 0)
            is_correct = (opt_idx == correct_idx)
            correct_letter = q['correct_option'].upper()
            correct_answer_text = q[f'option_{correct_letter.lower()}']
            question_text = q['question']
        else:
            await callback.answer("Question not found.", show_alert=True)
            return

    # --- Global leaderboard: always save every answer (no duplicate prevention across runs) ---
    run_id = lobby.get('run_id', 0) if lobby and is_live else 0
    await db.save_user_answer(
        user_id, test_id, run_id, q_idx,
        1 if is_correct else 0,
        is_live
    )

    # Popup feedback only (no private messages)
    if is_correct:
        await callback.answer("✅ Correct! +1 point", show_alert=False)
    else:
        await callback.answer(f"❌ Incorrect. Answer: {correct_letter}", show_alert=False)

async def show_leaderboard(chat_id: int, bot: Bot):
    lobby = active_lobbies.get(chat_id)
    if not lobby:
        return
    scores = lobby.get('scores', {})
    if not scores:
        await bot.send_message(chat_id, "🏁 <b>Quiz Finished!</b>\n\n😔 No one participated.", parse_mode="HTML")
    else:
        sorted_scores = sorted(scores.values(), key=lambda x: -x['score'])
        text = "🏆 <b>Quiz Results - Live Session</b>\n\n"
        for idx, s in enumerate(sorted_scores[:10], 1):
            medal = "🥇 " if idx == 1 else "🥈 " if idx == 2 else "🥉 " if idx == 3 else f"{idx}. "
            name = s['name']
            if name and not name.startswith('@'):
                name = f"@{name}"
            text += f"{medal}{name} – {s['score']} correct\n"
        text += f"\n📊 Total players: {len(scores)}"
        await bot.send_message(chat_id, text, parse_mode="HTML")
    if chat_id in active_lobbies:
        del active_lobbies[chat_id]

# ==================== 6. SCHEDULING ====================
@router.message(Command("schedule_test"))
async def cmd_schedule_test(message: Message, state: FSMContext):
    tests = await db.get_user_tests(message.from_user.id)
    if not tests:
        return await message.answer("📚 No tests found. Create one using /create_test")
    builder = InlineKeyboardBuilder()
    for test_id, name in tests:
        builder.button(text=name, callback_data=f"schedule_{test_id}")
    builder.adjust(1)
    await message.answer("📚 <b>Select a test to schedule:</b>", reply_markup=builder.as_markup(), parse_mode="HTML")
    await state.set_state(ScheduleTestStates.selecting_test)

@router.callback_query(ScheduleTestStates.selecting_test, F.data.startswith("schedule_"))
async def process_schedule_test(callback: CallbackQuery, state: FSMContext):
    test_id = int(callback.data.split("_")[1])
    test_data = await db.get_test(test_id)
    await state.update_data(test_id=test_id, test_name=test_data['name'])
    await callback.message.edit_text(
        "📅 <b>Enter date and time</b>\n\n"
        "Format: <code>YYYY-MM-DD HH:MM</code>\n"
        "Example: <code>2025-12-31 15:30</code>\n\n"
        "Send the date:",
        parse_mode="HTML"
    )
    await state.set_state(ScheduleTestStates.setting_datetime)
    await callback.answer()

@router.message(ScheduleTestStates.setting_datetime)
async def process_schedule_datetime(message: Message, state: FSMContext):
    try:
        dt = datetime.strptime(message.text.strip(), "%Y-%m-%d %H:%M")
        if dt < datetime.now():
            return await message.answer("❌ Please enter a future date and time.")
        await state.update_data(run_date=dt)
        settings = await db.get_user_settings(message.from_user.id)
        default_interval = settings.get('default_interval', 30)
        await message.answer(
            f"⏱ <b>Time per question</b>\n\n"
            f"Send a number ({MIN_INTERVAL_SECONDS}-{MAX_INTERVAL_SECONDS}) or /default for {default_interval}s",
            parse_mode="HTML"
        )
        await state.set_state(ScheduleTestStates.setting_interval)
    except ValueError:
        await message.answer("❌ Invalid format. Use: YYYY-MM-DD HH:MM")

@router.message(ScheduleTestStates.setting_interval)
async def process_schedule_interval(message: Message, state: FSMContext):
    data = await state.get_data()
    if message.text.lower() == "/default":
        interval = data.get('default_interval', 30)
    elif message.text.isdigit():
        interval = int(message.text)
        if interval < MIN_INTERVAL_SECONDS or interval > MAX_INTERVAL_SECONDS:
            return await message.answer(f"❌ Interval must be {MIN_INTERVAL_SECONDS}-{MAX_INTERVAL_SECONDS} seconds")
    else:
        return await message.answer("❌ Send a number or /default")
    await state.update_data(interval=interval)
    await message.answer(
        "🔀 <b>Shuffle questions?</b>\n\n"
        "Send <code>yes</code> or <code>no</code>",
        parse_mode="HTML"
    )
    await state.set_state(ScheduleTestStates.setting_shuffle)

@router.message(ScheduleTestStates.setting_shuffle)
async def process_schedule_shuffle(message: Message, state: FSMContext, scheduler: AsyncIOScheduler):
    data = await state.get_data()
    shuffle = message.text.lower() in ['yes', 'y', 'true', '1']
    sched_id = await db.create_schedule(
        message.chat.id,
        data['test_id'],
        f"job_{int(time.time())}",
        data['run_date'].strftime("%Y-%m-%d %H:%M:%S"),
        data['interval'],
        shuffle
    )
    scheduler.add_job(
        trigger_scheduled_test,
        'date',
        run_date=data['run_date'],
        args=[message.chat.id, data['test_id'], data['interval'], shuffle, sched_id],
        id=f"sched_{sched_id}"
    )
    await state.clear()
    await message.answer(
        f"✅ <b>Quiz Scheduled!</b>\n\n"
        f"📚 {data['test_name']}\n"
        f"📅 {data['run_date'].strftime('%Y-%m-%d %H:%M')}\n"
        f"⏱ {data['interval']}s per question\n"
        f"🔀 Shuffle: {'ON' if shuffle else 'OFF'}\n\n"
        f"Use /schedules to view or /cancel_schedule {sched_id} to cancel",
        parse_mode="HTML"
    )

@router.message(Command("schedules"))
async def cmd_schedules(message: Message):
    schedules = await db.get_schedules(message.chat.id)
    if not schedules:
        return await message.answer("📅 No scheduled quizzes.\n\nUse /schedule_test to create one.")
    text = "📅 <b>Scheduled Quizzes</b>\n\n"
    for s in schedules:
        text += f"📚 <b>{s['name']}</b>\n"
        text += f"🕒 {s['run_date']}\n"
        text += f"🆔 ID: <code>{s['id']}</code>\n\n"
    text += "Cancel: <code>/cancel_schedule [ID]</code>"
    await message.answer(text, parse_mode="HTML")

@router.message(Command("cancel_schedule"))
async def cmd_cancel_schedule(message: Message, command: CommandObject, scheduler: AsyncIOScheduler):
    if not command.args:
        return await message.answer("❌ Usage: /cancel_schedule [ID]\n\nUse /schedules to see IDs.")
    try:
        sched_id = int(command.args)
        try:
            scheduler.remove_job(f"sched_{sched_id}")
        except:
            pass
        await db.delete_schedule(sched_id, message.chat.id)
        await message.answer(f"✅ Schedule #{sched_id} cancelled.")
    except ValueError:
        await message.answer("❌ Invalid ID. Use a number.")

async def trigger_scheduled_test(chat_id: int, test_id: int, interval: int, shuffle: bool, sched_id: int):
    bot = bot_instance
    if not bot:
        logging.error("Bot instance not set for scheduled quiz")
        return
    try:
        if interval < MIN_INTERVAL_SECONDS or interval > MAX_INTERVAL_SECONDS:
            interval = max(MIN_INTERVAL_SECONDS, min(interval, MAX_INTERVAL_SECONDS))
        test_data = await db.get_test(test_id)
        questions = await db.get_test_questions(test_id)
        if shuffle:
            random.shuffle(questions)
        is_group = chat_id < 0
        min_players = 2 if is_group else 1
        run_id = await db.create_quiz_run(test_id, chat_id)
        active_lobbies[chat_id] = {
            "run_id": run_id,
            "test_id": test_id,
            "test_name": test_data['name'],
            "q_count": len(questions),
            "interval": interval,
            "ready_users": set(),
            "min_players": min_players,
            "status": "waiting",
            "scores": {},
            "current_idx": -1,
            "question_active": False,
            "lock": asyncio.Lock(),
            "created_at": time.time(),
            "answered": {}
        }
        lobby_text = (
            f"⏰ <b>Scheduled Quiz: {test_data['name']}</b>\n\n"
            f"📊 {len(questions)} questions\n"
            f"⏱ {interval} seconds per question\n\n"
            f"👥 <b>Players Ready: 0/{min_players}</b>\n\n"
            f"Click 'I'm Ready' to join!"
        )
        builder = InlineKeyboardBuilder()
        builder.button(text="✅ I'm Ready", callback_data="lobby_ready")
        await bot.send_message(chat_id, lobby_text, reply_markup=builder.as_markup(), parse_mode="HTML")
        active_lobbies[chat_id]["timeout_task"] = asyncio.create_task(lobby_timeout(chat_id, bot))
    except Exception as e:
        logging.error(f"Scheduled quiz error: {e}")
    finally:
        await db.delete_schedule(sched_id, chat_id)

# ==================== 7. UTILITIES ====================
@router.message(Command("stop"))
async def cmd_stop(message: Message):
    from config import ADMIN_IDS
    if message.from_user.id not in ADMIN_IDS:
        return await message.answer("❌ Only admins can use /stop")
    if message.chat.id in active_lobbies:
        lobby = active_lobbies[message.chat.id]
        if "timeout_task" in lobby:
            lobby["timeout_task"].cancel()
        del active_lobbies[message.chat.id]
        await message.answer("🛑 Quiz stopped by admin.")
    else:
        await message.answer("❌ No active quiz.")

@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    if await state.get_state():
        await state.clear()
        await message.answer("✅ Operation cancelled.")
    else:
        await message.answer("ℹ️ No active operation to cancel.")