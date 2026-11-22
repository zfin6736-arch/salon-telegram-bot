import sqlite3
from contextlib import closing

DB_PATH = 'salon.db'


def init_db():
    """Создаёт файл базы и таблицу, если их ещё нет."""
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            '''
            CREATE TABLE IF NOT EXISTS requests (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                user_id    INTEGER,
                username   TEXT,
                name       TEXT,
                phone      TEXT,
                service    TEXT,
                time_text  TEXT
            )
            '''
        )
        conn.commit()


def add_request(user_id, username, name, phone, service, time_text):
    """Добавляет заявку и возвращает её номер (id)."""
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute(
            '''
            INSERT INTO requests (user_id, username, name, phone, service, time_text)
            VALUES (?, ?, ?, ?, ?, ?)
            ''',
            (user_id, username, name, phone, service, time_text),
        )
        conn.commit()  # без commit запись не сохранится в файл
        return cur.lastrowid


def get_last_requests(limit=10):
    """Возвращает последние заявки, самые новые первыми."""
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row  # строки как словари: row['name']
        cur = conn.execute(
            'SELECT * FROM requests ORDER BY id DESC LIMIT ?',
            (limit,),
        )
        return cur.fetchall()