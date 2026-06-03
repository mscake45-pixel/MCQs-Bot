import json
import logging
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from database import db

router = Router()
bot: Bot = None
logger = logging.getLogger(__name__)

class CreateTest(StatesGroup):
    waiting_for_name = State()
    waiting_for_intro = State()
    waiting_for_json = State()

@router.message(Command("create_test"))
async def start_test_creation(message: Message, state: FSMContext):
    await message.answer("<b>Step 1 — Test Name</b>\n\n📚 Enter any test name:", parse_mode="HTML")
    await state.set_state(CreateTest.waiting_for_name)

@router.message(CreateTest.waiting_for_name)
async def process_test_name(message: Message, state: FSMContext):
    name = message.text.strip()
    if not name:
        return await message.answer("❌ Name cannot be empty. Please enter a valid name.")
    await state.update_data(test_name=name)
    await message.answer(
        "<b>Step 2 — Intro Message</b>\n\n"
        "📝 Enter a message to show before the test starts.\n"
        "Send /skip if you want no intro.",
        parse_mode="HTML"
    )
    await state.set_state(CreateTest.waiting_for_intro)

@router.message(CreateTest.waiting_for_intro)
async def process_test_intro(message: Message, state: FSMContext):
    intro = None if message.text == "/skip" else message.text
    await state.update_data(intro_message=intro)
    
    example = '''[
  {
    "question": "What is the largest cranial nerve?",
    "options": {"A": "Facial", "B": "Trigeminal", "C": "Vagus", "D": "Glossopharyngeal", "E": "Accessory"},
    "correct": "B",
    "explanation": "The trigeminal nerve is the largest cranial nerve."
  }
]'''
    await message.answer(
        f"<b>Step 3 — Send your JSON file or paste JSON text</b>\n\n"
        f"✅ <b>Format requirements:</b>\n"
        f"• Top-level array of questions\n"
        f"• Each question has: <code>question</code>, <code>options</code> (object with A,B,C,D,E), <code>correct</code> (letter), <code>explanation</code> (string)\n"
        f"• Optional: <code>image_url</code> (ignored for now)\n\n"
        f"<b>Example:</b>\n<pre>{example}</pre>\n\n"
        f"📎 Send a <b>JSON file</b> or paste the JSON text directly.",
        parse_mode="HTML"
    )
    await state.set_state(CreateTest.waiting_for_json)

@router.message(CreateTest.waiting_for_json)
async def process_test_json(message: Message, state: FSMContext):
    raw_data = None
    
    # Handle file upload
    if message.document:
        # Limit file size to 5 MB
        if message.document.file_size > 5 * 1024 * 1024:
            return await message.answer("❌ File too large (max 5 MB).")
        try:
            file_info = await bot.get_file(message.document.file_id)
            file_content = await bot.download_file(file_info.file_path)
            raw_data = file_content.read().decode('utf-8')
            logger.info(f"JSON file received: {message.document.file_name}")
        except Exception as e:
            logger.error(f"File download error: {e}")
            return await message.answer(f"❌ Failed to read file: {str(e)[:100]}")
    
    elif message.text:
        raw_data = message.text.strip()
    
    else:
        return await message.answer("❌ Please send a JSON file or paste the JSON text.")
    
    # Parse JSON
    try:
        data = json.loads(raw_data)
    except json.JSONDecodeError as e:
        return await message.answer(
            f"❌ <b>Invalid JSON</b>\n\nError: {str(e)}\n\n"
            f"Please check your syntax. Use a validator like jsonlint.com",
            parse_mode="HTML"
        )
    
    # Validate structure
    if not isinstance(data, list):
        return await message.answer("❌ JSON must be an array of questions (starts with `[` and ends with `]`).")
    
    if len(data) == 0:
        return await message.answer("❌ Array is empty. Add at least one question.")
    
    user_data = await state.get_data()
    validated_questions = []
    
    for idx, q in enumerate(data):
        # Required fields
        if not isinstance(q, dict):
            return await message.answer(f"❌ Question {idx+1} is not a valid object.")
        if 'question' not in q or not str(q['question']).strip():
            return await message.answer(f"❌ Question {idx+1} missing 'question' field.")
        if 'options' not in q or not isinstance(q['options'], dict):
            return await message.answer(f"❌ Question {idx+1} missing 'options' object.")
        if 'correct' not in q or not str(q['correct']).strip():
            return await message.answer(f"❌ Question {idx+1} missing 'correct' field.")
        
        # Build options dictionary with exactly A,B,C,D,E (empty strings allowed)
        opt_dict = {'A': '', 'B': '', 'C': '', 'D': '', 'E': ''}
        valid_letters = []
        for letter in ['A','B','C','D','E']:
            val = q['options'].get(letter, '')
            if val and str(val).strip():
                opt_dict[letter] = str(val).strip()
                valid_letters.append(letter)
        
        if len(valid_letters) < 2:
            return await message.answer(
                f"❌ Question {idx+1} must have at least 2 non‑empty options (A‑E).\n"
                f"Found: {valid_letters}"
            )
        
        correct_letter = str(q['correct']).upper().strip()
        if correct_letter not in valid_letters:
            return await message.answer(
                f"❌ Question {idx+1}: correct option '{correct_letter}' not in valid options {valid_letters}."
            )
        
        explanation = q.get('explanation', 'No explanation provided.').strip()
        if not explanation:
            explanation = 'No explanation provided.'
        
        validated_questions.append({
            'question': q['question'].strip(),
            'options': opt_dict,
            'correct': correct_letter,
            'explanation': explanation
        })
    
    # Create test in database
    try:
        test_id, added = await db.create_test_with_questions(
            user_data['test_name'],
            user_data['intro_message'],
            message.from_user.id,
            validated_questions
        )
        await state.clear()
        await message.answer(
            f"✅ <b>Test created successfully!</b>\n\n"
            f"📚 Name: {user_data['test_name']}\n"
            f"📊 Questions added: {added}\n"
            f"🆔 Test ID: <code>{test_id}</code>\n\n"
            f"Use /tests to see your tests, or /start_test to run this quiz.",
            parse_mode="HTML"
        )
        logger.info(f"User {message.from_user.id} created test {test_id} with {added} questions")
    except Exception as e:
        logger.error(f"Database error during test creation: {e}", exc_info=True)
        await message.answer(
            f"❌ <b>Database error</b>\n\nCould not save test. Please try again later.\n"
            f"Error: {str(e)[:200]}",
            parse_mode="HTML"
        )

# ----------------------------------------------------------------------
# Other existing commands (list_tests, delete_test) remain unchanged
# ----------------------------------------------------------------------

@router.message(Command("tests"))
async def list_tests(message: Message):
    tests = await db.get_user_tests(message.from_user.id)
    if not tests:
        return await message.answer("📚 No tests found. Use /create_test to make one.")
    
    builder = InlineKeyboardBuilder()
    text = "📚 <b>Your Tests</b>\n\n"
    for test_id, name in tests:
        text += f"• <b>{name}</b> (ID: {test_id})\n"
        builder.button(text=f"▶️ {name}", callback_data=f"lobby_test_{test_id}")
    builder.adjust(1)
    text += "\nClick a button to start the quiz."
    await message.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")

@router.message(Command("delete_test"))
async def cmd_delete_test(message: Message):
    tests = await db.get_user_tests(message.from_user.id)
    if not tests:
        return await message.answer("📚 You have no tests to delete.")
    
    builder = InlineKeyboardBuilder()
    for test_id, name in tests:
        builder.button(text=f"🗑 {name}", callback_data=f"del_test_{test_id}")
    builder.adjust(1)
    await message.answer(
        "🗑 <b>Select a test to delete</b>\n<i>Warning: This deletes all stats and schedules.</i>",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )

@router.callback_query(F.data.startswith("del_test_"))
async def process_delete_test(callback: CallbackQuery):
    test_id = int(callback.data.split("_")[2])
    success = await db.delete_test(test_id, callback.from_user.id)
    if success:
        await callback.message.edit_text("✅ Test deleted successfully.")
    else:
        await callback.answer("❌ Failed to delete or you are not the owner.", show_alert=True)
