import asyncio
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from html import escape

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from dotenv import load_dotenv

import db

# .env нужно загрузить ДО того, как читаем из него значения
load_dotenv()
BOT_TOKEN = os.getenv('BOT_TOKEN')
ADMIN_RAW = os.getenv('ADMIN_ID')

# Понятная ошибка вместо "TypeError: int() argument must be... NoneType"
if not BOT_TOKEN or not ADMIN_RAW:
    raise SystemExit('Заполни BOT_TOKEN и ADMIN_ID в файле .env')

bot = Bot(token=BOT_TOKEN)
ADMIN_ID = int(ADMIN_RAW)
dp = Dispatcher()


# ---------- Данные салона ----------
# Все услуги в одном месте. Чтобы добавить услугу, достаточно дописать
# один блок: кнопки, карточка и запись подтянутся сами.

SERVICES = {
    'cut': {
        'icon': '✂️',
        'title': 'Стрижка',
        'price': 20,
        'duration': '45 мин',
        'desc': 'Мужская и женская, с мытьём головы и укладкой.',
    },
    'color': {
        'icon': '🎨',
        'title': 'Окрашивание',
        'price': 60,
        'duration': '2 часа',
        'desc': 'Однотонное окрашивание, оттенок подбираем вместе с мастером.',
    },
}

ADDRESS = 'ул. Примерная, 1'
CURRENCY = 'byn'

# Для красивого вывода заявок: иконка по названию услуги и местное время
SERVICE_ICONS = {s['title']: s['icon'] for s in SERVICES.values()}
LOCAL_TZ = timezone(timedelta(hours=3))  # Минск, UTC+3

PRICE_LIST_TEXT = 'Выберите услугу, чтобы узнать подробности:'
TIME_PROMPT = 'На какой день и время записать? Например: завтра 14:30'


def service_card_text(s: dict) -> str:
    """Текст карточки услуги."""
    return (
        f'{s["icon"]} {s["title"]}\n\n'
        f'Стоимость: {s["price"]} {CURRENCY}\n'
        f'Длительность: {s["duration"]}\n\n'
        f'{s["desc"]}'
    )


# ---------- Клавиатуры ----------

BTN_PRICE = '💰 Прайс'
BTN_CONTACTS = '📞 Контакты'
BTN_REQUEST = '📝 Оставить заявку'

menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_PRICE), KeyboardButton(text=BTN_CONTACTS)],
        [KeyboardButton(text=BTN_REQUEST)],
    ],
    resize_keyboard=True,
)


def services_kb(prefix: str, with_price: bool = False) -> InlineKeyboardMarkup:
    """Кнопки услуг. prefix различает, куда ведёт нажатие:
    'info' — открыть карточку, 'book' — выбрать услугу в заявке."""
    rows = []
    for key, s in SERVICES.items():
        text = f'{s["icon"]} {s["title"]}'
        if with_price:
            text += f' — {s["price"]} {CURRENCY}'
        rows.append([InlineKeyboardButton(text=text, callback_data=f'{prefix}_{key}')])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def service_card_kb(key: str) -> InlineKeyboardMarkup:
    """Кнопки под карточкой услуги."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text='📝 Записаться на эту услугу',
                callback_data=f'order_{key}',
            )],
            [InlineKeyboardButton(
                text='← К списку услуг',
                callback_data='prices',
            )],
        ]
    )


# ---------- Состояния заявки ----------

class Request(StatesGroup):
    name = State()
    phone = State()
    service = State()
    time = State()


# ---------- Меню и команды ----------
# Эти хендлеры стоят ВЫШЕ хендлеров заявки: aiogram берёт первый подходящий.
# В каждом сбрасываем состояние, чтобы кнопка меню отменяла начатую заявку.

@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await message.answer(
    '🌸 Здравствуйте! Выберите, что вас интересует:',
    reply_markup=menu,
)


@dp.message(Command('about'))
async def about(message: Message, state: FSMContext):
    await state.clear()
    await message.answer('Я бот для записи клиентов в салон.')


@dp.message(Command('help'))
async def help_cmd(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        'Я умею:\n'
        '/start — открыть меню\n'
        '/cancel — отменить заявку\n'
        '/about — обо мне'
    )


@dp.message(Command('cancel'))
async def cancel(message: Message, state: FSMContext):
    # Узнаём, было ли что отменять, чтобы ответить честно
    was_active = await state.get_state() is not None
    await state.clear()
    if was_active:
        await message.answer('Заявка отменена.', reply_markup=menu)
    else:
        await message.answer('Сейчас нечего отменять.', reply_markup=menu)


def format_request(r) -> str:
    """Одна заявка из базы в виде красивого блока (HTML для Telegram)."""
    # SQLite хранит время в UTC, переводим в местное
    created = datetime.strptime(r['created_at'], '%Y-%m-%d %H:%M:%S')
    created = created.replace(tzinfo=timezone.utc).astimezone(LOCAL_TZ)

    icon = SERVICE_ICONS.get(r['service'], '💇')
    username = f'@{r["username"]}' if r['username'] else 'нет username'

    # escape защищает от клиентов, которые вводят в имени символы < >
    return (
        f'<b>№{r["id"]}</b> · {created:%d.%m %H:%M}\n'
        f'👤 {escape(r["name"])} · {escape(r["phone"])}\n'
        f'{icon} {escape(r["service"])} · {escape(r["time_text"])}\n'
        f'💬 {escape(username)}'
    )


@dp.message(Command('requests'))
async def show_requests(message: Message):
    # Команда только для админа: остальным бот просто не отвечает
    if message.from_user.id != ADMIN_ID:
        return

    rows = db.get_last_requests(10)
    if not rows:
        await message.answer('Заявок пока нет.')
        return

    blocks = '\n\n'.join(format_request(r) for r in rows)
    await message.answer(
        f'📋 <b>Последние заявки ({len(rows)})</b>\n\n{blocks}',
        parse_mode=ParseMode.HTML,
    )


@dp.message(F.text == BTN_PRICE)
async def price(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(PRICE_LIST_TEXT, reply_markup=services_kb('info'))


@dp.message(F.text == BTN_CONTACTS)
async def contacts(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(f'Наш адрес: {ADDRESS}.')


# ---------- Прайс: список → карточка → запись ----------
# Всё происходит в ОДНОМ сообщении: edit_text меняет его на месте,
# поэтому чат не засоряется новыми сообщениями.

@dp.callback_query(F.data.startswith('info_'))
async def service_info(callback: CallbackQuery):
    key = callback.data.removeprefix('info_')
    if key not in SERVICES:
        await callback.answer('Услуга не найдена', show_alert=True)
        return
    await callback.message.edit_text(
        service_card_text(SERVICES[key]),
        reply_markup=service_card_kb(key),
    )
    await callback.answer()  # убирает «часики» на кнопке


@dp.callback_query(F.data == 'prices')
async def back_to_prices(callback: CallbackQuery):
    await callback.message.edit_text(
        PRICE_LIST_TEXT,
        reply_markup=services_kb('info'),
    )
    await callback.answer()


# Кнопка «Записаться на эту услугу»: запускаем заявку с уже выбранной услугой
@dp.callback_query(F.data.startswith('order_'))
async def order_from_card(callback: CallbackQuery, state: FSMContext):
    key = callback.data.removeprefix('order_')
    if key not in SERVICES:
        await callback.answer('Услуга не найдена', show_alert=True)
        return

    await state.clear()
    await state.update_data(service=key)  # услуга уже известна
    await state.set_state(Request.name)
    await callback.message.answer(
        f'Записываем вас на «{SERVICES[key]["title"]}». '
        'Как вас зовут? (отмена: /cancel)'
    )
    await callback.answer()


# ---------- Заявка по шагам ----------

@dp.message(F.text == BTN_REQUEST)
async def start_request(message: Message, state: FSMContext):
    await state.clear()  # чистим старые данные, иначе останется чужая услуга
    await state.set_state(Request.name)
    await message.answer('Как вас зовут? (отмена: /cancel)')


@dp.message(Request.name, F.text)
async def get_name(message: Message, state: FSMContext):
    name = message.text.strip()
    if not 2 <= len(name) <= 50:
        await message.answer('Имя должно быть от 2 до 50 символов. Попробуйте ещё раз.')
        return

    await state.update_data(name=name)
    await state.set_state(Request.phone)
    await message.answer('Ваш телефон?')


@dp.message(Request.phone, F.text)
async def get_phone(message: Message, state: FSMContext):
    # оставляем только цифры и проверяем длину с обеих сторон
    digits = ''.join(ch for ch in message.text if ch.isdigit())
    if not 9 <= len(digits) <= 15:
        await message.answer('Похоже, номер неверный. Введите ещё раз.')
        return  # состояние не меняем, бот ждёт новый ответ

    await state.update_data(phone=message.text.strip())

    # Если услугу выбрали заранее (через карточку), шаг выбора пропускаем
    data = await state.get_data()
    if 'service' in data:
        await state.set_state(Request.time)
        await message.answer(TIME_PROMPT)
    else:
        await state.set_state(Request.service)
        await message.answer(
            'Выберите услугу:',
            reply_markup=services_kb('book', with_price=True),
        )


# Шаг «услуга» ждёт нажатие кнопки, поэтому здесь callback_query, а не message
@dp.callback_query(Request.service, F.data.startswith('book_'))
async def get_service(callback: CallbackQuery, state: FSMContext):
    key = callback.data.removeprefix('book_')
    if key not in SERVICES:
        await callback.answer('Услуга не найдена', show_alert=True)
        return

    await state.update_data(service=key)
    await state.set_state(Request.time)
    await callback.message.answer(TIME_PROMPT)
    await callback.answer()


@dp.message(Request.time, F.text)
async def get_time(message: Message, state: FSMContext):
    when = message.text.strip()
    # ищем время вида 14:30 или 14.30
    if not re.search(r'\d{1,2}[:.]\d{2}', when):
        await message.answer('Укажите время цифрами, например: завтра 14:30')
        return

    data = await state.get_data()
    s = SERVICES[data['service']]
    user = message.from_user

    # 1. Сначала сохраняем в базу: это главное, что нельзя потерять
    try:
        request_id = db.add_request(
            user_id=user.id,
            username=user.username,
            name=data['name'],
            phone=data['phone'],
            service=s['title'],
            time_text=when,
        )
    except sqlite3.Error:
        logging.exception('Не удалось сохранить заявку в базу')
        await message.answer(
            'Не получилось сохранить заявку. Отправьте время ещё раз.'
        )
        return  # состояние остаётся, данные клиента не потеряны

    # 2. Заявка в базе, диалог можно закрывать
    await state.clear()
    await message.answer(
        f'Спасибо, {data["name"]}! Заявка принята:\n'
        f'{s["icon"]} {s["title"]}, {when}\n\n'
        'Мы скоро свяжемся с вами для подтверждения.',
        reply_markup=menu,
    )

    # 3. Уведомление админу. Если оно не дошло, заявка всё равно в базе
    username = f'@{user.username}' if user.username else 'нет username'
    text = (
        f'Новая заявка №{request_id}!\n'
        f'Имя: {data["name"]}\n'
        f'Телефон: {data["phone"]}\n'
        f'Услуга: {s["title"]} ({s["price"]} {CURRENCY})\n'
        f'Время: {when}\n'
        f'Telegram: {username}'
    )
    try:
        await bot.send_message(ADMIN_ID, text)
    except TelegramAPIError:
        logging.exception(
            'Не удалось уведомить админа, заявка №%s в базе', request_id
        )


# ---------- Запасные хендлеры (если пришло не то, что ждём) ----------

# Стикер, фото и т.п. на шагах, где нужен текст
@dp.message(Request.name)
@dp.message(Request.phone)
@dp.message(Request.time)
async def not_text(message: Message):
    await message.answer('Пожалуйста, ответьте текстом.')


# Текст на шаге, где нужно нажать кнопку
@dp.message(Request.service)
async def not_button(message: Message):
    await message.answer('Выберите услугу кнопкой под сообщением выше 👆')


# ---------- Запуск ----------

async def main():
    logging.basicConfig(level=logging.INFO)
    db.init_db()  # создаст salon.db и таблицу при первом запуске
    await dp.start_polling(bot)


if __name__ == '__main__':
    asyncio.run(main())
