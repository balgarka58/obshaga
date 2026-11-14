"""
Модуль базы данных с безпотерьной архивацией (Lossless Rollup), защитой буфера записи,
пакетной проверкой серий и генерацией суточной сводки для администратора (v7.6).
"""
import gc
import time
import json
import random
import sqlite3
import asyncio
import threading
from datetime import datetime, date, timedelta, timezone
from typing import Optional, Dict, Any, List, Tuple, Set

from core.common import (
    DATABASE_URL,
    DB_PATH,
    TZ,
    logger,
    get_local_today,
    normalize_neon_pooler_url,
)
from core.learning import (
    SpacedRepetitionAlgorithm,
    catalog,
    session_tracker,
)


class Database:
    """
    Высокопроизводительный слой БД с автоматической суточной архивацией без потери точности:
    1. Ответы младше 30 дней хранятся в user_answers поминутно.
    2. Ответы старше 30 дней раз в сутки агрегируются в user_daily_archive
       с сохранением точного числа попыток (tries_count), ошибок (errors_count),
       суммарного изменения веса (total_delta_weight) и профиля пользователя (username, full_name).
    3. Полная защита от гонок кэша (monotone _write_seq), возврат батча при сбое сети
       с защитой от переполнения ОЗУ (max 1000 записей).
    """

    def __init__(
        self, db_url: Optional[str] = DATABASE_URL, sqlite_path: str = DB_PATH
    ):
        self.db_url = normalize_neon_pooler_url(db_url)
        self.sqlite_path = sqlite_path
        self.is_postgres = bool(self.db_url and self.db_url.startswith("postgres"))
        self._cache_lock = threading.Lock()
        self._write_seq: int = 0
        self._user_entries_cache: Dict[
            int, Tuple[float, int, List[Tuple[str, Any]]]
        ] = {}
        self._user_settings_cache: Dict[
            int, Tuple[float, int, bool, bool, str]
        ] = {}
        self._cache_ttl = 1800.0  # 30 минут для истории ответов (экономия Compute Hours)
        self._settings_cache_ttl = 21600.0  # 6 часов для редких настроек (ОГЭ/ЕГЭ, политика, токен)
        self._max_cache_size = 4500
        self._max_settings_cache_size = 10000
        self._write_queue_lock = threading.Lock()
        self._pending_writes: List[Tuple[int, Optional[str], Optional[str], str]] = []
        self._max_pending_writes = 1000
        self._flush_task: Optional[asyncio.Task] = None

    def get_connection(self):
        if self.is_postgres:
            import psycopg2

            return psycopg2.connect(self.db_url, connect_timeout=10)
        else:
            conn = sqlite3.connect(self.sqlite_path, timeout=15.0)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA journal_mode=WAL;")
                conn.execute("PRAGMA synchronous=NORMAL;")
                conn.execute("PRAGMA busy_timeout=10000;")
            except Exception:
                pass
            return conn

    def _evict_oldest_locked(
        self, cache_dict: Dict[Any, Any], max_size: Optional[int] = None
    ):
        limit = (
            max_size
            if max_size is not None
            else (
                self._max_settings_cache_size
                if cache_dict is self._user_settings_cache
                else self._max_cache_size
            )
        )
        while len(cache_dict) >= limit:
            oldest_key = next(iter(cache_dict))
            cache_dict.pop(oldest_key, None)

    def invalidate_user_cache(self, user_id: Optional[int] = None):
        with self._cache_lock:
            self._write_seq += 1
            if user_id is None:
                self._user_entries_cache.clear()
                self._user_settings_cache.clear()
            else:
                self._user_entries_cache.pop(user_id, None)
                self._user_settings_cache.pop(user_id, None)

    def init_db(self):
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            if self.is_postgres:
                cursor.execute(
                    """
                CREATE TABLE IF NOT EXISTS user_answers (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    username TEXT,
                    full_name TEXT,
                    entry TEXT NOT NULL,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                ALTER TABLE user_answers ADD COLUMN IF NOT EXISTS username TEXT;
                ALTER TABLE user_answers ADD COLUMN IF NOT EXISTS full_name TEXT;
                CREATE INDEX IF NOT EXISTS idx_user_answers_user ON user_answers(user_id);
                CREATE INDEX IF NOT EXISTS idx_user_answers_user_created ON user_answers(user_id, id);
                CREATE INDEX IF NOT EXISTS idx_user_answers_created_at ON user_answers(created_at);

                CREATE TABLE IF NOT EXISTS user_daily_archive (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    username TEXT,
                    full_name TEXT,
                    archive_date DATE NOT NULL,
                    mode VARCHAR(16) NOT NULL,
                    code VARCHAR(32) NOT NULL,
                    tries_count INTEGER NOT NULL DEFAULT 1,
                    errors_count INTEGER NOT NULL DEFAULT 0,
                    total_time_sec REAL NOT NULL DEFAULT 0.0,
                    total_delta_weight REAL NOT NULL DEFAULT 0.0,
                    last_delta_ans REAL NOT NULL DEFAULT 0.0,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_user_daily_code UNIQUE (user_id, archive_date, mode, code)
                );
                ALTER TABLE user_daily_archive ADD COLUMN IF NOT EXISTS username TEXT;
                ALTER TABLE user_daily_archive ADD COLUMN IF NOT EXISTS full_name TEXT;
                CREATE INDEX IF NOT EXISTS idx_archive_user ON user_daily_archive(user_id);
                CREATE INDEX IF NOT EXISTS idx_archive_user_date ON user_daily_archive(user_id, archive_date);

                CREATE TABLE IF NOT EXISTS teachers (
                    teacher_code VARCHAR(8) PRIMARY KEY,
                    user_id BIGINT NOT NULL UNIQUE,
                    full_name TEXT,
                    token_version INTEGER NOT NULL DEFAULT 1,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                ALTER TABLE teachers ADD COLUMN IF NOT EXISTS token_version INTEGER NOT NULL DEFAULT 1;

                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint WHERE conname = 'teachers_user_id_key'
                    ) THEN
                        ALTER TABLE teachers ADD CONSTRAINT teachers_user_id_key UNIQUE (user_id);
                    END IF;
                END $$;

                CREATE INDEX IF NOT EXISTS idx_teachers_user ON teachers(user_id);

                CREATE TABLE IF NOT EXISTS student_teachers (
                    student_id BIGINT NOT NULL,
                    teacher_code VARCHAR(8) NOT NULL,
                    joined_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (student_id, teacher_code)
                );
                CREATE INDEX IF NOT EXISTS idx_student_teachers_teacher ON student_teachers(teacher_code);
                CREATE INDEX IF NOT EXISTS idx_student_teachers_student ON student_teachers(student_id);

                CREATE TABLE IF NOT EXISTS user_settings (
                    user_id BIGINT PRIMARY KEY,
                    token_version INTEGER NOT NULL DEFAULT 1,
                    reminders_enabled INTEGER NOT NULL DEFAULT 1,
                    policy_accepted INTEGER NOT NULL DEFAULT 0,
                    exam_level VARCHAR(8) NOT NULL DEFAULT 'ege',
                    last_reminder_date TEXT,
                    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS policy_accepted INTEGER NOT NULL DEFAULT 0;
                ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS exam_level VARCHAR(8) NOT NULL DEFAULT 'ege';

                CREATE TABLE IF NOT EXISTS autopost_history (
                    id SERIAL PRIMARY KEY,
                    term_code VARCHAR(32) NOT NULL,
                    news_url TEXT,
                    format_type VARCHAR(32) NOT NULL,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_autopost_history_created ON autopost_history(created_at);

                CREATE TABLE IF NOT EXISTS autopost_drafts (
                    draft_id VARCHAR(32) PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                """
                )
            else:
                cursor.executescript(
                    """
                CREATE TABLE IF NOT EXISTS user_answers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    username TEXT,
                    full_name TEXT,
                    entry TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_user_answers_user ON user_answers(user_id);
                CREATE INDEX IF NOT EXISTS idx_user_answers_user_created ON user_answers(user_id, id);
                CREATE INDEX IF NOT EXISTS idx_user_answers_created_at ON user_answers(created_at);

                CREATE TABLE IF NOT EXISTS user_daily_archive (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    username TEXT,
                    full_name TEXT,
                    archive_date TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    code TEXT NOT NULL,
                    tries_count INTEGER NOT NULL DEFAULT 1,
                    errors_count INTEGER NOT NULL DEFAULT 0,
                    total_time_sec REAL NOT NULL DEFAULT 0.0,
                    total_delta_weight REAL NOT NULL DEFAULT 0.0,
                    last_delta_ans REAL NOT NULL DEFAULT 0.0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (user_id, archive_date, mode, code)
                );
                CREATE INDEX IF NOT EXISTS idx_archive_user ON user_daily_archive(user_id);
                CREATE INDEX IF NOT EXISTS idx_archive_user_date ON user_daily_archive(user_id, archive_date);

                CREATE TABLE IF NOT EXISTS teachers (
                    teacher_code TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL UNIQUE,
                    full_name TEXT,
                    token_version INTEGER NOT NULL DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_teachers_user ON teachers(user_id);

                CREATE TABLE IF NOT EXISTS student_teachers (
                    student_id INTEGER NOT NULL,
                    teacher_code TEXT NOT NULL,
                    joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (student_id, teacher_code)
                );
                CREATE INDEX IF NOT EXISTS idx_student_teachers_teacher ON student_teachers(teacher_code);
                CREATE INDEX IF NOT EXISTS idx_student_teachers_student ON student_teachers(student_id);

                CREATE TABLE IF NOT EXISTS user_settings (
                    user_id INTEGER PRIMARY KEY,
                    token_version INTEGER NOT NULL DEFAULT 1,
                    reminders_enabled INTEGER NOT NULL DEFAULT 1,
                    policy_accepted INTEGER NOT NULL DEFAULT 0,
                    exam_level TEXT NOT NULL DEFAULT 'ege',
                    last_reminder_date TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS autopost_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    term_code TEXT NOT NULL,
                    news_url TEXT,
                    format_type TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_autopost_history_created ON autopost_history(created_at);

                CREATE TABLE IF NOT EXISTS autopost_drafts (
                    draft_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """
                )
                try:
                    cursor.execute(
                        "CREATE UNIQUE INDEX IF NOT EXISTS idx_teachers_user_unique ON teachers(user_id);"
                    )
                except Exception:
                    pass

                for alter_sql in [
                    "ALTER TABLE user_answers ADD COLUMN username TEXT;",
                    "ALTER TABLE user_answers ADD COLUMN full_name TEXT;",
                    "ALTER TABLE user_daily_archive ADD COLUMN username TEXT;",
                    "ALTER TABLE user_daily_archive ADD COLUMN full_name TEXT;",
                    "ALTER TABLE teachers ADD COLUMN token_version INTEGER NOT NULL DEFAULT 1;",
                    "ALTER TABLE user_settings ADD COLUMN policy_accepted INTEGER NOT NULL DEFAULT 0;",
                    "ALTER TABLE user_settings ADD COLUMN exam_level TEXT NOT NULL DEFAULT 'ege';",
                ]:
                    try:
                        cursor.execute(alter_sql)
                    except Exception:
                        pass

            conn.commit()
        finally:
            cursor.close()
            conn.close()

        self.cleanup_inactive_teachers()
        mode_str = (
            "Neon PostgreSQL (Pooler + Lossless Archive)"
            if self.is_postgres
            else f"SQLite ({self.sqlite_path})"
        )
        logger.info(f"База данных успешно инициализирована: {mode_str}.")

    def _update_ram_cache_on_answer(
        self, user_id: int, entry_str: str, now_dt: datetime
    ):
        """Мгновенно (за 0.001с) обновляет RAM-кэш пользователя до физической записи в БД."""
        with self._cache_lock:
            self._write_seq += 1
            cached = self._user_entries_cache.get(user_id)
            if cached is not None:
                ts_cached, _, rows_list = cached
                if time.time() - ts_cached < self._cache_ttl:
                    new_list = list(rows_list)
                    new_list.append((entry_str, now_dt))
                    self._user_entries_cache[user_id] = (
                        time.time(),
                        self._write_seq,
                        new_list,
                    )
                else:
                    self._user_entries_cache.pop(user_id, None)

    def _insert_batch_to_db_sync(
        self, batch: List[Tuple[int, Optional[str], Optional[str], str]]
    ) -> bool:
        """
        Выполняет пакетную запись (Batch Insert) всех накопившихся ответов за 1 транзакцию.
        Возвращает True при успехе и False при временном сбое сети/БД.
        """
        if not batch:
            return True
        conn = None
        cursor = None
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            if self.is_postgres:
                cursor.executemany(
                    "INSERT INTO user_answers (user_id, username, full_name, entry) VALUES (%s, %s, %s, %s);",
                    batch,
                )
            else:
                cursor.executemany(
                    "INSERT INTO user_answers (user_id, username, full_name, entry) VALUES (?, ?, ?, ?);",
                    batch,
                )
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"Ошибка пакетной записи ({len(batch)} шт.) в БД: {e}")
            return False
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception:
                    pass
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def flush_pending_writes_sync(self):
        """
        Принудительно сбрасывает буфер ответов на диск в БД.
        При временном сбое сети возвращает несохранённую пачку обратно в очередь
        с ограничением до _max_pending_writes (1000 записей) для защиты ОЗУ.
        """
        with self._write_queue_lock:
            if not self._pending_writes:
                return
            batch = list(self._pending_writes)
            self._pending_writes.clear()

        ok = self._insert_batch_to_db_sync(batch)
        if not ok:
            with self._write_queue_lock:
                combined = batch + self._pending_writes
                if len(combined) > self._max_pending_writes:
                    combined = combined[-self._max_pending_writes :]
                self._pending_writes = combined

    async def _delayed_batch_flush(self):
        """Копит ответы в течение 2.5 сек и записывает их в БД одним пакетным запросом."""
        try:
            await asyncio.sleep(2.5)
            await asyncio.to_thread(self.flush_pending_writes_sync)
        except Exception as e:
            logger.error(f"Ошибка в _delayed_batch_flush: {e}")

    def save_answer_entry(
        self,
        user_id: int,
        username: Optional[str],
        full_name: Optional[str],
        entry_str: str,
    ):
        """Синхронное сохранение ответа: обновляет RAM-кэш и сразу пишет в БД."""
        now_dt = datetime.now(timezone.utc)
        self._update_ram_cache_on_answer(user_id, entry_str, now_dt)
        ok = self._insert_batch_to_db_sync([(user_id, username, full_name, entry_str)])
        if not ok:
            with self._write_queue_lock:
                if len(self._pending_writes) < self._max_pending_writes:
                    self._pending_writes.append(
                        (user_id, username, full_name, entry_str)
                    )

    async def save_answer_entry_fast(
        self,
        user_id: int,
        username: Optional[str],
        full_name: Optional[str],
        entry_str: str,
    ):
        """
        Неблокирующее пакетное сохранение ответа (Optimistic UI + Batch Queue):
        1) Если кэш пользователя ещё не прогрет в RAM — прогревает его 1 раз.
        2) Мгновенно обновляет RAM-кэш (следующая карточка выбирается сразу с новым весом за 0.001с).
        3) Добавляет ответ в буфер пакетной записи и планирует фоновый сброс через 0.8с.
        """
        with self._cache_lock:
            is_cached = user_id in self._user_entries_cache and (
                time.time() - self._user_entries_cache[user_id][0] < self._cache_ttl
            )
        if not is_cached:
            await asyncio.to_thread(self._fetch_user_entries_with_timestamps, user_id)

        now_dt = datetime.now(timezone.utc)
        self._update_ram_cache_on_answer(user_id, entry_str, now_dt)

        with self._write_queue_lock:
            self._pending_writes.append((user_id, username, full_name, entry_str))
            if len(self._pending_writes) > self._max_pending_writes:
                self._pending_writes = self._pending_writes[-self._max_pending_writes :]
            should_flush_immediately = len(self._pending_writes) >= 25

        if should_flush_immediately:
            asyncio.create_task(asyncio.to_thread(self.flush_pending_writes_sync))
        elif self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._delayed_batch_flush())

    def archive_old_answers(self, days_threshold: int = 30) -> int:
        """
        Фоновое сжатие (Lossless Rollup):
        Схлопывает ответы старше 30 дней в компактную таблицу user_daily_archive.
        После завершения вызывает gc.collect() для освобождения временных структур из RAM.
        """
        self.flush_pending_writes_sync()
        cutoff_local_date = get_local_today() - timedelta(days=days_threshold)
        cutoff_dt = datetime(
            cutoff_local_date.year,
            cutoff_local_date.month,
            cutoff_local_date.day,
            0,
            0,
            0,
            tzinfo=TZ,
        ).astimezone(timezone.utc)
        conn = self.get_connection()
        cursor = conn.cursor()
        compressed_count = 0
        try:
            if self.is_postgres:
                cursor.execute(
                    "SELECT id, user_id, username, full_name, entry, created_at FROM user_answers WHERE created_at < %s ORDER BY id ASC;",
                    (cutoff_dt,),
                )
            else:
                cursor.execute(
                    "SELECT id, user_id, username, full_name, entry, created_at FROM user_answers WHERE created_at < ? ORDER BY id ASC;",
                    (cutoff_dt.strftime("%Y-%m-%d %H:%M:%S"),),
                )
            old_rows = cursor.fetchall()
            if not old_rows:
                return 0

            compressed_count = len(old_rows)
            agg: Dict[Tuple[int, str, str, str], Dict[str, Any]] = {}
            row_ids_to_delete = []

            for r in old_rows:
                r_id = r[0]
                uid = int(r[1])
                uname = (
                    str(r[2]).strip()
                    if r[2] is not None and str(r[2]).strip() not in ("", "None", "nan")
                    else None
                )
                fname = (
                    str(r[3]).strip()
                    if r[3] is not None and str(r[3]).strip() not in ("", "None", "nan")
                    else None
                )
                entry_str = str(r[4] or "")
                raw_created = r[5]
                d_val = (
                    self._extract_date_from_ts(raw_created, target_tz=TZ)
                    or get_local_today()
                )
                d_str = d_val.isoformat()

                parts = entry_str.split(";")
                if len(parts) < 4:
                    row_ids_to_delete.append(r_id)
                    continue

                code = parts[0].strip()
                if not code:
                    row_ids_to_delete.append(r_id)
                    continue

                try:
                    d_ans = float(parts[1])
                    d_tm = float(parts[2])
                    sec = max(0.0, float(parts[3]))
                except ValueError:
                    row_ids_to_delete.append(r_id)
                    continue

                mode = parts[4].strip() if len(parts) >= 5 else "card"
                if mode == "test" and sec < SpacedRepetitionAlgorithm.MIN_TIME_WINDOW:
                    row_ids_to_delete.append(r_id)
                    continue

                sec_clamped = min(sec, SpacedRepetitionAlgorithm.MAX_TIME_WINDOW)
                is_err = 1 if (d_ans > 0) else 0

                key = (uid, d_str, mode, code)
                if key not in agg:
                    agg[key] = {
                        "username": uname,
                        "full_name": fname,
                        "tries": 0,
                        "errors": 0,
                        "time_sec": 0.0,
                        "delta_weight": 0.0,
                        "last_d_ans": d_ans,
                    }
                if uname:
                    agg[key]["username"] = uname
                if fname:
                    agg[key]["full_name"] = fname
                agg[key]["tries"] += 1
                agg[key]["errors"] += is_err
                agg[key]["time_sec"] += sec_clamped
                agg[key]["delta_weight"] += d_ans + d_tm
                agg[key]["last_d_ans"] = d_ans
                row_ids_to_delete.append(r_id)

            for (uid, d_str, mode, code), metrics in agg.items():
                if self.is_postgres:
                    cursor.execute(
                        """
                        INSERT INTO user_daily_archive 
                            (user_id, username, full_name, archive_date, mode, code, tries_count, errors_count, total_time_sec, total_delta_weight, last_delta_ans)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (user_id, archive_date, mode, code) DO UPDATE SET
                            username = COALESCE(EXCLUDED.username, user_daily_archive.username),
                            full_name = COALESCE(EXCLUDED.full_name, user_daily_archive.full_name),
                            tries_count = user_daily_archive.tries_count + EXCLUDED.tries_count,
                            errors_count = user_daily_archive.errors_count + EXCLUDED.errors_count,
                            total_time_sec = user_daily_archive.total_time_sec + EXCLUDED.total_time_sec,
                            total_delta_weight = user_daily_archive.total_delta_weight + EXCLUDED.total_delta_weight,
                            last_delta_ans = EXCLUDED.last_delta_ans;
                    """,
                        (
                            uid,
                            metrics["username"],
                            metrics["full_name"],
                            d_str,
                            mode,
                            code,
                            metrics["tries"],
                            metrics["errors"],
                            round(metrics["time_sec"], 2),
                            round(metrics["delta_weight"], 2),
                            metrics["last_d_ans"],
                        ),
                    )
                else:
                    cursor.execute(
                        """
                        INSERT INTO user_daily_archive 
                            (user_id, username, full_name, archive_date, mode, code, tries_count, errors_count, total_time_sec, total_delta_weight, last_delta_ans)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT (user_id, archive_date, mode, code) DO UPDATE SET
                            username = COALESCE(excluded.username, user_daily_archive.username),
                            full_name = COALESCE(excluded.full_name, user_daily_archive.full_name),
                            tries_count = user_daily_archive.tries_count + excluded.tries_count,
                            errors_count = user_daily_archive.errors_count + excluded.errors_count,
                            total_time_sec = user_daily_archive.total_time_sec + excluded.total_time_sec,
                            total_delta_weight = user_daily_archive.total_delta_weight + excluded.total_delta_weight,
                            last_delta_ans = excluded.last_delta_ans;
                    """,
                        (
                            uid,
                            metrics["username"],
                            metrics["full_name"],
                            d_str,
                            mode,
                            code,
                            metrics["tries"],
                            metrics["errors"],
                            round(metrics["time_sec"], 2),
                            round(metrics["delta_weight"], 2),
                            metrics["last_d_ans"],
                        ),
                    )

            if row_ids_to_delete:
                batch_size = 500
                for b_i in range(0, len(row_ids_to_delete), batch_size):
                    batch = row_ids_to_delete[b_i : b_i + batch_size]
                    if self.is_postgres:
                        cursor.execute(
                            "DELETE FROM user_answers WHERE id = ANY(%s);", (batch,)
                        )
                    else:
                        placeholders = ",".join("?" for _ in batch)
                        cursor.execute(
                            f"DELETE FROM user_answers WHERE id IN ({placeholders});",
                            batch,
                        )

            conn.commit()
            self.invalidate_user_cache()
            logger.info(
                f"Сжатие БД: {compressed_count} старых записей успешно агрегированы в {len(agg)} строк архива."
            )
            return compressed_count
        except Exception as e:
            logger.warning(f"Ошибка при сжатии старых записей БД: {e}")
            return 0
        finally:
            cursor.close()
            conn.close()
            gc.collect()

    def _fetch_user_entries_with_timestamps(
        self, user_id: int
    ) -> List[Tuple[str, Any]]:
        """
        Возвращает полную хронологическую историю (архив + свежие ответы).
        """
        now = time.time()
        with self._cache_lock:
            cached = self._user_entries_cache.get(user_id)
            if cached is not None:
                ts_cached, _, rows_list = cached
                if now - ts_cached < self._cache_ttl:
                    return list(rows_list)
            seq_before = self._write_seq

        result: List[Tuple[str, Any]] = []
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            try:
                if self.is_postgres:
                    cursor.execute(
                        """
                        SELECT archive_date, mode, code, tries_count, errors_count, total_time_sec, total_delta_weight, last_delta_ans 
                        FROM user_daily_archive WHERE user_id = %s ORDER BY archive_date ASC, id ASC;
                    """,
                        (user_id,),
                    )
                else:
                    cursor.execute(
                        """
                        SELECT archive_date, mode, code, tries_count, errors_count, total_time_sec, total_delta_weight, last_delta_ans 
                        FROM user_daily_archive WHERE user_id = ? ORDER BY archive_date ASC, id ASC;
                    """,
                        (user_id,),
                    )
                arch_rows = cursor.fetchall()
                for ar in arch_rows:
                    a_date = ar[0]
                    a_mode = str(ar[1]).strip()
                    a_code = str(ar[2]).strip()
                    a_tries = max(1, int(ar[3] or 1))
                    a_err = max(0, min(a_tries, int(ar[4] or 0)))
                    a_ok = a_tries - a_err
                    a_sec = round(max(0.0, float(ar[5] or 0.0)) / a_tries, 1)
                    if (
                        a_mode == "test"
                        and a_sec < SpacedRepetitionAlgorithm.MIN_TIME_WINDOW
                    ):
                        a_sec = SpacedRepetitionAlgorithm.MIN_TIME_WINDOW
                    a_total_dw = float(ar[6] or 0.0)
                    a_last_ans = float(ar[7] or 0.0)
                    ts_val = (
                        a_date
                        if isinstance(a_date, date)
                        else f"{str(a_date)[:10]}T12:00:00+00:00"
                    )

                    if a_mode == "test":
                        err_ans_val = a_last_ans if a_last_ans > 0 else 2.0
                        ok_entry = f"{a_code};-1.0;0.0;{a_sec};test"
                        err_entry = f"{a_code};{err_ans_val};0.0;{a_sec};test"
                        if a_last_ans > 0 and a_err > 0:
                            for _ in range(a_ok):
                                result.append((ok_entry, ts_val))
                            for _ in range(a_err):
                                result.append((err_entry, ts_val))
                        else:
                            for _ in range(a_err):
                                result.append((err_entry, ts_val))
                            for _ in range(a_ok):
                                result.append((ok_entry, ts_val))
                    else:
                        if a_tries == 1:
                            d_tm_single = round(a_total_dw - a_last_ans, 2)
                            result.append(
                                (
                                    f"{a_code};{a_last_ans};{d_tm_single};{a_sec};{a_mode}",
                                    ts_val,
                                )
                            )
                        else:
                            step_dw = round(a_total_dw / a_tries, 2)
                            err_ans_val = a_last_ans if a_last_ans > 0 else 2.0
                            for i in range(a_tries - 1):
                                is_step_err = (
                                    (i < a_err) if a_last_ans <= 0 else (i >= a_ok)
                                )
                                step_ans = err_ans_val if is_step_err else -1.0
                                step_tm = round(step_dw - step_ans, 2)
                                result.append(
                                    (
                                        f"{a_code};{step_ans};{step_tm};{a_sec};{a_mode}",
                                        ts_val,
                                    )
                                )
                            rem_dw = round(a_total_dw - step_dw * (a_tries - 1), 2)
                            last_tm = round(rem_dw - a_last_ans, 2)
                            result.append(
                                (
                                    f"{a_code};{a_last_ans};{last_tm};{a_sec};{a_mode}",
                                    ts_val,
                                )
                            )
            except Exception as arch_err:
                logger.warning(
                    f"Не удалось прочитать архив для user_id={user_id}: {arch_err}"
                )

            if self.is_postgres:
                cursor.execute(
                    "SELECT entry, created_at FROM user_answers WHERE user_id = %s ORDER BY id ASC;",
                    (user_id,),
                )
            else:
                cursor.execute(
                    "SELECT entry, created_at FROM user_answers WHERE user_id = ? ORDER BY id ASC;",
                    (user_id,),
                )
            rows = cursor.fetchall()
            for r in rows:
                result.append((str(r[0]), r[1]))
        finally:
            cursor.close()
            conn.close()

        with self._cache_lock:
            if self._write_seq == seq_before:
                self._evict_oldest_locked(self._user_entries_cache)
                self._user_entries_cache[user_id] = (
                    time.time(),
                    self._write_seq,
                    result,
                )
        return list(result)

    def _fetch_user_entries(self, user_id: int) -> List[str]:
        rows = self._fetch_user_entries_with_timestamps(user_id)
        return [r[0] for r in rows]

    @staticmethod
    def _compute_progress_from_entries(
        entries: List[str],
    ) -> Dict[str, Dict[str, Any]]:
        progress: Dict[str, Dict[str, Any]] = {}
        for entry_str in entries:
            parts = entry_str.split(";")
            if len(parts) >= 4:
                mode = parts[4].strip() if len(parts) >= 5 else "card"
                if mode == "test":
                    continue

                code = parts[0].strip()
                if not code:
                    continue
                try:
                    d_ans = float(parts[1])
                    d_tm = float(parts[2])
                except ValueError:
                    continue

                if code not in progress:
                    progress[code] = {
                        "total_weight": SpacedRepetitionAlgorithm.DEFAULT_WEIGHT,
                        "review_count": 0,
                        "last_delta_ans": 0.0,
                    }

                cur_w = progress[code]["total_weight"]
                progress[code]["total_weight"] = SpacedRepetitionAlgorithm.update_weight(
                    cur_w, d_ans, d_tm
                )
                progress[code]["review_count"] += 1
                progress[code]["last_delta_ans"] = d_ans
        return progress

    def get_user_progress_map(self, user_id: int) -> Dict[str, Dict[str, Any]]:
        entries = self._fetch_user_entries(user_id)
        return self._compute_progress_from_entries(entries)

    def get_problem_term_codes(
        self,
        user_id: int,
        pre_entries: Optional[List[str]] = None,
        pre_progress: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> List[str]:
        entries = (
            pre_entries
            if pre_entries is not None
            else self._fetch_user_entries(user_id)
        )
        progress_map = (
            pre_progress
            if pre_progress is not None
            else self._compute_progress_from_entries(entries)
        )

        last_card_problem: Dict[str, bool] = {}
        last_test_problem: Dict[str, bool] = {}
        last_action_mode: Dict[str, str] = {}

        for entry_str in entries:
            parts = entry_str.split(";")
            if len(parts) >= 4:
                code = parts[0].strip()
                if not code:
                    continue
                try:
                    d_ans = float(parts[1])
                    sec = float(parts[3])
                except ValueError:
                    continue
                mode = parts[4].strip() if len(parts) >= 5 else "card"

                if mode == "test":
                    if sec >= SpacedRepetitionAlgorithm.MIN_TIME_WINDOW:
                        is_err = d_ans > 0
                        last_test_problem[code] = is_err
                        last_action_mode[code] = "test"
                        if not is_err:
                            last_card_problem[code] = False
                else:
                    if sec >= 0.0:
                        last_card_problem[code] = d_ans > 0
                        last_action_mode[code] = "card"

        problem_codes = []
        seen = set()

        for code, p in progress_map.items():
            is_high_weight = p["total_weight"] >= 7.0
            is_card_err = last_card_problem.get(code, False)
            is_test_ok = last_action_mode.get(
                code
            ) == "test" and not last_test_problem.get(code, False)

            if is_test_ok:
                continue

            if (
                (is_high_weight or is_card_err)
                and catalog.get_by_code(code)
                and code not in seen
            ):
                seen.add(code)
                problem_codes.append(code)

        for code, is_prob in last_test_problem.items():
            if (
                is_prob
                and last_action_mode.get(code) == "test"
                and catalog.get_by_code(code)
                and code not in seen
            ):
                seen.add(code)
                problem_codes.append(code)

        for code, is_prob in last_card_problem.items():
            if (
                is_prob
                and last_action_mode.get(code) == "card"
                and catalog.get_by_code(code)
                and code not in seen
            ):
                seen.add(code)
                problem_codes.append(code)

        return problem_codes

    @staticmethod
    def _extract_date_from_ts(raw_ts: Any, target_tz=None) -> Optional[date]:
        if raw_ts is None:
            return None
        if isinstance(raw_ts, datetime):
            dt = raw_ts
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if target_tz is not None:
                dt = dt.astimezone(target_tz)
            return dt.date()
        if isinstance(raw_ts, date):
            return raw_ts
        s = str(raw_ts).strip()
        if len(s) == 10 and s[4] == "-" and s[7] == "-":
            try:
                return date.fromisoformat(s)
            except ValueError:
                pass
        if len(s) >= 10:
            try:
                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if target_tz is not None:
                    dt = dt.astimezone(target_tz)
                return dt.date()
            except ValueError:
                try:
                    dt = datetime.fromisoformat(s[:19]).replace(tzinfo=timezone.utc)
                    if target_tz is not None:
                        dt = dt.astimezone(target_tz)
                    return dt.date()
                except ValueError:
                    pass
        return None

    def get_user_streak_info(
        self,
        user_id: int,
        reference_date: Optional[date] = None,
        pre_rows: Optional[List[Tuple[str, Any]]] = None,
    ) -> Dict[str, int]:
        today_dt = reference_date or get_local_today()
        rows = (
            pre_rows
            if pre_rows is not None
            else self._fetch_user_entries_with_timestamps(user_id)
        )

        active_dates_set = set()
        today_actions = 0

        for entry_str, created_at in rows:
            parts = entry_str.split(";")
            if len(parts) < 4:
                continue
            try:
                sec = float(parts[3])
            except ValueError:
                continue
            mode = parts[4].strip() if len(parts) >= 5 else "card"
            if mode == "test" and sec < SpacedRepetitionAlgorithm.MIN_TIME_WINDOW:
                continue

            d = self._extract_date_from_ts(created_at, target_tz=TZ) or today_dt
            if d <= today_dt:
                active_dates_set.add(d)
                if d == today_dt:
                    today_actions += 1

        if not active_dates_set:
            return {
                "current_streak": 0,
                "best_streak": 0,
                "active_days_year": 0,
                "today_actions": 0,
            }

        sorted_dates = sorted(active_dates_set)
        year_ago = today_dt - timedelta(days=364)
        active_days_year = sum(1 for d in sorted_dates if year_ago <= d <= today_dt)

        best_streak = 1
        cur_run = 1
        for i in range(1, len(sorted_dates)):
            if (sorted_dates[i] - sorted_dates[i - 1]).days == 1:
                cur_run += 1
                if cur_run > best_streak:
                    best_streak = cur_run
            elif (sorted_dates[i] - sorted_dates[i - 1]).days > 1:
                cur_run = 1

        if today_dt in active_dates_set:
            check_d = today_dt
        elif (today_dt - timedelta(days=1)) in active_dates_set:
            check_d = today_dt - timedelta(days=1)
        else:
            check_d = None

        current_streak = 0
        while check_d is not None and check_d in active_dates_set:
            current_streak += 1
            check_d -= timedelta(days=1)

        return {
            "current_streak": current_streak,
            "best_streak": best_streak,
            "active_days_year": active_days_year,
            "today_actions": today_actions,
        }

    def get_user_stats(self, user_id: int) -> Dict[str, Any]:
        """Вычисляет все показатели статистики за один запрос к кэшу/БД без повторного чтения."""
        rows = self._fetch_user_entries_with_timestamps(user_id)
        entries = [r[0] for r in rows]
        progress_map = self._compute_progress_from_entries(entries)

        cards_count = 0
        tests_count = 0
        tests_correct = 0
        tests_errors = 0
        test_times = []

        for entry_str in entries:
            parts = entry_str.split(";")
            if len(parts) >= 4:
                try:
                    d_ans = float(parts[1])
                    sec = float(parts[3])
                except ValueError:
                    continue

                mode = parts[4].strip() if len(parts) >= 5 else "card"
                if mode == "test":
                    if sec >= SpacedRepetitionAlgorithm.MIN_TIME_WINDOW:
                        tests_count += 1
                        if d_ans <= 0:
                            tests_correct += 1
                        else:
                            tests_errors += 1
                        if (
                            SpacedRepetitionAlgorithm.MIN_TIME_WINDOW
                            <= sec
                            <= SpacedRepetitionAlgorithm.MAX_TIME_WINDOW
                        ):
                            test_times.append(sec)
                else:
                    cards_count += 1

        tests_accuracy = (
            round(tests_correct / tests_count * 100, 1) if tests_count > 0 else 0.0
        )
        tests_avg_time = (
            round(sum(test_times) / len(test_times), 1) if test_times else 0.0
        )

        learned = sum(1 for p in progress_map.values() if p["total_weight"] <= 3.0)
        in_progress = sum(
            1 for p in progress_map.values() if 3.0 < p["total_weight"] < 7.0
        )
        needs_review = sum(
            1 for p in progress_map.values() if p["total_weight"] >= 7.0
        )

        streak_info = self.get_user_streak_info(user_id, pre_rows=rows)
        problem_count = len(
            self.get_problem_term_codes(
                user_id, pre_entries=entries, pre_progress=progress_map
            )
        )

        return {
            "cards_count": cards_count,
            "learned": learned,
            "in_progress": in_progress,
            "needs_review": needs_review,
            "tests_count": tests_count,
            "tests_correct": tests_correct,
            "tests_errors": tests_errors,
            "tests_accuracy": tests_accuracy,
            "tests_avg_time": tests_avg_time,
            "current_streak": streak_info["current_streak"],
            "best_streak": streak_info["best_streak"],
            "active_days_year": streak_info["active_days_year"],
            "today_actions": streak_info["today_actions"],
            "problem_count": problem_count,
        }

    # --- НАСТРОЙКИ ПОЛЬЗОВАТЕЛЯ, УРОВЕНЬ ЭКЗАМЕНА, ТОКЕНЫ И НАПОМИНАНИЯ ---

    def _ensure_user_settings_row(self, cursor, user_id: int):
        if self.is_postgres:
            cursor.execute(
                "INSERT INTO user_settings (user_id, token_version, reminders_enabled, policy_accepted, exam_level) VALUES (%s, 1, 1, 0, 'ege') ON CONFLICT (user_id) DO NOTHING;",
                (user_id,),
            )
        else:
            cursor.execute(
                "INSERT OR IGNORE INTO user_settings (user_id, token_version, reminders_enabled, policy_accepted, exam_level) VALUES (?, 1, 1, 0, 'ege');",
                (user_id,),
            )

    def _load_user_settings_cached(
        self, user_id: int
    ) -> Tuple[int, bool, bool, str]:
        now = time.time()
        with self._cache_lock:
            cached = self._user_settings_cache.get(user_id)
            if cached is not None and (now - cached[0] < self._settings_cache_ttl):
                return cached[1], cached[2], cached[3], cached[4]
            seq_before = self._write_seq

        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            if self.is_postgres:
                cursor.execute(
                    "SELECT token_version, reminders_enabled, COALESCE(policy_accepted, 0), COALESCE(exam_level, 'ege') FROM user_settings WHERE user_id = %s;",
                    (user_id,),
                )
            else:
                cursor.execute(
                    "SELECT token_version, reminders_enabled, COALESCE(policy_accepted, 0), COALESCE(exam_level, 'ege') FROM user_settings WHERE user_id = ?;",
                    (user_id,),
                )
            row = cursor.fetchone()
            t_ver = int(row[0]) if row and row[0] is not None else 1
            rem_on = bool(int(row[1])) if row and row[1] is not None else True
            pol_ok = bool(int(row[2])) if row and row[2] is not None else False
            raw_lvl = (
                str(row[3] if row and len(row) > 3 and row[3] is not None else "ege")
                .strip()
                .lower()
            )
            exam_lvl = "oge" if raw_lvl == "oge" else "ege"

            if not pol_ok:
                if self.is_postgres:
                    cursor.execute(
                        "SELECT 1 FROM user_answers WHERE user_id = %s LIMIT 1;",
                        (user_id,),
                    )
                else:
                    cursor.execute(
                        "SELECT 1 FROM user_answers WHERE user_id = ? LIMIT 1;",
                        (user_id,),
                    )
                has_ans = bool(cursor.fetchone())
                if not has_ans:
                    try:
                        if self.is_postgres:
                            cursor.execute(
                                "SELECT 1 FROM user_daily_archive WHERE user_id = %s LIMIT 1;",
                                (user_id,),
                            )
                        else:
                            cursor.execute(
                                "SELECT 1 FROM user_daily_archive WHERE user_id = ? LIMIT 1;",
                                (user_id,),
                            )
                        has_ans = bool(cursor.fetchone())
                    except Exception:
                        pass
                if has_ans:
                    pol_ok = True
                    self._ensure_user_settings_row(cursor, user_id)
                    if self.is_postgres:
                        cursor.execute(
                            "UPDATE user_settings SET policy_accepted = 1 WHERE user_id = %s;",
                            (user_id,),
                        )
                    else:
                        cursor.execute(
                            "UPDATE user_settings SET policy_accepted = 1 WHERE user_id = ?;",
                            (user_id,),
                        )
                    conn.commit()

            with self._cache_lock:
                if self._write_seq == seq_before:
                    self._evict_oldest_locked(self._user_settings_cache)
                    self._user_settings_cache[user_id] = (
                        now,
                        t_ver,
                        rem_on,
                        pol_ok,
                        exam_lvl,
                    )
            return t_ver, rem_on, pol_ok, exam_lvl
        except Exception:
            return 1, True, False, "ege"
        finally:
            cursor.close()
            conn.close()

    def get_user_token_version(self, user_id: int) -> int:
        t_ver, _, _, _ = self._load_user_settings_cached(user_id)
        return t_ver

    def get_user_reminders_enabled(self, user_id: int) -> bool:
        _, rem_on, _, _ = self._load_user_settings_cached(user_id)
        return rem_on

    def is_user_policy_accepted(self, user_id: int) -> bool:
        _, _, pol_ok, _ = self._load_user_settings_cached(user_id)
        return pol_ok

    def get_user_exam_level(self, user_id: int) -> str:
        _, _, _, exam_lvl = self._load_user_settings_cached(user_id)
        return "oge" if exam_lvl == "oge" else "ege"

    def set_user_exam_level(self, user_id: int, level: str):
        clean_level = "oge" if str(level or "").strip().lower() == "oge" else "ege"
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            self._ensure_user_settings_row(cursor, user_id)
            if self.is_postgres:
                cursor.execute(
                    "UPDATE user_settings SET exam_level = %s, updated_at = CURRENT_TIMESTAMP WHERE user_id = %s;",
                    (clean_level, user_id),
                )
            else:
                cursor.execute(
                    "UPDATE user_settings SET exam_level = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?;",
                    (clean_level, user_id),
                )
            conn.commit()
        finally:
            cursor.close()
            conn.close()

        with self._cache_lock:
            self._write_seq += 1
            self._user_settings_cache.pop(user_id, None)

    def toggle_user_exam_level(self, user_id: int) -> str:
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            self._ensure_user_settings_row(cursor, user_id)
            if self.is_postgres:
                cursor.execute(
                    "UPDATE user_settings SET exam_level = CASE WHEN COALESCE(exam_level, 'ege') = 'oge' THEN 'ege' ELSE 'oge' END, updated_at = CURRENT_TIMESTAMP WHERE user_id = %s RETURNING exam_level;",
                    (user_id,),
                )
                row = cursor.fetchone()
                new_level = str(row[0]).strip().lower() if row and row[0] else "ege"
            else:
                cursor.execute(
                    "UPDATE user_settings SET exam_level = CASE WHEN COALESCE(exam_level, 'ege') = 'oge' THEN 'ege' ELSE 'oge' END, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?;",
                    (user_id,),
                )
                cursor.execute(
                    "SELECT COALESCE(exam_level, 'ege') FROM user_settings WHERE user_id = ?;",
                    (user_id,),
                )
                row = cursor.fetchone()
                new_level = str(row[0]).strip().lower() if row and row[0] else "ege"
            conn.commit()
        finally:
            cursor.close()
            conn.close()

        clean_new = "oge" if new_level == "oge" else "ege"
        with self._cache_lock:
            self._write_seq += 1
            self._user_settings_cache.pop(user_id, None)
        return clean_new

    def accept_user_policy(self, user_id: int):
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            self._ensure_user_settings_row(cursor, user_id)
            if self.is_postgres:
                cursor.execute(
                    "UPDATE user_settings SET policy_accepted = 1, updated_at = CURRENT_TIMESTAMP WHERE user_id = %s;",
                    (user_id,),
                )
                cursor.execute(
                    "SELECT token_version, reminders_enabled, COALESCE(exam_level, 'ege') FROM user_settings WHERE user_id = %s;",
                    (user_id,),
                )
            else:
                cursor.execute(
                    "UPDATE user_settings SET policy_accepted = 1, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?;",
                    (user_id,),
                )
                cursor.execute(
                    "SELECT token_version, reminders_enabled, COALESCE(exam_level, 'ege') FROM user_settings WHERE user_id = ?;",
                    (user_id,),
                )
            row = cursor.fetchone()
            conn.commit()
            t_ver = int(row[0]) if row and row[0] is not None else 1
            rem_on = bool(int(row[1])) if row and row[1] is not None else True
            raw_lvl = (
                str(row[2] if row and len(row) > 2 and row[2] is not None else "ege")
                .strip()
                .lower()
            )
            exam_lvl = "oge" if raw_lvl == "oge" else "ege"
            with self._cache_lock:
                self._write_seq += 1
                self._evict_oldest_locked(self._user_settings_cache)
                self._user_settings_cache[user_id] = (
                    time.time(),
                    t_ver,
                    rem_on,
                    True,
                    exam_lvl,
                )
        finally:
            cursor.close()
            conn.close()

    def rotate_user_token(self, user_id: int) -> int:
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            self._ensure_user_settings_row(cursor, user_id)
            if self.is_postgres:
                cursor.execute(
                    "UPDATE user_settings SET token_version = token_version + 1, updated_at = CURRENT_TIMESTAMP WHERE user_id = %s RETURNING token_version, reminders_enabled, COALESCE(policy_accepted, 0), COALESCE(exam_level, 'ege');",
                    (user_id,),
                )
                row = cursor.fetchone()
            else:
                cursor.execute(
                    "UPDATE user_settings SET token_version = token_version + 1, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?;",
                    (user_id,),
                )
                cursor.execute(
                    "SELECT token_version, reminders_enabled, COALESCE(policy_accepted, 0), COALESCE(exam_level, 'ege') FROM user_settings WHERE user_id = ?;",
                    (user_id,),
                )
                row = cursor.fetchone()
            new_ver = int(row[0]) if row else 2
            rem_on = bool(int(row[1])) if row and row[1] is not None else True
            pol_ok = bool(int(row[2])) if row and row[2] is not None else True
            raw_lvl = (
                str(row[3] if row and len(row) > 3 and row[3] is not None else "ege")
                .strip()
                .lower()
            )
            exam_lvl = "oge" if raw_lvl == "oge" else "ege"
            conn.commit()
            with self._cache_lock:
                self._write_seq += 1
                self._evict_oldest_locked(self._user_settings_cache)
                self._user_settings_cache[user_id] = (
                    time.time(),
                    new_ver,
                    rem_on,
                    pol_ok,
                    exam_lvl,
                )
            return new_ver
        finally:
            cursor.close()
            conn.close()

    def toggle_user_reminders(self, user_id: int) -> bool:
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            self._ensure_user_settings_row(cursor, user_id)
            if self.is_postgres:
                cursor.execute(
                    "UPDATE user_settings SET reminders_enabled = CASE WHEN reminders_enabled = 1 THEN 0 ELSE 1 END, updated_at = CURRENT_TIMESTAMP WHERE user_id = %s;",
                    (user_id,),
                )
                cursor.execute(
                    "SELECT token_version, reminders_enabled, COALESCE(policy_accepted, 0), COALESCE(exam_level, 'ege') FROM user_settings WHERE user_id = %s;",
                    (user_id,),
                )
            else:
                cursor.execute(
                    "UPDATE user_settings SET reminders_enabled = CASE WHEN reminders_enabled = 1 THEN 0 ELSE 1 END, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?;",
                    (user_id,),
                )
                cursor.execute(
                    "SELECT token_version, reminders_enabled, COALESCE(policy_accepted, 0), COALESCE(exam_level, 'ege') FROM user_settings WHERE user_id = ?;",
                    (user_id,),
                )
            row = cursor.fetchone()
            conn.commit()
            t_ver = int(row[0]) if row and row[0] is not None else 1
            rem_on = bool(int(row[1])) if row and row[1] is not None else True
            pol_ok = bool(int(row[2])) if row and row[2] is not None else True
            raw_lvl = (
                str(row[3] if row and len(row) > 3 and row[3] is not None else "ege")
                .strip()
                .lower()
            )
            exam_lvl = "oge" if raw_lvl == "oge" else "ege"
            with self._cache_lock:
                self._write_seq += 1
                self._evict_oldest_locked(self._user_settings_cache)
                self._user_settings_cache[user_id] = (
                    time.time(),
                    t_ver,
                    rem_on,
                    pol_ok,
                    exam_lvl,
                )
            return rem_on
        finally:
            cursor.close()
            conn.close()

    def mark_reminder_sent(self, user_id: int, date_iso: str):
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            self._ensure_user_settings_row(cursor, user_id)
            if self.is_postgres:
                cursor.execute(
                    "UPDATE user_settings SET last_reminder_date = %s, updated_at = CURRENT_TIMESTAMP WHERE user_id = %s;",
                    (date_iso, user_id),
                )
            else:
                cursor.execute(
                    "UPDATE user_settings SET last_reminder_date = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?;",
                    (date_iso, user_id),
                )
            conn.commit()
        finally:
            cursor.close()
            conn.close()

    def get_users_for_daily_reminder(
        self, reference_date: Optional[date] = None
    ) -> List[Dict[str, Any]]:
        """
        Пакетный расчёт кандидатов для вечерних напоминаний за 1 подключение к БД (без N+1 запросов).
        """
        self.flush_pending_writes_sync()
        today_dt = reference_date or get_local_today()
        today_iso = today_dt.isoformat()
        yesterday_dt = today_dt - timedelta(days=1)
        cutoff_dt = datetime(
            yesterday_dt.year,
            yesterday_dt.month,
            yesterday_dt.day,
            0,
            0,
            0,
            tzinfo=TZ,
        ).astimezone(timezone.utc)

        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            recent_uids_set: Set[int] = set()
            if self.is_postgres:
                cursor.execute(
                    "SELECT DISTINCT user_id FROM user_answers WHERE created_at >= %s;",
                    (cutoff_dt,),
                )
                for r in cursor.fetchall():
                    if r and r[0] is not None:
                        recent_uids_set.add(int(r[0]))
                cursor.execute(
                    "SELECT DISTINCT user_id FROM user_daily_archive WHERE archive_date >= %s;",
                    (yesterday_dt,),
                )
                for r in cursor.fetchall():
                    if r and r[0] is not None:
                        recent_uids_set.add(int(r[0]))
            else:
                cutoff_str = cutoff_dt.strftime("%Y-%m-%d %H:%M:%S")
                cursor.execute(
                    "SELECT DISTINCT user_id FROM user_answers WHERE created_at >= ?;",
                    (cutoff_str,),
                )
                for r in cursor.fetchall():
                    if r and r[0] is not None:
                        recent_uids_set.add(int(r[0]))
                cursor.execute(
                    "SELECT DISTINCT user_id FROM user_daily_archive WHERE archive_date >= ?;",
                    (yesterday_dt.isoformat(),),
                )
                for r in cursor.fetchall():
                    if r and r[0] is not None:
                        recent_uids_set.add(int(r[0]))

            recent_uids = sorted(recent_uids_set)
            if not recent_uids:
                return []

            settings_map: Dict[int, Tuple[bool, str]] = {}
            batch_size = 500
            for i in range(0, len(recent_uids), batch_size):
                batch = recent_uids[i : i + batch_size]
                if self.is_postgres:
                    cursor.execute(
                        "SELECT user_id, reminders_enabled, last_reminder_date FROM user_settings WHERE user_id = ANY(%s);",
                        (batch,),
                    )
                else:
                    placeholders = ",".join("?" for _ in batch)
                    cursor.execute(
                        f"SELECT user_id, reminders_enabled, last_reminder_date FROM user_settings WHERE user_id IN ({placeholders});",
                        batch,
                    )
                for r in cursor.fetchall():
                    settings_map[int(r[0])] = (
                        bool(int(r[1])) if r[1] is not None else True,
                        str(r[2] or ""),
                    )

            target_uids = [
                uid
                for uid in recent_uids
                if settings_map.get(uid, (True, ""))[0]
                and settings_map.get(uid, (True, ""))[1] != today_iso
            ]
            if not target_uids:
                return []

            # Групповая выгрузка истории по всем target_uids за 1 соединение (без N+1)
            user_rows_map: Dict[int, List[Tuple[str, Any]]] = {
                uid: [] for uid in target_uids
            }
            for i in range(0, len(target_uids), batch_size):
                batch = target_uids[i : i + batch_size]
                if self.is_postgres:
                    cursor.execute(
                        """
                        SELECT user_id, archive_date, mode, code, tries_count, total_time_sec 
                        FROM user_daily_archive WHERE user_id = ANY(%s);
                        """,
                        (batch,),
                    )
                else:
                    placeholders = ",".join("?" for _ in batch)
                    cursor.execute(
                        f"""
                        SELECT user_id, archive_date, mode, code, tries_count, total_time_sec 
                        FROM user_daily_archive WHERE user_id IN ({placeholders});
                        """,
                        batch,
                    )
                for ar in cursor.fetchall():
                    uid_val = int(ar[0])
                    a_date = ar[1]
                    a_mode = str(ar[2] or "card").strip()
                    a_code = str(ar[3] or "1.01").strip()
                    a_tries = max(1, int(ar[4] or 1))
                    a_sec = round(max(0.0, float(ar[5] or 0.0)) / a_tries, 1)
                    if (
                        a_mode == "test"
                        and a_sec < SpacedRepetitionAlgorithm.MIN_TIME_WINDOW
                    ):
                        a_sec = SpacedRepetitionAlgorithm.MIN_TIME_WINDOW
                    ts_val = (
                        a_date
                        if isinstance(a_date, date)
                        else f"{str(a_date)[:10]}T12:00:00+00:00"
                    )
                    user_rows_map[uid_val].append(
                        (f"{a_code};-1.0;0.0;{a_sec};{a_mode}", ts_val)
                    )

                if self.is_postgres:
                    cursor.execute(
                        "SELECT user_id, entry, created_at FROM user_answers WHERE user_id = ANY(%s);",
                        (batch,),
                    )
                else:
                    placeholders = ",".join("?" for _ in batch)
                    cursor.execute(
                        f"SELECT user_id, entry, created_at FROM user_answers WHERE user_id IN ({placeholders});",
                        batch,
                    )
                for r in cursor.fetchall():
                    uid_val = int(r[0])
                    user_rows_map[uid_val].append((str(r[1]), r[2]))
        finally:
            cursor.close()
            conn.close()

        candidates = []
        for uid in target_uids:
            streak_info = self.get_user_streak_info(
                uid, reference_date=today_dt, pre_rows=user_rows_map.get(uid, [])
            )
            if streak_info["current_streak"] >= 1 and streak_info["today_actions"] == 0:
                candidates.append(
                    {
                        "user_id": uid,
                        "current_streak": streak_info["current_streak"],
                    }
                )
        return candidates

    def get_daily_admin_digest(
        self, reference_date: Optional[date] = None
    ) -> Dict[str, Any]:
        """
        Собирает суточную сводку по пользователям и активности для отправки в канал логов (LOG_CHAT_ID).
        Выполняется за 1 подключение к БД.
        """
        self.flush_pending_writes_sync()
        today_dt = reference_date or get_local_today()
        start_local = datetime(
            today_dt.year, today_dt.month, today_dt.day, 0, 0, 0, tzinfo=TZ
        )
        end_local = start_local + timedelta(days=1)
        start_utc = start_local.astimezone(timezone.utc)
        end_utc = end_local.astimezone(timezone.utc)

        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            all_users_set: Set[int] = set()
            for tbl in ("user_settings", "user_answers", "user_daily_archive"):
                try:
                    cursor.execute(f"SELECT DISTINCT user_id FROM {tbl};")
                    for r in cursor.fetchall():
                        if r and r[0] is not None:
                            all_users_set.add(int(r[0]))
                except Exception:
                    pass
            total_users = len(all_users_set)

            # Пользователи, у которых были действия ДО сегодняшнего дня
            prior_users_set: Set[int] = set()
            try:
                if self.is_postgres:
                    cursor.execute(
                        "SELECT DISTINCT user_id FROM user_answers WHERE created_at < %s;",
                        (start_utc,),
                    )
                else:
                    cursor.execute(
                        "SELECT DISTINCT user_id FROM user_answers WHERE created_at < ?;",
                        (start_utc.strftime("%Y-%m-%d %H:%M:%S"),),
                    )
                for r in cursor.fetchall():
                    if r and r[0] is not None:
                        prior_users_set.add(int(r[0]))

                if self.is_postgres:
                    cursor.execute(
                        "SELECT DISTINCT user_id FROM user_daily_archive WHERE archive_date < %s;",
                        (today_dt,),
                    )
                else:
                    cursor.execute(
                        "SELECT DISTINCT user_id FROM user_daily_archive WHERE archive_date < ?;",
                        (today_dt.isoformat(),),
                    )
                for r in cursor.fetchall():
                    if r and r[0] is not None:
                        prior_users_set.add(int(r[0]))
            except Exception:
                pass

            # Действия за сегодня
            dau_users_set: Set[int] = set()
            cards_today = 0
            tests_today = 0
            errors_today = 0

            if self.is_postgres:
                cursor.execute(
                    "SELECT user_id, entry FROM user_answers WHERE created_at >= %s AND created_at < %s;",
                    (start_utc, end_utc),
                )
            else:
                cursor.execute(
                    "SELECT user_id, entry FROM user_answers WHERE created_at >= ? AND created_at < ?;",
                    (
                        start_utc.strftime("%Y-%m-%d %H:%M:%S"),
                        end_utc.strftime("%Y-%m-%d %H:%M:%S"),
                    ),
                )
            for r in cursor.fetchall():
                if not r or r[0] is None:
                    continue
                uid_val = int(r[0])
                entry_str = str(r[1] or "")
                parts = entry_str.split(";")
                if len(parts) < 4:
                    continue
                try:
                    d_ans = float(parts[1])
                    sec = float(parts[3])
                except ValueError:
                    continue
                mode = parts[4].strip() if len(parts) >= 5 else "card"
                if mode == "test" and sec < SpacedRepetitionAlgorithm.MIN_TIME_WINDOW:
                    continue
                dau_users_set.add(uid_val)
                if mode == "test":
                    tests_today += 1
                else:
                    cards_today += 1
                if d_ans > 0:
                    errors_today += 1

            new_users_today = len(dau_users_set - prior_users_set)
            total_actions_today = cards_today + tests_today
            accuracy_today = (
                round((1.0 - errors_today / total_actions_today) * 100, 1)
                if total_actions_today > 0
                else 0.0
            )

            teachers_count = 0
            connected_students_count = 0
            try:
                cursor.execute("SELECT COUNT(*) FROM teachers;")
                row_t = cursor.fetchone()
                teachers_count = int(row_t[0]) if row_t and row_t[0] else 0

                cursor.execute("SELECT COUNT(DISTINCT student_id) FROM student_teachers;")
                row_st = cursor.fetchone()
                connected_students_count = (
                    int(row_st[0]) if row_st and row_st[0] else 0
                )
            except Exception:
                pass

            return {
                "date_str": today_dt.strftime("%d.%m.%Y"),
                "total_users": total_users,
                "new_users_today": new_users_today,
                "dau": len(dau_users_set),
                "total_actions_today": total_actions_today,
                "cards_today": cards_today,
                "tests_today": tests_today,
                "accuracy_today": accuracy_today,
                "teachers_count": teachers_count,
                "connected_students_count": connected_students_count,
            }
        finally:
            cursor.close()
            conn.close()

    # --- УЧИТЕЛЯ И КЛАССЫ ---

    def cleanup_inactive_teachers(self):
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            try:
                if self.is_postgres:
                    cursor.execute(
                        """
                        DELETE FROM teachers t
                        WHERE t.created_at < CURRENT_TIMESTAMP - INTERVAL '7 days'
                          AND NOT EXISTS (
                              SELECT 1 FROM student_teachers st WHERE st.teacher_code = t.teacher_code
                          );
                    """
                    )
                    cursor.execute(
                        """
                        DELETE FROM student_teachers st
                        WHERE NOT EXISTS (
                            SELECT 1 FROM teachers t WHERE t.teacher_code = st.teacher_code
                        );
                    """
                    )
                else:
                    cursor.execute(
                        """
                        DELETE FROM teachers
                        WHERE created_at < datetime('now', '-7 days')
                          AND NOT EXISTS (
                              SELECT 1 FROM student_teachers st WHERE st.teacher_code = teachers.teacher_code
                          );
                    """
                    )
                    cursor.execute(
                        """
                        DELETE FROM student_teachers
                        WHERE NOT EXISTS (
                            SELECT 1 FROM teachers t WHERE t.teacher_code = student_teachers.teacher_code
                        );
                    """
                    )
                conn.commit()
            finally:
                cursor.close()
                conn.close()
        except Exception as e:
            logger.warning(f"Ошибка при очистке неактивных учителей: {e}")

    def get_teacher(self, teacher_code: str) -> Optional[Dict[str, Any]]:
        clean_code = str(teacher_code or "").strip()
        if not clean_code or not clean_code.isdigit():
            return None
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            if self.is_postgres:
                cursor.execute(
                    "SELECT teacher_code, user_id, full_name, created_at, COALESCE(token_version, 1) FROM teachers WHERE teacher_code = %s;",
                    (clean_code,),
                )
            else:
                cursor.execute(
                    "SELECT teacher_code, user_id, full_name, created_at, COALESCE(token_version, 1) FROM teachers WHERE teacher_code = ?;",
                    (clean_code,),
                )
            row = cursor.fetchone()
        finally:
            cursor.close()
            conn.close()

        if row:
            return {
                "teacher_code": str(row[0]),
                "user_id": int(row[1]),
                "full_name": str(row[2]) if row[2] else f"Учитель {row[0]}",
                "created_at": row[3],
                "token_version": int(
                    row[4] if len(row) > 4 and row[4] is not None else 1
                ),
            }
        return None

    def update_teacher_name(self, user_id: int, full_name: str):
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            if self.is_postgres:
                cursor.execute(
                    "UPDATE teachers SET full_name = %s WHERE user_id = %s;",
                    (full_name, user_id),
                )
            else:
                cursor.execute(
                    "UPDATE teachers SET full_name = ? WHERE user_id = ?;",
                    (full_name, user_id),
                )
            conn.commit()
        except Exception:
            pass
        finally:
            cursor.close()
            conn.close()

    def get_teacher_by_user_id(self, user_id: int) -> Optional[Dict[str, Any]]:
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            if self.is_postgres:
                cursor.execute(
                    "SELECT teacher_code, user_id, full_name, created_at, COALESCE(token_version, 1) FROM teachers WHERE user_id = %s;",
                    (user_id,),
                )
            else:
                cursor.execute(
                    "SELECT teacher_code, user_id, full_name, created_at, COALESCE(token_version, 1) FROM teachers WHERE user_id = ?;",
                    (user_id,),
                )
            row = cursor.fetchone()
        finally:
            cursor.close()
            conn.close()

        if row:
            return {
                "teacher_code": str(row[0]),
                "user_id": int(row[1]),
                "full_name": str(row[2]) if row[2] else f"Учитель {row[0]}",
                "created_at": row[3],
                "token_version": int(
                    row[4] if len(row) > 4 and row[4] is not None else 1
                ),
            }
        return None

    def rotate_teacher_token(self, user_id: int) -> Optional[Tuple[str, int]]:
        existing = self.get_teacher_by_user_id(user_id)
        if not existing:
            return None
        code = existing["teacher_code"]
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            if self.is_postgres:
                cursor.execute(
                    "UPDATE teachers SET token_version = COALESCE(token_version, 1) + 1 WHERE teacher_code = %s RETURNING token_version;",
                    (code,),
                )
                row = cursor.fetchone()
                new_ver = int(row[0]) if row else existing["token_version"] + 1
            else:
                cursor.execute(
                    "UPDATE teachers SET token_version = COALESCE(token_version, 1) + 1 WHERE teacher_code = ?;",
                    (code,),
                )
                cursor.execute(
                    "SELECT COALESCE(token_version, 1) FROM teachers WHERE teacher_code = ?;",
                    (code,),
                )
                row = cursor.fetchone()
                new_ver = int(row[0]) if row else existing["token_version"] + 1
            conn.commit()
            return code, new_ver
        finally:
            cursor.close()
            conn.close()

    def register_teacher(self, user_id: int, full_name: str) -> str:
        """Потокобезопасная регистрация учителя с защитой от гонок (ON CONFLICT DO NOTHING)."""
        existing = self.get_teacher_by_user_id(user_id)
        if existing:
            return existing["teacher_code"]

        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            for _ in range(1000):
                if self.is_postgres:
                    cursor.execute(
                        "SELECT teacher_code FROM teachers WHERE user_id = %s;",
                        (user_id,),
                    )
                else:
                    cursor.execute(
                        "SELECT teacher_code FROM teachers WHERE user_id = ?;",
                        (user_id,),
                    )
                row = cursor.fetchone()
                if row:
                    return str(row[0])

                candidate = str(random.randint(10000000, 99999999))
                try:
                    if self.is_postgres:
                        cursor.execute(
                            """
                            INSERT INTO teachers (teacher_code, user_id, full_name, token_version)
                            VALUES (%s, %s, %s, 1)
                            ON CONFLICT DO NOTHING
                            RETURNING teacher_code;
                            """,
                            (candidate, user_id, full_name),
                        )
                        ins_row = cursor.fetchone()
                        conn.commit()
                        if ins_row:
                            logger.info(
                                f"Зарегистрирован новый преподаватель {full_name} с кодом {candidate}."
                            )
                            return str(ins_row[0])
                    else:
                        cursor.execute(
                            "INSERT OR IGNORE INTO teachers (teacher_code, user_id, full_name, token_version) VALUES (?, ?, ?, 1);",
                            (candidate, user_id, full_name),
                        )
                        conn.commit()
                        if cursor.rowcount and cursor.rowcount > 0:
                            logger.info(
                                f"Зарегистрирован новый преподаватель {full_name} с кодом {candidate}."
                            )
                            return candidate
                except Exception:
                    try:
                        conn.rollback()
                    except Exception:
                        pass

            raise RuntimeError(
                "Не удалось сгенерировать уникальный 8-значный код учителя."
            )
        finally:
            cursor.close()
            conn.close()

    def is_student_connected(self, student_id: int, teacher_code: str) -> bool:
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            if self.is_postgres:
                cursor.execute(
                    "SELECT 1 FROM student_teachers WHERE student_id = %s AND teacher_code = %s;",
                    (student_id, str(teacher_code)),
                )
            else:
                cursor.execute(
                    "SELECT 1 FROM student_teachers WHERE student_id = ? AND teacher_code = ?;",
                    (student_id, str(teacher_code)),
                )
            row = cursor.fetchone()
        finally:
            cursor.close()
            conn.close()
        return bool(row)

    def connect_student_to_teacher(self, student_id: int, teacher_code: str):
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            if self.is_postgres:
                cursor.execute(
                    """
                    INSERT INTO student_teachers (student_id, teacher_code) 
                    VALUES (%s, %s) 
                    ON CONFLICT (student_id, teacher_code) DO NOTHING;
                """,
                    (student_id, str(teacher_code)),
                )
            else:
                cursor.execute(
                    """
                    INSERT OR IGNORE INTO student_teachers (student_id, teacher_code) 
                    VALUES (?, ?);
                """,
                    (student_id, str(teacher_code)),
                )
            conn.commit()
        finally:
            cursor.close()
            conn.close()

    def disconnect_student_from_teacher(self, student_id: int, teacher_code: str):
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            if self.is_postgres:
                cursor.execute(
                    "DELETE FROM student_teachers WHERE student_id = %s AND teacher_code = %s;",
                    (student_id, str(teacher_code)),
                )
            else:
                cursor.execute(
                    "DELETE FROM student_teachers WHERE student_id = ? AND teacher_code = ?;",
                    (student_id, str(teacher_code)),
                )
            conn.commit()
        finally:
            cursor.close()
            conn.close()

    def get_student_teachers(self, student_id: int) -> List[Dict[str, Any]]:
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            query = (
                """
                SELECT t.teacher_code, t.full_name, st.joined_at 
                FROM student_teachers st
                JOIN teachers t ON st.teacher_code = t.teacher_code
                WHERE st.student_id = %s
                ORDER BY st.joined_at DESC;
            """
                if self.is_postgres
                else """
                SELECT t.teacher_code, t.full_name, st.joined_at 
                FROM student_teachers st
                JOIN teachers t ON st.teacher_code = t.teacher_code
                WHERE st.student_id = ?
                ORDER BY st.joined_at DESC;
            """
            )
            cursor.execute(query, (student_id,))
            rows = cursor.fetchall()
        finally:
            cursor.close()
            conn.close()

        teachers = []
        for r in rows:
            teachers.append(
                {
                    "teacher_code": str(r[0]),
                    "full_name": str(r[1]) if r[1] else f"Учитель {r[0]}",
                    "joined_at": r[2],
                }
            )
        return teachers

    def get_teacher_students_count(self, teacher_code: str) -> int:
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            if self.is_postgres:
                cursor.execute(
                    "SELECT COUNT(*) FROM student_teachers WHERE teacher_code = %s;",
                    (str(teacher_code),),
                )
            else:
                cursor.execute(
                    "SELECT COUNT(*) FROM student_teachers WHERE teacher_code = ?;",
                    (str(teacher_code),),
                )
            row = cursor.fetchone()
        finally:
            cursor.close()
            conn.close()

        return int(row[0]) if row else 0

    def delete_user_data(self, user_id: int):
        """Полностью стирает историю ответов, настройки и привязки пользователя."""
        with self._write_queue_lock:
            self._pending_writes = [
                item for item in self._pending_writes if item[0] != user_id
            ]
        self.flush_pending_writes_sync()
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            if self.is_postgres:
                cursor.execute(
                    "DELETE FROM user_answers WHERE user_id = %s;", (user_id,)
                )
                cursor.execute(
                    "DELETE FROM user_daily_archive WHERE user_id = %s;", (user_id,)
                )
                cursor.execute(
                    "DELETE FROM user_settings WHERE user_id = %s;", (user_id,)
                )
                cursor.execute(
                    "DELETE FROM student_teachers WHERE student_id = %s;", (user_id,)
                )
                cursor.execute(
                    "DELETE FROM student_teachers WHERE teacher_code IN (SELECT teacher_code FROM teachers WHERE user_id = %s);",
                    (user_id,),
                )
                cursor.execute(
                    "DELETE FROM teachers WHERE user_id = %s;", (user_id,)
                )
            else:
                cursor.execute(
                    "DELETE FROM user_answers WHERE user_id = ?;", (user_id,)
                )
                cursor.execute(
                    "DELETE FROM user_daily_archive WHERE user_id = ?;", (user_id,)
                )
                cursor.execute(
                    "DELETE FROM user_settings WHERE user_id = ?;", (user_id,)
                )
                cursor.execute(
                    "DELETE FROM student_teachers WHERE student_id = ?;", (user_id,)
                )
                cursor.execute(
                    "DELETE FROM student_teachers WHERE teacher_code IN (SELECT teacher_code FROM teachers WHERE user_id = ?);",
                    (user_id,),
                )
                cursor.execute(
                    "DELETE FROM teachers WHERE user_id = ?;", (user_id,)
                )
            conn.commit()
        finally:
            cursor.close()
            conn.close()
        self.invalidate_user_cache(user_id)



    def get_recent_autopost_history(
        self, limit_terms: int = 30, limit_urls: int = 100
    ) -> Tuple[Set[str], Set[str]]:
        conn = self.get_connection()
        cursor = conn.cursor()
        recent_terms: Set[str] = set()
        recent_urls: Set[str] = set()
        try:
            cursor.execute(
                ("SELECT term_code, news_url FROM autopost_history ORDER BY id DESC LIMIT %s" if self.is_postgres else "SELECT term_code, news_url FROM autopost_history ORDER BY id DESC LIMIT ?"),
                (max(limit_terms, limit_urls),),
            )
            rows = cursor.fetchall()
            for idx, row in enumerate(rows):
                t_code = (row[0] or "").strip()
                n_url = (row[1] or "").strip()
                if t_code and idx < limit_terms:
                    recent_terms.add(t_code)
                if n_url and idx < limit_urls:
                    recent_urls.add(n_url)
            return recent_terms, recent_urls
        finally:
            cursor.close()
            conn.close()

    def record_autopost_published(
        self, term_code: str, news_url: str, format_type: str
    ) -> None:
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                ("INSERT INTO autopost_history (term_code, news_url, format_type) VALUES (%s, %s, %s)" if self.is_postgres else "INSERT INTO autopost_history (term_code, news_url, format_type) VALUES (?, ?, ?)"),
                (term_code.strip(), (news_url or "").strip(), format_type.strip()),
            )
            conn.commit()
        finally:
            cursor.close()
            conn.close()

    def save_autopost_draft(self, draft_id: str, payload: Dict[str, Any]) -> None:
        conn = self.get_connection()
        cursor = conn.cursor()
        raw = json.dumps(payload, ensure_ascii=False)
        try:
            if self.is_postgres:
                cursor.execute(
                    """
                    INSERT INTO autopost_drafts (draft_id, payload_json)
                    VALUES (%s, %s)
                    ON CONFLICT (draft_id) DO UPDATE SET
                        payload_json = EXCLUDED.payload_json,
                        created_at = CURRENT_TIMESTAMP
                    """,
                    (draft_id, raw),
                )
            else:
                cursor.execute(
                    """
                    INSERT INTO autopost_drafts (draft_id, payload_json)
                    VALUES (?, ?)
                    ON CONFLICT (draft_id) DO UPDATE SET
                        payload_json = excluded.payload_json,
                        created_at = CURRENT_TIMESTAMP
                    """,
                    (draft_id, raw),
                )
            if self.is_postgres:
                cursor.execute(
                    "DELETE FROM autopost_drafts WHERE created_at < NOW() - INTERVAL '7 days'"
                )
            else:
                cursor.execute(
                    "DELETE FROM autopost_drafts WHERE created_at < datetime('now', '-7 days')"
                )
            conn.commit()
        finally:
            cursor.close()
            conn.close()

    def get_autopost_draft(self, draft_id: str) -> Optional[Dict[str, Any]]:
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                ("SELECT payload_json FROM autopost_drafts WHERE draft_id = %s" if self.is_postgres else "SELECT payload_json FROM autopost_drafts WHERE draft_id = ?"),
                (draft_id,),
            )
            row = cursor.fetchone()
            if not row or not row[0]:
                return None
            return json.loads(row[0])
        finally:
            cursor.close()
            conn.close()

    def delete_autopost_draft(self, draft_id: str) -> None:
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                ("DELETE FROM autopost_drafts WHERE draft_id = %s" if self.is_postgres else "DELETE FROM autopost_drafts WHERE draft_id = ?"),
                (draft_id,),
            )
            conn.commit()
        finally:
            cursor.close()
            conn.close()

db = Database()


def select_next_card(
    user_id: int,
    module_filter: Optional[int] = None,
    custom_codes: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    if custom_codes is not None:
        available_terms = [
            catalog.get_by_code(c) for c in custom_codes if catalog.get_by_code(c)
        ]
    else:
        available_terms = catalog.get_terms(module_filter)

    if not available_terms:
        return None

    progress_map = db.get_user_progress_map(user_id)
    recent_codes = set(session_tracker.get_recent(user_id))

    candidates = [t for t in available_terms if t["code"] not in recent_codes]
    if not candidates:
        last_shown = (
            session_tracker.get_recent(user_id)[-1]
            if session_tracker.get_recent(user_id)
            else None
        )
        if len(available_terms) > 1 and last_shown is not None:
            candidates = [t for t in available_terms if t["code"] != last_shown]
        if not candidates:
            candidates = available_terms

    def get_effective_weight(term_dict: Dict[str, Any]) -> float:
        code = term_dict["code"]
        if code not in progress_map or progress_map[code]["review_count"] == 0:
            return SpacedRepetitionAlgorithm.UNREVIEWED_PRIORITY_WEIGHT
        return max(
            SpacedRepetitionAlgorithm.MIN_WEIGHT,
            float(progress_map[code]["total_weight"]),
        )

    weights = [get_effective_weight(t) for t in candidates]
    chosen = random.choices(candidates, weights=weights, k=1)[0]
    session_tracker.push(user_id, chosen["code"])
    return chosen
