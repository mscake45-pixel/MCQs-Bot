import asyncio
import logging
import random
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from database import db
from shared.state import active_lobbies
import config

# Constants
LOBBY_TIMEOUT_SECONDS = 120
MIN_INTERVAL_SECONDS = 5
MAX_INTERVAL_SECONDS = 120
LEADERBOARD_DISPLAY_LIMIT = 10
MAX_MESSAGE_LENGTH = 4000

router = Router()
bot_instance = None

# --- FSM States ---
class StartTestSetup(StatesGroup):
    waiting_for_test = State()
    waiting_for_interval = State()
    waiting_for_shuffle = State()
    waiting_for_confirmation = State()

class ScheduleTestSetup(StatesGroup):
    waiting_for_test = State()
    waiting_for_datetime = State()
    waiting_for_interval = State()
    waiting_for_shuffle = State()

def truncate_text(text: str, max_length: int = MAX_MESSAGE_LENGTH) -> str:
    """Truncate text to fit within Telegram's message limit"""
    if len(text) <= max_length:
        return text
    return text[:max_length - 3] + "..."

def format_question_text(question: str, options: list, q_num: int, total: int) -> str:
    """Format question with truncation to avoid message length limits"""
    options_text = "\n\n<b>Options:</b>\n" + "\n".join([f"{letter}) {text}" for letter, text in options])
    question_text = f"❓ <b>Question {q_num} of {total}</b>\n\n{question}{options_text}"
    
    if len(question_text) > MAX_MESSAGE_LENGTH:
        available_for_question = MAX_MESSAGE_LENGTH - len(options_text) - 50
        truncated_question = question[:available_for_question] + "..."
        question_text = f"❓ <b>Question {q_num} of {total}</b>\n\n{truncated_question}{options_text}"
    
    return question_text

def format_leaderboard(scores: dict, title: str) -> str:
    """Format leaderboard text"""
    if not scores:
        return "😔 No one participated."
    
    sorted_scores = sorted(
        scores.values(), 
        key=lambda x: (-x['score'], -x.get('answered_count', 0))
    )[:LEADERBOARD_DISPLAY_LIMIT]
    
    text = f"{title}\n\n"
    
    for idx, s in enumerate(sorted_scores, 1):
        medal = ""
        if idx == 1:
            medal = "🥇 "
        elif idx == 2:
            medal = "🥈 "
        elif idx == 3:
            medal = "🥉 "
        else:
            medal = f"{idx}. "
        
        name = s['name']
        if name and not name.startswith('@'):
            name = f"@{name}"
        
        percentage = int((s['score'] / s['answered_count']) * 100) if s['answered_count'] > 0 else 0
        text += f"{medal}{name} – {s['score']}/{s['answered_count']} ({percentage}%)\n"
    
    return text

async def lobby_timeout_task(chat_id: int, bot: Bot):
    try:
        await asyncio.sleep(LOBBY_TIMEOUT_SECONDS)
        lobby = active_lobbies.get(chat_id)
        if lobby and lobby["status"] == "waiting":
            del active_lobbies[chat_id]
            await bot.send_message(chat_id, "⌛ Quiz cancelled. Not enough players joined in time.")
    except asyncio.CancelledError:
        pass

# ==================== 1. START TEST WITH LOBBY SYSTEM ====================

@router.message(Command("start_test"))
async def start_test_cmd(message: Message, state: FSMContext):
    tests = await db.get_user_tests(message.from_user.id)
    if not tests: 
        return await message.answer("📚 No tests found. Create one using /create_test")

    builder = InlineKeyboardBuilder()
    for test_id, name in tests: 
        builder.button(text=name, callback_data=f"lobby_test_{test_id}")
    builder.adjust(1)
    await message.answer("📚 Select a test:", reply_markup=builder.as_markup())
    await state.set_state(StartTestSetup.waiting_for_test)

@router.callback_query(StartTestSetup.waiting_for_test, F.data.startswith("lobby_test_"))
async def process_test_selection(callback: CallbackQuery, state: FSMContext):
    test_id = int(callback.data.split("_")[2])
    test_data = await db.get_test(test_id)
    questions = await db.get_test_questions(test_id)
    
    settings = await db.get_user_settings(callback.from_user.id)
    default_interval = settings.get('default_interval', MIN_INTERVAL_SECONDS)
    
    await state.update_data(
        test_id=test_id, 
        test_name=test_data['name'], 
        q_count=len(questions), 
        default_interval=default_interval,
        shuffle=False
    )
    await callback.message.edit_text(
        f"⏱ <b>Time per question</b> (in seconds):\n\n"
        f"<i>Send a number ({MIN_INTERVAL_SECONDS}-{MAX_INTERVAL_SECONDS}) or /default to use {default_interval}s</i>",
        parse_mode="HTML"
    )
    await state.set_state(StartTestSetup.waiting_for_interval)
    await callback.answer()

@router.message(StartTestSetup.waiting_for_interval)
async def process_interval(message: Message, state: FSMContext):
    data = await state.get_data()
    
    if message.text.lower() == "/default":
        interval = data['default_interval']
    elif message.text.isdigit():
        interval = int(message.text)
        if interval < MIN_INTERVAL_SECONDS:
            return await message.answer(f"❌ Minimum interval is {MIN_INTERVAL_SECONDS} seconds.")
        if interval > MAX_INTERVAL_SECONDS:
            return await message.answer(f"❌ Maximum interval is {MAX_INTERVAL_SECONDS} seconds.")
    else:
        return await message.answer("❌ Please enter a valid number or send /default.")
    
    await state.update_data(interval=interval)
    
    builder = InlineKeyboardBuilder()
    builder.button(text="🔀 Shuffle: OFF", callback_data="toggle_shuffle")
    builder.button(text="✅ Next", callback_data="shuffle_next")
    builder.adjust(1)
    
    await message.answer(
        f"🔀 <b>Shuffle questions?</b>\n\n"
        f"Current: <b>OFF</b>\n\n"
        f"Toggle the setting, then press Next.",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await state.set_state(StartTestSetup.waiting_for_shuffle)

@router.callback_query(StartTestSetup.waiting_for_shuffle, F.data == "toggle_shuffle")
async def toggle_shuffle(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    current = data.get('shuffle', False)
    await state.update_data(shuffle=not current)
    
    shuffle_status = "ON" if not current else "OFF"
    builder = InlineKeyboardBuilder()
    builder.button(text=f"🔀 Shuffle: {shuffle_status}", callback_data="toggle_shuffle")
    builder.button(text="✅ Next", callback_data="shuffle_next")
    builder.adjust(1)
    
    await callback.message.edit_text(
        f"🔀 <b>Shuffle questions?</b>\n\n"
        f"Current: <b>{shuffle_status}</b>\n\n"
        f"Toggle the setting, then press Next.",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await callback.answer(f"Shuffle turned {shuffle_status}")

@router.callback_query(StartTestSetup.waiting_for_shuffle, F.data == "shuffle_next")
async def shuffle_next(callback: CallbackQuery, state: FSMContext):
    await callback.message.delete()
    
    data = await state.get_data()
    test_name = data['test_name']
    q_count = data['q_count']
    interval = data['interval']
    shuffle = data.get('shuffle', False)
    
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Start Quiz", callback_data="confirm_start_yes")
    builder.button(text="❌ Cancel", callback_data="confirm_start_no")
    
    await callback.message.answer(
        f"📚 <b>{test_name}</b>\n"
        f"📊 {q_count} questions\n"
        f"⏱ {interval} seconds per question\n"
        f"🔀 Shuffle: {'ON' if shuffle else 'OFF'}\n\n"
        f"<i>Click 'Start Quiz' to begin. Players must press 'I'm Ready' to join!</i>",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await state.set_state(StartTestSetup.waiting_for_confirmation)
    await callback.answer()

@router.callback_query(StartTestSetup.waiting_for_confirmation, F.data == "confirm_start_no")
async def cancel_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("❌ Quiz start cancelled.")
    await state.clear()
    await callback.answer()

@router.callback_query(StartTestSetup.waiting_for_confirmation, F.data == "confirm_start_yes")
async def confirm_and_create_lobby(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    
    chat_id = callback.message.chat.id
    is_group = callback.message.chat.type in ['group', 'supergroup']
    min_players = 2 if is_group else 1

    try:
        run_id = await db.create_quiz_run(data['test_id'], chat_id)
    except Exception as e:
        logging.error(f"Failed to create quiz run: {e}")
        return await callback.message.answer("❌ Failed to start quiz. Please try again.")

    active_lobbies[chat_id] = {
        "run_id": run_id,
        "test_id": data['test_id'],
        "test_name": data['test_name'],
        "q_count": data['q_count'],
        "interval": data['interval'],
        "shuffle": data.get('shuffle', False),
        "ready_users": set(),
        "min_players": min_players,
        "status": "waiting",
        "scores": {},
        "current_question_idx": -1,
        "question_active": False,
        "lock": asyncio.Lock(),
        "starting_lock": asyncio.Lock(),
        "created_at": datetime.now().timestamp()
    }

    lobby_text = (
        f"🎲 <b>Quiz Lobby: {data['test_name']}</b>\n\n"
        f"📊 {data['q_count']} questions\n"
        f"⏱ {data['interval']} seconds per question\n"
        f"🔀 Shuffle: {'ON' if data.get('shuffle', False) else 'OFF'}\n\n"
        f"👥 <b>Players Ready: 0/{min_players}</b>\n\n"
        f"Click 'I'm Ready' to join the quiz!"
    )
    
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ I'm Ready", callback_data="lobby_ready")
    await callback.message.edit_text(lobby_text, reply_markup=builder.as_markup(), parse_mode="HTML")
    
    active_lobbies[chat_id]["timeout_task"] = asyncio.create_task(lobby_timeout_task(chat_id, callback.bot))
    await callback.answer()

@router.callback_query(F.data == "lobby_ready")
async def process_lobby_ready(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    lobby = active_lobbies.get(chat_id)
    
    if not lobby or lobby["status"] != "waiting":
        return await callback.answer("❌ Lobby no longer active.", show_alert=True)
    
    user_id = callback.from_user.id
    username = callback.from_user.username or callback.from_user.first_name
    
    async with lobby['lock']:
        if user_id in lobby["ready_users"]:
            return await callback.answer("✅ You're already ready!", show_alert=True)
        
        lobby["ready_users"].add(user_id)
        ready_count = len(lobby["ready_users"])
        min_players = lobby["min_players"]
        
        lobby_text = (
            f"🎲 <b>Quiz Lobby: {lobby['test_name']}</b>\n\n"
            f"📊 {lobby['q_count']} questions\n"
            f"⏱ {lobby['interval']} seconds per question\n"
            f"🔀 Shuffle: {'ON' if lobby['shuffle'] else 'OFF'}\n\n"
            f"👥 <b>Players Ready: {ready_count}/{min_players}</b>\n\n"
            f"Click 'I'm Ready' to join the quiz!"
        )
        
        builder = InlineKeyboardBuilder()
        builder.button(text="✅ I'm Ready", callback_data="lobby_ready")
        
        await callback.message.edit_text(lobby_text, reply_markup=builder.as_markup(), parse_mode="HTML")
        
        if ready_count >= min_players and lobby["status"] == "waiting":
            async with lobby.get('starting_lock', asyncio.Lock()):
                if lobby["status"] == "waiting":
                    lobby["status"] = "starting"
                    if "timeout_task" in lobby:
                        lobby["timeout_task"].cancel()
                    
                    await callback.message.answer("🎉 Minimum players reached! Starting quiz...")
                    asyncio.create_task(run_countdown(callback.message.chat.id, callback.bot))
    
    await callback.answer(f"✅ Ready! ({ready_count}/{min_players})")

# ==================== 2. COUNTDOWN ANIMATION ====================

async def run_countdown(chat_id: int, bot: Bot):
    lobby = active_lobbies.get(chat_id)
    if not lobby:
        return
    
    msg = await bot.send_message(chat_id, "🎮 Starting in...")
    countdown_steps = ["5️⃣", "4️⃣", "3️⃣", "2️⃣ READY?", "1️⃣ SET", "🚀 GO!"]
    
    for step in countdown_steps:
        await msg.edit_text(step)
        await asyncio.sleep(1)
    
    lobby["status"] = "running"
    asyncio.create_task(run_question_loop(chat_id, bot))

# ==================== 3. QUIZ ENGINE ====================

async def run_question_loop(chat_id: int, bot: Bot):
    lobby = active_lobbies.get(chat_id)
    if not lobby:
        return

    questions = await db.get_test_questions(lobby['test_id'])
    if lobby.get('shuffle', False):
        random.shuffle(questions)
    
    option_map = {'A': 0, 'B': 1, 'C': 2, 'D': 3, 'E': 4}

    for idx, q in enumerate(questions):
        lobby = active_lobbies.get(chat_id)
        if not lobby or lobby.get("status") != "running":
            break

        options = [
            ('A', q['option_a']), ('B', q['option_b']), 
            ('C', q['option_c']), ('D', q['option_d']), 
            ('E', q['option_e'])
        ]
        valid_options = [(letter, text) for letter, text in options if text and str(text).strip()]
        
        question_text = format_question_text(q['question'], valid_options, idx + 1, len(questions))
        
        builder = InlineKeyboardBuilder()
        for letter, text in valid_options:
            builder.button(text=letter, callback_data=f"ans_{lobby['test_id']}_{idx}_{option_map[letter]}")
        builder.adjust(len(valid_options))
        
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=question_text,
                reply_markup=builder.as_markup(),
                parse_mode="HTML"
            )
        except Exception as e:
            logging.error(f"Failed to send question {idx+1}: {e}")
            await bot.send_message(
                chat_id=chat_id,
                text=f"❓ Question {idx + 1} of {len(questions)}\n\n{q['question']}",
                reply_markup=builder.as_markup()
            )
        
        async with lobby['lock']:
            lobby['current_question_idx'] = idx
            lobby['question_active'] = True
            lobby['current_correct_idx'] = option_map.get(q['correct_option'].upper(), 0)
            lobby['current_correct_letter'] = q['correct_option'].upper()
            lobby['current_correct_answer'] = q[f'option_{q["correct_option"].lower()}']
            lobby['current_question_text'] = q['question']
            lobby['current_explanation'] = q.get('explanation', 'No explanation provided.')
        
        await asyncio.sleep(lobby['interval'])
        
        lobby = active_lobbies.get(chat_id)
        if not lobby or lobby.get("status") != "running":
            break
        
        async with lobby['lock']:
            if lobby.get('question_active', False):
                lobby['question_active'] = False
                
                correct_letter = lobby['current_correct_letter']
                correct_answer = lobby['current_correct_answer']
                explanation = lobby['current_explanation']
                
                if len(explanation) > 500:
                    explanation = explanation[:497] + "..."
                
                reveal_text = (
                    f"⏰ <b>Time's up for question {idx + 1}!</b>\n\n"
                    f"✅ <b>Correct answer:</b>\n"
                    f"<blockquote><tg-spoiler>{correct_letter}. {correct_answer}</tg-spoiler></blockquote>\n"
                    f"📚 <b>Explanation:</b>\n"
                    f"<blockquote><tg-spoiler>{explanation}</tg-spoiler></blockquote>"
                )
                await bot.send_message(chat_id, reveal_text, parse_mode="HTML")
                await asyncio.sleep(2)

    lobby = active_lobbies.get(chat_id)
    if lobby and lobby.get("status") == "running":
        await finish_quiz(chat_id, bot)

# ==================== 4. ANSWER PROCESSING ====================

@router.callback_query(F.data.startswith("ans_"))
async def process_answer(callback: CallbackQuery):
    _, test_id, q_idx, opt_idx = callback.data.split("_")
    test_id, q_idx, opt_idx = int(test_id), int(q_idx), int(opt_idx)
    user_id = callback.from_user.id
    chat_id = callback.message.chat.id
    username = callback.from_user.username or callback.from_user.first_name
    
    lobby = active_lobbies.get(chat_id)
    
    is_live = 0
    is_correct = False
    correct_answer_text = ""
    correct_letter = ""
    question_text = ""
    
    if lobby and lobby.get("status") in ["running", "waiting"]:
        is_live = 1
        run_id = lobby.get('run_id', 0)
        
        async with lobby['lock']:
            if 'answered_questions' not in lobby:
                lobby['answered_questions'] = {}
            
            if user_id not in lobby['answered_questions']:
                lobby['answered_questions'][user_id] = set()
            
            if q_idx in lobby['answered_questions'][user_id]:
                await callback.answer("You already answered this question in this quiz session!", show_alert=True)
                return
            
            lobby['answered_questions'][user_id].add(q_idx)
            
            if lobby.get('current_question_idx') == q_idx:
                correct_letter = lobby.get('current_correct_letter', '?')
                correct_answer_text = lobby.get('current_correct_answer', 'Unknown')
                question_text = lobby.get('current_question_text', 'Question')
                
                if opt_idx == lobby.get('current_correct_idx', -1):
                    is_correct = True
            else:
                questions = await db.get_test_questions(test_id)
                if q_idx < len(questions):
                    q = questions[q_idx]
                    option_map = {'A': 0, 'B': 1, 'C': 2, 'D': 3, 'E': 4}
                    correct_idx = option_map.get(q['correct_option'].upper(), 0)
                    is_correct = (opt_idx == correct_idx)
                    correct_letter = q['correct_option'].upper()
                    correct_answer_text = q[f'option_{correct_letter.lower()}']
                    question_text = q['question']
            
            if user_id not in lobby['scores']:
                lobby['scores'][user_id] = {
                    "name": username,
                    "score": 0,
                    "answered_count": 0
                }
            
            lobby['scores'][user_id]['answered_count'] += 1
            if is_correct:
                lobby['scores'][user_id]['score'] += 1
    
    else:
        questions = await db.get_test_questions(test_id)
        if q_idx < len(questions):
            q = questions[q_idx]
            option_map = {'A': 0, 'B': 1, 'C': 2, 'D': 3, 'E': 4}
            correct_idx = option_map.get(q['correct_option'].upper(), 0)
            is_correct = (opt_idx == correct_idx)
            correct_letter = q['correct_option'].upper()
            correct_answer_text = q[f'option_{correct_letter.lower()}']
            question_text = q['question']
    
    already_answered_global = await db.has_user_answered_question_globally(user_id, test_id, q_idx)
    
    if not already_answered_global:
        run_id = lobby.get('run_id', 0) if lobby and is_live else 0
        await db.save_user_answer(
            user_id=user_id,
            test_id=test_id,
            run_id=run_id,
            question_idx=q_idx,
            is_correct=1 if is_correct else 0,
            is_live=is_live
        )
        
        try:
            if is_correct:
                feedback_text = f"✅ <b>Correct!</b>\n\n📝 {question_text[:100]}...\n\n🏆 +1 point to your <b>Global Score</b>!"
                if is_live:
                    feedback_text += f"\n🎯 Also counted for this <b>Quiz Session</b>!"
                await callback.answer("✅ Correct! +1 point", show_alert=False)
            else:
                feedback_text = f"❌ <b>Incorrect</b>\n\n📝 {question_text[:100]}...\n✅ Correct answer: <b>{correct_letter}. {correct_answer_text}</b>"
                if is_live:
                    feedback_text += f"\n⚠️ Your answer was recorded for this Quiz Session."
                await callback.answer(f"❌ Incorrect. Answer: {correct_letter}", show_alert=False)
            
            await callback.bot.send_message(user_id, feedback_text, parse_mode="HTML")
        except Exception as e:
            logging.debug(f"Could not send feedback: {e}")
    else:
        if is_live:
            await callback.answer(f"{'✅ Correct! ' if is_correct else '❌ Incorrect. '}Counted for this session! (Global already earned)", show_alert=False)
            try:
                if is_correct:
                    feedback_text = f"✅ <b>Correct!</b>\n\n📝 {question_text[:100]}...\n\n🎯 Counted for this <b>Quiz Session</b>!\nℹ️ You already earned the global point for this question before."
                else:
                    feedback_text = f"❌ <b>Incorrect</b>\n\n📝 {question_text[:100]}...\n✅ Correct answer: <b>{correct_letter}. {correct_answer_text}</b>\n\n⚠️ Recorded for this Quiz Session (no global point)."
                await callback.bot.send_message(user_id, feedback_text, parse_mode="HTML")
            except Exception:
                pass
        else:
            await callback.answer("You already answered this question before! Check your stats with /my_stats", show_alert=True)

# ==================== 5. FINISH QUIZ ====================

async def finish_quiz(chat_id: int, bot: Bot):
    lobby = active_lobbies.get(chat_id)
    if not lobby:
        return
    
    scores = lobby.get('scores', {})
    
    if not scores:
        lb_text = "😔 No one participated in this quiz."
    else:
        lb_text = format_leaderboard(scores, "🏆 <b>Quiz Results - Live Session</b>")
        lb_text += f"\n📊 Total participants: {len(scores)}"
    
    lb_text += "\n\n💡 <i>Use /leaderboard to see Global Rankings across all quizzes!</i>"
    
    await bot.send_message(chat_id, lb_text, parse_mode="HTML")
    
    if chat_id in active_lobbies:
        del active_lobbies[chat_id]

# ==================== 6. SCHEDULING SYSTEM (with Riyadh timezone) ====================

@router.message(Command("schedule_test"))
async def start_schedule_cmd(message: Message, state: FSMContext):
    tests = await db.get_user_tests(message.from_user.id)
    if not tests:
        return await message.answer("📚 No tests found. Create one using /create_test")
    
    builder = InlineKeyboardBuilder()
    for test_id, name in tests:
        builder.button(text=name, callback_data=f"sched_test_{test_id}")
    builder.adjust(1)
    await message.answer("📚 Select a test to schedule:", reply_markup=builder.as_markup())
    await state.set_state(ScheduleTestSetup.waiting_for_test)

@router.callback_query(ScheduleTestSetup.waiting_for_test, F.data.startswith("sched_test_"))
async def process_schedule_test_selection(callback: CallbackQuery, state: FSMContext):
    test_id = int(callback.data.split("_")[2])
    test_data = await db.get_test(test_id)
    await state.update_data(test_id=test_id, test_name=test_data['name'])
    await callback.message.edit_text(
        "📅 Enter date and time (YYYY-MM-DD HH:MM) in **Riyadh time**:\n"
        "<i>Example: 2026-12-31 15:30</i>",
        parse_mode="HTML"
    )
    await state.set_state(ScheduleTestSetup.waiting_for_datetime)
    await callback.answer()

@router.message(ScheduleTestSetup.waiting_for_datetime)
async def process_schedule_datetime(message: Message, state: FSMContext):
    try:
        # 1. Parse user input as naive datetime (Riyadh local time)
        user_naive_dt = datetime.strptime(message.text, "%Y-%m-%d %H:%M")
        
        # 2. Get the local timezone from config (e.g., 'Asia/Riyadh')
        local_tz = ZoneInfo(config.TIMEZONE)
        
        # 3. Make the datetime timezone-aware in the local timezone
        local_aware_dt = user_naive_dt.replace(tzinfo=local_tz)
        
        # 4. Convert to UTC
        utc_dt = local_aware_dt.astimezone(timezone.utc)
        
        # 5. Check if UTC time is in the future
        if utc_dt <= datetime.now(timezone.utc):
            return await message.answer("❌ Please enter a future date and time (in Riyadh time).")
        
        # 6. Store the UTC datetime for scheduling
        await state.update_data(run_date=utc_dt)
        
        # Continue with interval and shuffle
        settings = await db.get_user_settings(message.from_user.id)
        default_interval = settings.get('default_interval', MIN_INTERVAL_SECONDS)
        await state.update_data(default_interval=default_interval)
        
        await message.answer(
            f"⏱ Enter time per question (in seconds):\n"
            f"<i>Send a number or /default to use {default_interval}s</i>",
            parse_mode="HTML"
        )
        await state.set_state(ScheduleTestSetup.waiting_for_interval)
    except ValueError:
        await message.answer("❌ Invalid format. Please use YYYY-MM-DD HH:MM")

@router.message(ScheduleTestSetup.waiting_for_interval)
async def process_schedule_interval(message: Message, state: FSMContext):
    data = await state.get_data()
    
    if message.text.lower() == "/default":
        interval = data['default_interval']
    elif message.text.isdigit():
        interval = int(message.text)
        if interval < MIN_INTERVAL_SECONDS:
            return await message.answer(f"❌ Minimum interval is {MIN_INTERVAL_SECONDS} seconds.")
        if interval > MAX_INTERVAL_SECONDS:
            return await message.answer(f"❌ Maximum interval is {MAX_INTERVAL_SECONDS} seconds.")
    else:
        return await message.answer("❌ Please enter a valid number or send /default.")
    
    await state.update_data(interval=interval)
    await message.answer("🔀 Shuffle questions? (yes/no)")
    await state.set_state(ScheduleTestSetup.waiting_for_shuffle)

@router.message(ScheduleTestSetup.waiting_for_shuffle)
async def process_schedule_shuffle(message: Message, state: FSMContext, scheduler: AsyncIOScheduler, bot: Bot):
    data = await state.get_data()
    shuffle = message.text.lower() in ['yes', 'y', 'true', '1']
    
    # Store the UTC datetime as ISO string in database
    sched_id = await db.create_schedule(
        message.chat.id, 
        data['test_id'], 
        data['run_date'].isoformat(),   # store full UTC ISO string
        data['interval'], 
        shuffle
    )
    
    # Add job to scheduler with UTC run_date and misfire grace time
    scheduler.add_job(
        trigger_scheduled_test, 
        'date', 
        run_date=data['run_date'],   # timezone-aware UTC datetime
        args=[message.chat.id, data['test_id'], data['interval'], shuffle, bot, sched_id], 
        id=f"test_job_{sched_id}",
        misfire_grace_time=60
    )
    
    # Convert UTC back to local time for display to user
    local_tz = ZoneInfo(config.TIMEZONE)
    local_dt = data['run_date'].astimezone(local_tz)
    
    await state.clear()
    await message.answer(
        f"✅ <b>Quiz scheduled successfully!</b>\n\n"
        f"📚 {data['test_name']}\n"
        f"📅 {local_dt.strftime('%Y-%m-%d %H:%M')} (Riyadh time)\n"
        f"⏱ {data['interval']}s per question\n"
        f"🔀 Shuffle: {'ON' if shuffle else 'OFF'}\n\n"
        f"Use /schedules to view or /cancel_schedule {sched_id} to cancel.",
        parse_mode="HTML"
    )

async def trigger_scheduled_test(chat_id: int, test_id: int, interval: int, shuffle: bool, bot: Bot, sched_id: int):
    logging.info(f"🔥 Scheduled job FIRED for chat {chat_id}, test {test_id}, schedule ID {sched_id}")
    try:
        test_data = await db.get_test(test_id)
        questions = await db.get_test_questions(test_id)
        is_group = chat_id < 0
        min_players = 2 if is_group else 1
        
        run_id = await db.create_quiz_run(test_id, chat_id)
        
        active_lobbies[chat_id] = {
            "run_id": run_id,
            "test_id": test_id,
            "test_name": test_data['name'],
            "q_count": len(questions),
            "interval": interval,
            "shuffle": shuffle,
            "ready_users": set(),
            "min_players": min_players,
            "status": "waiting",
            "scores": {},
            "current_question_idx": -1,
            "question_active": False,
            "lock": asyncio.Lock(),
            "starting_lock": asyncio.Lock(),
            "created_at": datetime.now().timestamp()
        }
        
        lobby_text = (
            f"⏰ <b>Scheduled Quiz: {test_data['name']}</b>\n\n"
            f"📊 {len(questions)} questions\n"
            f"⏱ {interval} seconds per question\n"
            f"🔀 Shuffle: {'ON' if shuffle else 'OFF'}\n\n"
            f"👥 <b>Players Ready: 0/{min_players}</b>\n\n"
            f"Click 'I'm Ready' to join the quiz!"
        )
        
        builder = InlineKeyboardBuilder()
        builder.button(text="✅ I'm Ready", callback_data="lobby_ready")
        await bot.send_message(chat_id, lobby_text, reply_markup=builder.as_markup(), parse_mode="HTML")
        
        active_lobbies[chat_id]["timeout_task"] = asyncio.create_task(lobby_timeout_task(chat_id, bot))
        
    except Exception as e:
        logging.error(f"Scheduled test trigger failed: {e}")
    finally:
        await db.delete_schedule(sched_id, chat_id)

@router.message(Command("schedules"))
async def view_schedules(message: Message):
    schedules = await db.get_schedules(message.chat.id)
    if not schedules:
        return await message.answer("📅 No scheduled tests for this chat.")
    
    text = "📅 <b>Scheduled Tests</b>\n\n"
    local_tz = ZoneInfo(config.TIMEZONE)
    for s in schedules:
        # Convert stored UTC string to local time for display
        utc_dt = datetime.fromisoformat(s['run_date'])
        local_dt = utc_dt.replace(tzinfo=timezone.utc).astimezone(local_tz)
        text += f"📚 <b>{s['name']}</b>\n"
        text += f"   🕒 {local_dt.strftime('%Y-%m-%d %H:%M')} (Riyadh time)\n"
        text += f"   🆔 ID: {s['id']}\n\n"
    
    text += "Use <code>/cancel_schedule [ID]</code> to cancel."
    await message.answer(text, parse_mode="HTML")

@router.message(Command("cancel_schedule"))
async def cancel_schedule_cmd(message: Message, command: CommandObject, scheduler: AsyncIOScheduler):
    if not command.args or not command.args.isdigit():
        return await message.answer("❌ Usage: /cancel_schedule [ID]\n\nUse /schedules to see IDs.")
    
    sched_id = int(command.args)
    job_id = f"test_job_{sched_id}"
    
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    
    await db.delete_schedule(sched_id, message.chat.id)
    await message.answer("✅ Schedule cancelled successfully.")

# ==================== 7. EMERGENCY STOP ====================

@router.message(Command("stop"))
async def stop_test_cmd(message: Message):
    if message.chat.id in active_lobbies:
        lobby = active_lobbies[message.chat.id]
        if "timeout_task" in lobby:
            lobby["timeout_task"].cancel()
        del active_lobbies[message.chat.id]
        await message.answer("🛑 <b>Quiz stopped by administrator.</b>", parse_mode="HTML")
    else:
        await message.answer("❌ No active quiz in this chat.")

# ==================== 8. RESET USER STATE ====================

@router.message(Command("cancel"))
async def cancel_command(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state:
        await state.clear()
        await message.answer("✅ Operation cancelled. You can start over with /start_test or /create_test.")
    else:
        await message.answer("ℹ️ You don't have any active operation to cancel.")
