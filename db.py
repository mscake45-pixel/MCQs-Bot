import aiosqlite
import asyncio
from datetime import datetime
import os
from contextlib import asynccontextmanager
from config import DB_PATH

# Ensure database directory exists
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

# -------------------------------------------------------------------
# Connection Pool
# -------------------------------------------------------------------
class DatabasePool:
    """Simple connection pool for SQLite to handle concurrent access"""
    def __init__(self, db_path: str, pool_size: int = 5):
        self.db_path = db_path
        self.pool_size = pool_size
        self._pool = asyncio.Queue(maxsize=pool_size)
        self._initialized = False

    async def initialize(self):
        """Create connections and fill the pool"""
        for _ in range(self.pool_size):
            conn = await aiosqlite.connect(self.db_path)
            conn.row_factory = aiosqlite.Row
            await self._pool.put(conn)
        self._initialized = True

    @asynccontextmanager
    async def connect(self):
        """Get a connection from the pool"""
        if not self._initialized:
            await self.initialize()
        conn = await self._pool.get()
        try:
            yield conn
        finally:
            await self._pool.put(conn)

    async def close_all(self):
        """Close all connections in the pool"""
        while not self._pool.empty():
            conn = await self._pool.get()
            await conn.close()

# Global pool instance
db_pool = DatabasePool(DB_PATH, pool_size=5)

# -------------------------------------------------------------------
# Write Queue (for high‑frequency writes)
# -------------------------------------------------------------------
class WriteQueue:
    def __init__(self):
        self._queue = asyncio.Queue()
        self._worker_task = None

    async def start(self):
        self._worker_task = asyncio.create_task(self._worker())

    async def _worker(self):
        while True:
            try:
                operation, args, kwargs, future = await self._queue.get()
                try:
                    result = await operation(*args, **kwargs)
                    future.set_result(result)
                except Exception as e:
                    future.set_exception(e)
                finally:
                    self._queue.task_done()
            except asyncio.CancelledError:
                break

    async def add(self, operation, *args, **kwargs):
        future = asyncio.Future()
        await self._queue.put((operation, args, kwargs, future))
        return await future

    async def stop(self):
        if self._worker_task:
            self._worker_task.cancel()
            await self._worker_task

# Global write queue instance
write_queue = WriteQueue()

# -------------------------------------------------------------------
# Database initialization
# -------------------------------------------------------------------
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        # Users & settings
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                join_date TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                notifications_enabled INTEGER DEFAULT 1,
                default_interval INTEGER DEFAULT 30
            )
        """)

        # Tests & questions
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                intro_message TEXT,
                owner_id INTEGER,
                created_at TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS test_questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                test_id INTEGER,
                question TEXT NOT NULL,
                option_a TEXT,
                option_b TEXT,
                option_c TEXT,
                option_d TEXT,
                option_e TEXT,
                correct_option TEXT NOT NULL,
                explanation TEXT,
                FOREIGN KEY(test_id) REFERENCES tests(id) ON DELETE CASCADE
            )
        """)

        # Scheduled tests
        await db.execute("""
            CREATE TABLE IF NOT EXISTS scheduled_tests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                test_id INTEGER,
                job_id TEXT,
                run_date TEXT,
                interval INTEGER,
                shuffle INTEGER,
                FOREIGN KEY(test_id) REFERENCES tests(id) ON DELETE CASCADE
            )
        """)

        # Quiz runs (sessions)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS quiz_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                test_id INTEGER,
                chat_id INTEGER,
                start_time TEXT,
                FOREIGN KEY(test_id) REFERENCES tests(id) ON DELETE CASCADE
            )
        """)

        # User answers – NO UNIQUE constraint, every answer counts separately
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_answers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                test_id INTEGER,
                run_id INTEGER,
                question_idx INTEGER,
                is_correct INTEGER,
                is_live INTEGER,
                timestamp TEXT
            )
        """)

        # Indexes for performance
        await db.execute("CREATE INDEX IF NOT EXISTS idx_answers_user ON user_answers(user_id, is_correct)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_answers_run ON user_answers(run_id, is_live)")

        await db.commit()

    # Initialize connection pool
    await db_pool.initialize()

# -------------------------------------------------------------------
# User & settings
# -------------------------------------------------------------------
async def add_user(user_id: int, username: str, first_name: str):
    async with aiosqlite.connect(DB_PATH) as db:
        now = datetime.now().isoformat()
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id, username, first_name, join_date) VALUES (?, ?, ?, ?)",
            (user_id, username, first_name, now)
        )
        await db.commit()

async def get_user_settings(user_id: int) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM user_settings WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            if row:
                return dict(row)
            await db.execute("INSERT OR IGNORE INTO user_settings (user_id) VALUES (?)", (user_id,))
            await db.commit()
            return {"user_id": user_id, "notifications_enabled": 1, "default_interval": 30}

async def update_user_setting(user_id: int, setting: str, value: int):
    ALLOWED_SETTINGS = {'notifications_enabled', 'default_interval'}
    if setting not in ALLOWED_SETTINGS:
        raise ValueError(f"Invalid setting: {setting}")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE user_settings SET {setting} = ? WHERE user_id = ?", (value, user_id))
        await db.commit()

# -------------------------------------------------------------------
# Test management
# -------------------------------------------------------------------
async def create_test(name: str, intro_message: str, owner_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        now = datetime.now().isoformat()
        cursor = await db.execute(
            "INSERT INTO tests (name, intro_message, owner_id, created_at) VALUES (?, ?, ?, ?)",
            (name, intro_message, owner_id, now)
        )
        await db.commit()
        return cursor.lastrowid

async def add_test_question(test_id: int, q_data: dict):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO test_questions 
               (test_id, question, option_a, option_b, option_c, option_d, option_e, correct_option, explanation)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (test_id, q_data['question'],
             q_data['options'].get('A'), q_data['options'].get('B'),
             q_data['options'].get('C'), q_data['options'].get('D'),
             q_data['options'].get('E'),
             q_data['correct'], q_data.get('explanation', 'No explanation provided.'))
        )
        await db.commit()

async def create_test_with_questions(name: str, intro_message: str, owner_id: int, questions: list) -> tuple:
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute("BEGIN TRANSACTION")
            now = datetime.now().isoformat()
            cursor = await db.execute(
                "INSERT INTO tests (name, intro_message, owner_id, created_at) VALUES (?, ?, ?, ?)",
                (name, intro_message, owner_id, now)
            )
            test_id = cursor.lastrowid
            for q in questions:
                await db.execute(
                    """INSERT INTO test_questions 
                       (test_id, question, option_a, option_b, option_c, option_d, option_e, correct_option, explanation)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (test_id, q['question'],
                     q['options'].get('A'), q['options'].get('B'),
                     q['options'].get('C'), q['options'].get('D'),
                     q['options'].get('E'),
                     q['correct'], q.get('explanation', 'No explanation provided.'))
                )
            await db.commit()
            return test_id, len(questions)
        except Exception:
            await db.rollback()
            raise

async def get_user_tests(owner_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id, name FROM tests WHERE owner_id = ?", (owner_id,)) as cursor:
            return await cursor.fetchall()

async def delete_test(test_id: int, owner_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("DELETE FROM tests WHERE id = ? AND owner_id = ?", (test_id, owner_id))
        await db.commit()
        return cursor.rowcount > 0

async def get_test(test_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM tests WHERE id = ?", (test_id,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

async def get_test_questions(test_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM test_questions WHERE test_id = ?", (test_id,)) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

# -------------------------------------------------------------------
# Scheduling
# -------------------------------------------------------------------
async def create_schedule(chat_id: int, test_id: int, job_id: str, run_date: str, interval: int, shuffle: bool) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO scheduled_tests (chat_id, test_id, job_id, run_date, interval, shuffle) VALUES (?, ?, ?, ?, ?, ?)",
            (chat_id, test_id, job_id, run_date, interval, 1 if shuffle else 0)
        )
        await db.commit()
        return cursor.lastrowid

async def get_schedules(chat_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT s.id, s.run_date, t.name FROM scheduled_tests s JOIN tests t ON s.test_id = t.id WHERE s.chat_id = ?",
            (chat_id,)
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

async def delete_schedule(schedule_id: int, chat_id: int) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT job_id FROM scheduled_tests WHERE id = ? AND chat_id = ?", (schedule_id, chat_id)) as cursor:
            row = await cursor.fetchone()
            if row:
                job_id = row[0]
                await db.execute("DELETE FROM scheduled_tests WHERE id = ?", (schedule_id,))
                await db.commit()
                return job_id
    return None

# -------------------------------------------------------------------
# Quiz runs
# -------------------------------------------------------------------
async def create_quiz_run(test_id: int, chat_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        now = datetime.now().isoformat()
        cursor = await db.execute(
            "INSERT INTO quiz_runs (test_id, chat_id, start_time) VALUES (?, ?, ?)",
            (test_id, chat_id, now)
        )
        await db.commit()
        return cursor.lastrowid

async def delete_quiz_run(run_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM quiz_runs WHERE id = ?", (run_id,))
        await db.commit()

async def get_quiz_run(run_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM quiz_runs WHERE id = ?", (run_id,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

# -------------------------------------------------------------------
# Global answers (no duplicate prevention)
# -------------------------------------------------------------------
async def save_user_answer(user_id: int, test_id: int, run_id: int,
                           question_idx: int, is_correct: int, is_live: int):
    async with aiosqlite.connect(DB_PATH) as db:
        now = datetime.now().isoformat()
        await db.execute(
            """INSERT INTO user_answers
               (user_id, test_id, run_id, question_idx, is_correct, is_live, timestamp)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (user_id, test_id, run_id, question_idx, is_correct, is_live, now)
        )
        await db.commit()

async def get_user_global_stats(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*), SUM(is_correct) FROM user_answers WHERE user_id = ?",
            (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            total = row[0] or 0
            correct = row[1] or 0
            return total, correct

async def get_global_leaderboard(limit: int = 10):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT user_id, COUNT(*) as total_answered, SUM(is_correct) as correct_answers
            FROM user_answers
            GROUP BY user_id
            ORDER BY correct_answers DESC, total_answered ASC
            LIMIT ?""",
            (limit,)
        ) as cursor:
            return await cursor.fetchall()

# -------------------------------------------------------------------
# Reset stats
# -------------------------------------------------------------------
async def reset_user_stats(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("DELETE FROM user_answers WHERE user_id = ?", (user_id,))
        await db.commit()
        return cursor.rowcount

async def get_user_answer_count(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM user_answers WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            return row[0] or 0