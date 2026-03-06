import asyncio
import os
import io
import logging
import random
import shutil
import mimetypes
import subprocess
import threading
from pathlib import Path
from datetime import datetime
from enum import Enum, auto
from concurrent.futures import TimeoutError as FutureTimeoutError

from flask import Flask, Response, request
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)
from pydub import AudioSegment
try:
    import imageio_ffmpeg
    IMAGEIO_FFMPEG_OK = True
except ImportError:
    IMAGEIO_FFMPEG_OK = False

# mutagen для работы с тегами
try:
    from mutagen.mp3 import MP3
    from mutagen.id3 import (
        ID3, TIT2, TPE1, TALB, TRCK, TDRC, TCON,
        APIC, ID3NoHeaderError
    )
    from mutagen.id3 import ID3 as ID3Tags
    MUTAGEN_OK = True
except ImportError:
    MUTAGEN_OK = False

# ════════════════════════════════════════════
#  НАСТРОЙКИ
# ════════════════════════════════════════════
DEFAULT_ADMIN_IDS = {8206124108}

def _parse_admin_ids(raw_value: str) -> set[int]:
    ids: set[int] = set()
    for chunk in raw_value.split(","):
        chunk = chunk.strip()
        if chunk.isdigit():
            ids.add(int(chunk))
    return ids

def _resolve_ffmpeg_binary() -> str:
    env_bin = os.getenv("FFMPEG_BINARY", "").strip()
    if env_bin:
        return env_bin
    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg
    if IMAGEIO_FFMPEG_OK:
        try:
            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            pass
    return "ffmpeg"

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip() or "ВСТАВЬ_СЮДА_СВОЙ_TOKEN"
ADMIN_IDS = _parse_admin_ids(os.getenv("ADMIN_IDS", "")) or DEFAULT_ADMIN_IDS
SUPPORT_TAG = os.getenv("SUPPORT_TAG", "@grateful4you").strip() or "@grateful4you"
TEMP_DIR = Path(os.getenv("TEMP_DIR", "temp_files"))
TEMP_DIR.mkdir(parents=True, exist_ok=True)
RUN_MODE = os.getenv("RUN_MODE", "auto").strip().lower()
if RUN_MODE not in {"auto", "webhook", "polling"}:
    RUN_MODE = "auto"
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "0.0.0.0")
WEBHOOK_PORT = int(os.getenv("PORT", os.getenv("WEBHOOK_PORT", "10000")))
WEBHOOK_PATH = (os.getenv("WEBHOOK_PATH") or f"webhook/{BOT_TOKEN}").lstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip() or None
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()
if not WEBHOOK_URL:
    render_url = os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
    if render_url:
        WEBHOOK_URL = f"{render_url}/{WEBHOOK_PATH}"

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
ANIMATION_FRAMES = ["🌀", "✨", "🎚️", "🎛️", "🔊", "🎧", "⚡"]
ANIMATION_DELAY = 0.15
FFMPEG_BIN = _resolve_ffmpeg_binary()
AudioSegment.converter = FFMPEG_BIN
AudioSegment.ffmpeg = FFMPEG_BIN
if shutil.which("ffprobe"):
    AudioSegment.ffprobe = shutil.which("ffprobe")

def resolve_run_mode() -> str:
    if RUN_MODE in {"webhook", "polling"}:
        return RUN_MODE
    return "webhook" if WEBHOOK_URL else "polling"

def run_ffmpeg(args: list[str]) -> bool:
    try:
        result = subprocess.run(
            [FFMPEG_BIN, "-y", "-loglevel", "error", *args],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError:
        logger.error("FFmpeg binary not found: %s", FFMPEG_BIN)
        return False

    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="ignore").strip()
        if stderr:
            logger.error("FFmpeg error: %s", stderr)
        return False
    return True

def resolve_chat_action(*names: str):
    for name in names:
        action = getattr(ChatAction, name, None)
        if action is not None:
            return action
    return "typing"

CHAT_ACTION_UPLOAD_VIDEO = resolve_chat_action("UPLOAD_VIDEO", "RECORD_VIDEO", "TYPING")
CHAT_ACTION_UPLOAD_AUDIO = resolve_chat_action(
    "UPLOAD_AUDIO", "UPLOAD_DOCUMENT", "RECORD_AUDIO", "TYPING"
)

# ════════════════════════════════════════════
#  СОСТОЯНИЯ ДИАЛОГОВ (ConversationHandler)
# ════════════════════════════════════════════
class State(Enum):
    # Редактор тегов
    EDIT_WAIT_AUDIO    = auto()   # ждём аудиофайл
    EDIT_CHOOSE_ACTION = auto()   # выбор: что менять
    EDIT_WAIT_TITLE    = auto()   # ввод названия
    EDIT_WAIT_ARTIST   = auto()   # ввод исполнителя
    EDIT_WAIT_COVER    = auto()   # отправка обложки

# ════════════════════════════════════════════
#  СТАТИСТИКА
# ════════════════════════════════════════════
stats = {
    "total_users"   : set(),
    "total_requests": 0,
    "video_conv"    : 0,
    "voice_conv"    : 0,
    "audio_conv"    : 0,
    "tags_edited"   : 0,
    "started_at"    : datetime.now().strftime("%d.%m.%Y %H:%M"),
}
blocked_users: set = set()

# ════════════════════════════════════════════
#  УТИЛИТЫ
# ════════════════════════════════════════════
def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

def is_blocked(uid: int) -> bool:
    return uid in blocked_users

def track(uid: int):
    stats["total_users"].add(uid)
    stats["total_requests"] += 1

def clean_temp(uid: int):
    """Удаляем все temp-файлы пользователя."""
    for f in TEMP_DIR.glob(f"{uid}_*"):
        f.unlink(missing_ok=True)

async def remember_msg(ctx: ContextTypes.DEFAULT_TYPE, msg):
    if not msg:
        return
    bucket = ctx.user_data.setdefault("cleanup_msg_ids", [])
    bucket.append(msg.message_id)
    if len(bucket) > 30:
        del bucket[:-30]

async def cleanup_old_bot_msgs(update: Update, ctx: ContextTypes.DEFAULT_TYPE, keep_last: int = 2):
    ids = ctx.user_data.get("cleanup_msg_ids", [])
    if not ids:
        return
    delete_ids = ids[:-keep_last] if keep_last > 0 else ids[:]
    if not delete_ids:
        return
    chat_id = update.effective_chat.id
    remaining = ids[-keep_last:] if keep_last > 0 else []
    for mid in delete_ids:
        try:
            await ctx.bot.delete_message(chat_id=chat_id, message_id=mid)
        except Exception:
            pass
    ctx.user_data["cleanup_msg_ids"] = remaining

async def animate_status(msg, text: str, loops: int = 1):
    """Лёгкая анимация статуса через смену эмодзи в одном сообщении."""
    for _ in range(loops):
        for frame in ANIMATION_FRAMES:
            try:
                await msg.edit_text(f"{frame} {text}")
            except Exception:
                return
            await asyncio.sleep(ANIMATION_DELAY)

def support_url() -> str:
    return f"https://t.me/{SUPPORT_TAG.lstrip('@')}"

async def pulse_action(update: Update, ctx: ContextTypes.DEFAULT_TYPE, action):
    try:
        await ctx.bot.send_chat_action(chat_id=update.effective_chat.id, action=action)
    except Exception:
        pass

# ════════════════════════════════════════════
#  КЛАВИАТУРЫ
# ════════════════════════════════════════════
def kb_main(uid: int = 0) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton("🎬 Видео → Кружок"),   KeyboardButton("🎤 Голос → Аудио")],
        [KeyboardButton("🎵 Аудио → Голосовое"), KeyboardButton("✏️ Редактор тегов")],
        [KeyboardButton("ℹ️ Помощь"),            KeyboardButton("🆘 Поддержка")],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

def kb_edit_choose() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Название",    callback_data="edit_title"),
         InlineKeyboardButton("🎤 Исполнитель", callback_data="edit_artist")],
        [InlineKeyboardButton("🖼 Обложка",     callback_data="edit_cover")],
        [InlineKeyboardButton("🗑 Удалить ВСЕ теги",     callback_data="edit_strip_tags")],
        [InlineKeyboardButton("🗑 Удалить обложку",      callback_data="edit_strip_cover")],
        [InlineKeyboardButton("✅ Готово — скачать",      callback_data="edit_done")],
        [InlineKeyboardButton("❌ Отмена",               callback_data="edit_cancel")],
    ])

def kb_mp3_choice() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎙 Превратить в ГС", callback_data="mp3_to_voice")],
        [InlineKeyboardButton("✏️ Редактировать файл", callback_data="mp3_to_edit")],
        [InlineKeyboardButton("❌ Отмена", callback_data="mp3_cancel")],
    ])

def kb_video_choice() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎬 Превратить в кружок", callback_data="video_to_circle")],
        [InlineKeyboardButton("🎧 Извлечь аудио", callback_data="video_to_audio")],
        [InlineKeyboardButton("❌ Отмена", callback_data="video_cancel")],
    ])

def kb_video_note_choice() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎧 Извлечь аудио", callback_data="video_to_audio")],
        [InlineKeyboardButton("❌ Отмена", callback_data="video_cancel")],
    ])

def _media_file_ext(media) -> str:
    if hasattr(media, "file_name") and media.file_name and "." in media.file_name:
        return media.file_name.rsplit(".", 1)[-1].lower()
    return ""

def _is_mp3_media(media) -> bool:
    ext = _media_file_ext(media)
    mime = getattr(media, "mime_type", "") or ""
    return ext == "mp3" or mime in {"audio/mpeg", "audio/mp3"}

# ════════════════════════════════════════════
#  /start  /help
# ════════════════════════════════════════════
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_blocked(user.id): return
    track(user.id)

    await cleanup_old_bot_msgs(update, ctx)
    sent = await update.message.reply_text(
        f"بِسْمِ اللَّهِ الرَّحْمَنِ الرَّحِيمِ\n\n"
        f"Ас-саляму алейкум, <b>{user.first_name}</b>! 🕌\n\n"
        f"<b>AL IHSAN | Мусульманский аудио-редактор</b>\n"
        "Ваш помощник для быстрой обработки медиа в Telegram.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "🎬 <b>Видео → Кружок</b>\n"
        "   Преобразование видео в видеокружок Telegram\n\n"
        "🎤 <b>Голос → Аудио</b>\n"
        "   Конвертация голосового сообщения в MP3\n\n"
        "🎵 <b>Аудио → Голосовое</b>\n"
        "   Конвертация MP3/OGG в голосовое сообщение\n\n"
        "🎯 <b>MP3</b>\n"
        "   Выбор действия: в ГС или в редактор\n\n"
        "🎯 <b>Видео/кружок</b>\n"
        "   Выбор действия: в кружок или в аудио\n\n"
        "✏️ <b>Редактор тегов</b>\n"
        "   Название, исполнитель, обложка\n"
        f"🆘 <b>Поддержка</b> — {SUPPORT_TAG}\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "Выберите действие на кнопках ниже 👇",
        parse_mode="HTML",
        reply_markup=kb_main(user.id),
    )
    await remember_msg(ctx, sent)

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if is_blocked(update.effective_user.id): return
    await cleanup_old_bot_msgs(update, ctx)
    sent = await update.message.reply_text(
        "<b>AL IHSAN — Инструкция</b>\n\n"
        "🎬 <b>Видео → Кружок</b>\n"
        "   Отправь видео: выбор — кружок или аудио\n"
        "   Отправь кружок: выбор — извлечь аудио\n\n"
        "🎤 <b>Голос → Аудио</b>\n"
        "   Отправь голосовое сообщение\n\n"
        "🎵 <b>Аудио → Голосовое</b>\n"
        "   Отправь аудио файл (MP3, OGG, WAV, FLAC)\n\n"
        "✏️ <b>Редактор тегов</b>\n"
        "   Нажми кнопку → отправь MP3 → редактируй\n"
        "   Можно менять: название, исполнитель, обложка\n\n"
        "🎯 <b>Авто-выбор для MP3</b>\n"
        "   Если отправишь MP3 без команды,\n"
        "   появится выбор: в ГС или в редактор\n\n"
        f"🆘 <b>Поддержка:</b> {SUPPORT_TAG}\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "Лимиты: видео 50 МБ, аудио 20 МБ",
        parse_mode="HTML",
        reply_markup=kb_main(update.effective_user.id),
    )
    await remember_msg(ctx, sent)

async def cmd_support(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if is_blocked(update.effective_user.id): return
    await cleanup_old_bot_msgs(update, ctx)
    sent = await update.message.reply_text(
        f"🆘 Поддержка: <b>{SUPPORT_TAG}</b>\n"
        f"Написать напрямую: {support_url()}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Открыть поддержку", url=support_url())],
        ]),
    )
    await remember_msg(ctx, sent)

# ════════════════════════════════════════════
#  ОБЩИЙ HANDLER ТЕКСТОВЫХ КНОПОК МЕНЮ
# ════════════════════════════════════════════
async def menu_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_blocked(user.id): return
    text = update.message.text
    await cleanup_old_bot_msgs(update, ctx)

    if text == "ℹ️ Помощь":
        await cmd_help(update, ctx)
    elif text == "🆘 Поддержка":
        await cmd_support(update, ctx)
    elif text in ("🎬 Видео → Кружок", "🎤 Голос → Аудио", "🎵 Аудио → Голосовое"):
        sent = await update.message.reply_text(
            f"Хорошо! Просто отправь мне файл и я его обработаю. 👇",
        )
        await remember_msg(ctx, sent)
    elif text == "✏️ Редактор тегов":
        return await tag_editor_start(update, ctx)
    else:
        sent = await update.message.reply_text(
            "Отправь файл или выбери действие на кнопках 👇",
            reply_markup=kb_main(user.id),
        )
        await remember_msg(ctx, sent)

# ════════════════════════════════════════════
#  ВИДЕО/КРУЖОК → ВЫБОР ДЕЙСТВИЯ
# ════════════════════════════════════════════
async def _convert_video_to_circle(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    *,
    file_id: str,
    file_size: int,
    file_name: str = "video.mp4",
):
    msg = await update.effective_message.reply_text("⏳ Конвертирую видео в кружок…")
    await remember_msg(ctx, msg)
    await pulse_action(update, ctx, CHAT_ACTION_UPLOAD_VIDEO)
    await animate_status(msg, "Конвертирую видео в кружок…")
    if file_size > 50 * 1024 * 1024:
        await msg.edit_text("❌ Файл больше 50 МБ — не могу скачать.")
        return

    ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else "mp4"
    inp = TEMP_DIR / f"{update.effective_user.id}_vid_in.{ext}"
    out = TEMP_DIR / f"{update.effective_user.id}_vid_out.mp4"
    file = await ctx.bot.get_file(file_id)
    await file.download_to_drive(str(inp))

    ok = run_ffmpeg([
        "-i", str(inp),
        "-vf", "crop=min(iw\\,ih):min(iw\\,ih),scale=480:480",
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "28",
        "-c:a", "aac",
        "-b:a", "128k",
        "-t", "60",
        str(out),
    ])
    if not ok or not out.exists():
        await msg.edit_text("❌ Ошибка ffmpeg. Убедись, что ffmpeg установлен.")
        inp.unlink(missing_ok=True)
        return

    await pulse_action(update, ctx, CHAT_ACTION_UPLOAD_VIDEO)
    await msg.edit_text("📤 Отправляю кружок…")
    with open(out, "rb") as f:
        await update.effective_message.reply_video_note(f)
    stats["video_conv"] += 1
    await msg.delete()
    inp.unlink(missing_ok=True)
    out.unlink(missing_ok=True)

async def _convert_video_to_audio(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    *,
    file_id: str,
    file_size: int,
    file_name: str = "video.mp4",
):
    msg = await update.effective_message.reply_text("⏳ Извлекаю аудио из видео…")
    await remember_msg(ctx, msg)
    await pulse_action(update, ctx, CHAT_ACTION_UPLOAD_AUDIO)
    await animate_status(msg, "Извлекаю аудио из видео…")
    if file_size > 50 * 1024 * 1024:
        await msg.edit_text("❌ Файл больше 50 МБ — не могу скачать.")
        return

    ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else "mp4"
    inp = TEMP_DIR / f"{update.effective_user.id}_v2a_in.{ext}"
    out = TEMP_DIR / f"{update.effective_user.id}_v2a_out.mp3"
    file = await ctx.bot.get_file(file_id)
    await file.download_to_drive(str(inp))

    ok = run_ffmpeg([
        "-i", str(inp),
        "-vn",
        "-c:a", "libmp3lame",
        "-b:a", "192k",
        str(out),
    ])
    if not ok or not out.exists():
        await msg.edit_text("❌ Не удалось извлечь аудио из этого видео.")
        inp.unlink(missing_ok=True)
        out.unlink(missing_ok=True)
        return

    await pulse_action(update, ctx, CHAT_ACTION_UPLOAD_AUDIO)
    await msg.edit_text("📤 Отправляю аудио…")
    with open(out, "rb") as f:
        await update.effective_message.reply_audio(f, title="Аудио из видео", performer="AL IHSAN ☪️")
    await msg.delete()
    inp.unlink(missing_ok=True)
    out.unlink(missing_ok=True)

async def handle_video(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_blocked(user.id): return
    track(user.id)
    video = update.message.video
    ctx.user_data["pending_video"] = {
        "kind": "video",
        "file_id": video.file_id,
        "file_size": video.file_size,
        "file_name": video.file_name or "video.mp4",
    }
    await cleanup_old_bot_msgs(update, ctx)
    sent = await update.message.reply_text("🎯 Видео получено.\n\nВыбери действие:", reply_markup=kb_video_choice())
    await remember_msg(ctx, sent)

async def handle_video_note(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_blocked(user.id): return
    track(user.id)
    note = update.message.video_note
    ctx.user_data["pending_video"] = {
        "kind": "video_note",
        "file_id": note.file_id,
        "file_size": note.file_size,
        "file_name": "video_note.mp4",
    }
    await cleanup_old_bot_msgs(update, ctx)
    sent = await update.message.reply_text("🎯 Кружок получен.\n\nВыбери действие:", reply_markup=kb_video_note_choice())
    await remember_msg(ctx, sent)

async def video_choice_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    data = query.data

    if data == "video_cancel":
        ctx.user_data.pop("pending_video", None)
        await cleanup_old_bot_msgs(update, ctx)
        sent = await query.message.reply_text("❌ Отменено. Главное меню.", reply_markup=kb_main(uid))
        await remember_msg(ctx, sent)
        return

    pending = ctx.user_data.get("pending_video")
    if not pending:
        await cleanup_old_bot_msgs(update, ctx)
        sent = await query.message.reply_text("❌ Видео не найдено. Отправь заново.", reply_markup=kb_main(uid))
        await remember_msg(ctx, sent)
        return

    if data == "video_to_circle":
        if pending.get("kind") == "video_note":
            await cleanup_old_bot_msgs(update, ctx)
            sent = await query.message.reply_text("❌ Кружок уже в нужном формате. Выбери извлечение аудио.", reply_markup=kb_main(uid))
            await remember_msg(ctx, sent)
            return
        ctx.user_data.pop("pending_video", None)
        await cleanup_old_bot_msgs(update, ctx)
        await _convert_video_to_circle(
            update,
            ctx,
            file_id=pending["file_id"],
            file_size=pending["file_size"],
            file_name=pending.get("file_name", "video.mp4"),
        )
        return

    if data == "video_to_audio":
        ctx.user_data.pop("pending_video", None)
        await cleanup_old_bot_msgs(update, ctx)
        await _convert_video_to_audio(
            update,
            ctx,
            file_id=pending["file_id"],
            file_size=pending["file_size"],
            file_name=pending.get("file_name", "video.mp4"),
        )
        return

# ════════════════════════════════════════════
#  ГОЛОСОВОЕ → АУДИО
# ════════════════════════════════════════════
async def handle_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_blocked(user.id): return
    track(user.id)

    msg  = await update.message.reply_text("⏳ Конвертирую голосовое в MP3…")
    await pulse_action(update, ctx, CHAT_ACTION_UPLOAD_AUDIO)
    await animate_status(msg, "Конвертирую голосовое в MP3…")
    voice = update.message.voice
    file  = await ctx.bot.get_file(voice.file_id)
    inp   = TEMP_DIR / f"{user.id}_voice_in.ogg"
    out   = TEMP_DIR / f"{user.id}_voice_out.mp3"
    await file.download_to_drive(str(inp))

    try:
        seg = AudioSegment.from_ogg(str(inp))
        seg.export(str(out), format="mp3", bitrate="192k")
    except Exception as e:
        logger.error(e)
        await msg.edit_text("❌ Ошибка конвертации. Нужен ffmpeg.")
        inp.unlink(missing_ok=True)
        return

    await pulse_action(update, ctx, CHAT_ACTION_UPLOAD_AUDIO)
    await msg.edit_text("📤 Отправляю MP3…")
    with open(out, "rb") as f:
        await update.message.reply_audio(
            f, title="Аудио", performer="AL IHSAN ☪️",
            duration=len(seg) // 1000,
        )
    stats["voice_conv"] += 1
    await msg.delete()
    inp.unlink(missing_ok=True)
    out.unlink(missing_ok=True)

# ════════════════════════════════════════════
#  АУДИО → ГОЛОСОВОЕ
# ════════════════════════════════════════════
async def _convert_media_to_voice(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    *,
    file_id: str,
    file_size: int,
    file_name: str = "audio.mp3",
):
    msg = await update.effective_message.reply_text("⏳ Конвертирую аудио в голосовое…")
    await pulse_action(update, ctx, CHAT_ACTION_UPLOAD_AUDIO)
    await animate_status(msg, "Конвертирую аудио в голосовое…")
    if file_size > 20 * 1024 * 1024:
        await msg.edit_text("❌ Файл больше 20 МБ.")
        return

    file = await ctx.bot.get_file(file_id)
    ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else "mp3"
    inp = TEMP_DIR / f"{update.effective_user.id}_aud_in.{ext}"
    out = TEMP_DIR / f"{update.effective_user.id}_aud_out.ogg"
    await file.download_to_drive(str(inp))

    try:
        seg = AudioSegment.from_file(str(inp))
        seg.export(str(out), format="ogg", codec="libopus", parameters=["-b:a", "64k"])
    except Exception as e:
        logger.error(e)
        await msg.edit_text("❌ Ошибка конвертации. Нужен ffmpeg.")
        inp.unlink(missing_ok=True)
        return

    await pulse_action(update, ctx, CHAT_ACTION_UPLOAD_AUDIO)
    await msg.edit_text("📤 Отправляю голосовое…")
    with open(out, "rb") as f:
        await update.effective_message.reply_voice(f, duration=len(seg) // 1000)
    stats["audio_conv"] += 1
    await msg.delete()
    inp.unlink(missing_ok=True)
    out.unlink(missing_ok=True)

async def handle_audio_to_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Срабатывает только если НЕ в режиме редактора."""
    user = update.effective_user
    if is_blocked(user.id): return
    track(user.id)

    audio_m = update.message.audio or update.message.document
    if audio_m is None:
        return
    file_name = getattr(audio_m, "file_name", None) or "audio.mp3"

    if _is_mp3_media(audio_m):
        ctx.user_data["pending_mp3"] = {
            "file_id": audio_m.file_id,
            "file_size": audio_m.file_size,
            "file_name": file_name,
        }
        await cleanup_old_bot_msgs(update, ctx)
        sent = await update.message.reply_text(
            "🎯 MP3 получен.\n\nВыбери действие:",
            reply_markup=kb_mp3_choice(),
        )
        await remember_msg(ctx, sent)
        return

    await _convert_media_to_voice(
        update,
        ctx,
        file_id=audio_m.file_id,
        file_size=audio_m.file_size,
        file_name=file_name,
    )

async def mp3_choice_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    data = query.data

    if data == "mp3_cancel":
        ctx.user_data.pop("pending_mp3", None)
        await cleanup_old_bot_msgs(update, ctx)
        sent = await query.message.reply_text("❌ Отменено. Главное меню.", reply_markup=kb_main(uid))
        await remember_msg(ctx, sent)
        return

    pending = ctx.user_data.get("pending_mp3")
    if not pending:
        await cleanup_old_bot_msgs(update, ctx)
        sent = await query.message.reply_text("❌ MP3 не найден. Отправь файл заново.", reply_markup=kb_main(uid))
        await remember_msg(ctx, sent)
        return

    if data == "mp3_to_voice":
        ctx.user_data.pop("pending_mp3", None)
        await _convert_media_to_voice(
            update,
            ctx,
            file_id=pending["file_id"],
            file_size=pending["file_size"],
            file_name=pending.get("file_name", "audio.mp3"),
        )
        return

async def tag_editor_from_pending_mp3(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    pending = ctx.user_data.pop("pending_mp3", None)
    if not pending:
        await cleanup_old_bot_msgs(update, ctx)
        sent = await query.message.reply_text("❌ MP3 не найден. Отправь файл заново.", reply_markup=kb_main(uid))
        await remember_msg(ctx, sent)
        return ConversationHandler.END
    if not MUTAGEN_OK:
        await query.message.reply_text(
            "❌ Библиотека <code>mutagen</code> не установлена.\n"
            "Выполни: <code>pip install mutagen</code>",
            parse_mode="HTML",
            reply_markup=kb_main(uid),
        )
        return ConversationHandler.END
    sent = await query.message.reply_text("✏️ Открываю редактор MP3…", reply_markup=ReplyKeyboardRemove())
    await remember_msg(ctx, sent)
    ok = await _prepare_edit_file(
        update,
        ctx,
        file_id=pending["file_id"],
        file_size=pending["file_size"],
    )
    return State.EDIT_CHOOSE_ACTION if ok else ConversationHandler.END

# ════════════════════════════════════════════
#  ✏️  РЕДАКТОР ТЕГОВ (ConversationHandler)
# ════════════════════════════════════════════
async def _prepare_edit_file(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    *,
    file_id: str,
    file_size: int,
) -> bool:
    await cleanup_old_bot_msgs(update, ctx)
    if file_size > 20 * 1024 * 1024:
        sent = await update.effective_message.reply_text("❌ Файл больше 20 МБ.")
        await remember_msg(ctx, sent)
        return False

    msg = await update.effective_message.reply_text("⏳ Загружаю файл…")
    await remember_msg(ctx, msg)
    await pulse_action(update, ctx, CHAT_ACTION_UPLOAD_AUDIO)
    await animate_status(msg, "Загружаю файл…")

    file = await ctx.bot.get_file(file_id)
    path = TEMP_DIR / f"{update.effective_user.id}_edit.mp3"
    await file.download_to_drive(str(path))
    ctx.user_data["edit_path"] = str(path)
    ctx.user_data["edit_modified"] = False

    await msg.edit_text(_editor_view_text(str(path)), parse_mode="HTML", reply_markup=kb_edit_choose())
    return True

async def tag_editor_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if is_blocked(update.effective_user.id): return ConversationHandler.END
    if not MUTAGEN_OK:
        sent = await update.message.reply_text(
            "❌ Библиотека <code>mutagen</code> не установлена.\n"
            "Выполни: <code>pip install mutagen</code>",
            parse_mode="HTML",
        )
        await remember_msg(ctx, sent)
        return ConversationHandler.END

    await cleanup_old_bot_msgs(update, ctx)
    sent = await update.message.reply_text(
        "✏️ <b>Редактор тегов</b>\n\n"
        "Отправь мне <b>MP3 файл</b> (именно как файл, не как аудио),\n"
        "и я покажу текущие теги и меню редактирования.\n\n"
        "Или /cancel — отменить.",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove(),
    )
    await remember_msg(ctx, sent)
    return State.EDIT_WAIT_AUDIO


async def tag_editor_got_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_blocked(user.id): return ConversationHandler.END

    audio_m = update.message.audio or update.message.document
    if audio_m is None:
        sent = await update.message.reply_text("Отправь MP3 файл. /cancel — отмена.")
        await remember_msg(ctx, sent)
        return State.EDIT_WAIT_AUDIO

    ok = await _prepare_edit_file(
        update,
        ctx,
        file_id=audio_m.file_id,
        file_size=audio_m.file_size,
    )
    return State.EDIT_CHOOSE_ACTION if ok else ConversationHandler.END


def _read_tags(path: str) -> dict:
    info = {"title": "—", "artist": "—", "album": "—",
            "year": "—", "genre": "—", "has_cover": False}
    if not MUTAGEN_OK:
        return info
    try:
        tags = ID3(path)
        info["title"]  = str(tags.get("TIT2", "—"))
        info["artist"] = str(tags.get("TPE1", "—"))
        info["album"]  = str(tags.get("TALB", "—"))
        info["year"]   = str(tags.get("TDRC", "—"))
        info["genre"]  = str(tags.get("TCON", "—"))
        info["has_cover"] = bool(tags.getall("APIC"))
    except Exception:
        pass
    return info

def _editor_view_text(path: str) -> str:
    info = _read_tags(path)
    return (
        f"📋 <b>Текущие теги:</b>\n\n"
        f"📝 Название: <code>{info['title']}</code>\n"
        f"🎤 Исполнитель: <code>{info['artist']}</code>\n"
        f"🖼 Обложка: {'✅ есть' if info['has_cover'] else '❌ нет'}\n\n"
        "Выбери, что изменить."
    )


async def tag_editor_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data  = query.data
    uid   = query.from_user.id

    if data == "edit_cancel":
        clean_temp(uid)
        ctx.user_data.clear()
        sent = await query.message.reply_text("❌ Отменено. Главное меню.", reply_markup=kb_main(uid))
        await remember_msg(ctx, sent)
        await cleanup_old_bot_msgs(update, ctx)
        return ConversationHandler.END

    if data == "edit_done":
        path = ctx.user_data.get("edit_path")
        if not path or not Path(path).exists():
            sent = await query.message.reply_text("❌ Файл не найден.", reply_markup=kb_main(uid))
            await remember_msg(ctx, sent)
            return ConversationHandler.END
        msg = await query.message.reply_text("📤 Отправляю отредактированный файл…")
        await remember_msg(ctx, msg)
        await animate_status(msg, "Подготавливаю финальный файл…")
        with open(path, "rb") as f:
            info = _read_tags(path)
            await query.message.reply_audio(
                f,
                title=info["title"] if info["title"] != "—" else "Аудио",
                performer=info["artist"] if info["artist"] != "—" else "AL IHSAN",
            )
        stats["tags_edited"] += 1
        await msg.delete()
        Path(path).unlink(missing_ok=True)
        ctx.user_data.clear()
        sent = await query.message.reply_text("✅ Готово!", reply_markup=kb_main(uid))
        await remember_msg(ctx, sent)
        await cleanup_old_bot_msgs(update, ctx)
        return ConversationHandler.END

    if data == "edit_strip_tags":
        path = ctx.user_data.get("edit_path")
        _strip_all_tags(path)
        sent = await query.message.reply_text(
            _editor_view_text(path),
            parse_mode="HTML",
            reply_markup=kb_edit_choose(),
        )
        await remember_msg(ctx, sent)
        return State.EDIT_CHOOSE_ACTION

    if data == "edit_strip_cover":
        path = ctx.user_data.get("edit_path")
        _strip_cover(path)
        sent = await query.message.reply_text(
            _editor_view_text(path),
            parse_mode="HTML",
            reply_markup=kb_edit_choose(),
        )
        await remember_msg(ctx, sent)
        return State.EDIT_CHOOSE_ACTION

    # Переход к вводу конкретного тега
    prompts = {
        "edit_title":  ("📝 Введи новое <b>название</b> аудио:", State.EDIT_WAIT_TITLE),
        "edit_artist": ("🎤 Введи имя <b>исполнителя</b>:",      State.EDIT_WAIT_ARTIST),
        "edit_cover":  ("🖼 Отправь <b>изображение</b> (фото или файл) — оно станет обложкой.", State.EDIT_WAIT_COVER),
    }
    if data in prompts:
        text, next_state = prompts[data]
        sent = await query.message.reply_text(text, parse_mode="HTML")
        await remember_msg(ctx, sent)
        return next_state


async def tag_got_title(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    _set_tag(ctx.user_data.get("edit_path"), "TIT2", update.message.text)
    sent = await update.message.reply_text(
        _editor_view_text(ctx.user_data.get("edit_path")),
        parse_mode="HTML",
        reply_markup=kb_edit_choose(),
    )
    await remember_msg(ctx, sent)
    return State.EDIT_CHOOSE_ACTION

async def tag_got_artist(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    _set_tag(ctx.user_data.get("edit_path"), "TPE1", update.message.text)
    sent = await update.message.reply_text(
        _editor_view_text(ctx.user_data.get("edit_path")),
        parse_mode="HTML",
        reply_markup=kb_edit_choose(),
    )
    await remember_msg(ctx, sent)
    return State.EDIT_CHOOSE_ACTION

async def tag_got_cover(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    photo = update.message.photo[-1] if update.message.photo else None
    doc = update.message.document
    image_doc = bool(doc)
    if not photo and not image_doc:
        sent = await update.message.reply_text("Отправь изображение (фото или файл).")
        await remember_msg(ctx, sent)
        return State.EDIT_WAIT_COVER

    file_id = photo.file_id if photo else doc.file_id
    src_name = (doc.file_name if doc and doc.file_name else "cover.jpg")
    ext = src_name.rsplit(".", 1)[-1].lower() if "." in src_name else "jpg"
    if ext not in {"jpg", "jpeg", "png", "webp"}:
        ext = "jpg"
    file  = await ctx.bot.get_file(file_id)
    uid   = update.effective_user.id
    cover_path = TEMP_DIR / f"{uid}_cover.{ext}"
    await file.download_to_drive(str(cover_path))

    ok = _set_cover(ctx.user_data.get("edit_path"), str(cover_path))
    cover_path.unlink(missing_ok=True)
    if not ok:
        sent = await update.message.reply_text(
            "❌ Не удалось встроить обложку. Отправь другое изображение (JPEG/PNG/WEBP).",
            reply_markup=kb_edit_choose(),
        )
        await remember_msg(ctx, sent)
        return State.EDIT_CHOOSE_ACTION

    sent = await update.message.reply_text(
        _editor_view_text(ctx.user_data.get("edit_path")),
        parse_mode="HTML",
        reply_markup=kb_edit_choose(),
    )
    await remember_msg(ctx, sent)
    return State.EDIT_CHOOSE_ACTION

# Низкоуровневые функции для тегов
def _set_tag(path: str, tag_name: str, value: str):
    if not path or not MUTAGEN_OK: return
    try:
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            tags = ID3Tags()
        frame_map = {
            "TIT2": TIT2, "TPE1": TPE1,
            "TALB": TALB, "TDRC": TDRC, "TCON": TCON,
        }
        if tag_name in frame_map:
            tags[tag_name] = frame_map[tag_name](encoding=3, text=value)
        tags.save(path, v2_version=3)
    except Exception as e:
        logger.error(f"Tag set error: {e}")

def _guess_image_mime(cover_path: str, data: bytes) -> str:
    mime, _ = mimetypes.guess_type(cover_path)
    if mime in {"image/jpeg", "image/png", "image/webp"}:
        return mime
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return ""

def _set_cover(path: str, cover_path: str) -> bool:
    if not path or not MUTAGEN_OK:
        return False
    try:
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            tags = ID3Tags()
        with open(cover_path, "rb") as img:
            data = img.read()
        mime = _guess_image_mime(cover_path, data)
        if not mime:
            # fallback: пробуем перекодировать нестандартный формат в JPEG
            converted = Path(cover_path).with_name(f"{Path(cover_path).stem}_conv.jpg")
            ok = run_ffmpeg([
                "-i", str(cover_path),
                "-frames:v", "1",
                str(converted),
            ])
            if ok and converted.exists():
                with open(converted, "rb") as cimg:
                    data = cimg.read()
                mime = "image/jpeg"
                converted.unlink(missing_ok=True)
            else:
                return False
        tags.delall("APIC")
        tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
        tags.save(path, v2_version=3)
        # Проверяем, что обложка реально записалась.
        verify = ID3(path)
        if not verify.getall("APIC"):
            return False
        return True
    except Exception as e:
        logger.error(f"Cover set error: {e}")
        return False

def _strip_all_tags(path: str):
    if not path or not MUTAGEN_OK: return
    try:
        tags = ID3(path)
        tags.delete()
        tags.save(path)
    except Exception as e:
        logger.error(f"Strip tags error: {e}")

def _strip_cover(path: str):
    if not path or not MUTAGEN_OK: return
    try:
        tags = ID3(path)
        tags.delall("APIC")
        tags.save(path)
    except Exception as e:
        logger.error(f"Strip cover error: {e}")

async def tag_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    clean_temp(uid)
    ctx.user_data.clear()
    sent = await update.message.reply_text("❌ Отменено. Главное меню.", reply_markup=kb_main(uid))
    await remember_msg(ctx, sent)
    await cleanup_old_bot_msgs(update, ctx)
    return ConversationHandler.END

# ════════════════════════════════════════════
#  ДОКУМЕНТЫ (автоопределение)
# ════════════════════════════════════════════
async def handle_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if is_blocked(update.effective_user.id): return
    doc = update.message.document
    ext = (doc.file_name.rsplit(".", 1)[-1].lower() if doc.file_name and "." in doc.file_name else "")
    audio_ext = {"mp3", "wav", "ogg", "flac", "m4a", "aac", "opus"}
    if (doc.mime_type and doc.mime_type.startswith("audio")) or ext in audio_ext:
        await handle_audio_to_voice(update, ctx)
    elif doc.mime_type and doc.mime_type.startswith("video"):
        await update.message.reply_text(
            "💡 Для конвертации видео отправь его как видео, а не как файл."
        )
    else:
        await update.message.reply_text(
            "Не распознан тип файла.\n"
            "Принимаю: видео, аудио (MP3/OGG/WAV), голосовые."
        )

# ════════════════════════════════════════════
#  АДМИН-ПАНЕЛЬ
# ════════════════════════════════════════════
async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("⛔ Нет доступа.")
        return
    await update.message.reply_text(
        f"🔐 <b>Admin access</b>\n"
        f"ID: <code>{user.id}</code>\n\n"
        "Доступ к /admin активен только для 8206124108.",
        parse_mode="HTML",
    )

# ════════════════════════════════════════════
#  ЗАПУСК
# ════════════════════════════════════════════
def build_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()

    # ── ConversationHandler: Редактор тегов ──
    tag_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^✏️ Редактор тегов$"), tag_editor_start),
            CommandHandler("edit", tag_editor_start),
            CallbackQueryHandler(tag_editor_from_pending_mp3, pattern="^mp3_to_edit$"),
        ],
        states={
            State.EDIT_WAIT_AUDIO: [
                MessageHandler(filters.AUDIO | filters.Document.ALL, tag_editor_got_file),
            ],
            State.EDIT_CHOOSE_ACTION: [
                CallbackQueryHandler(tag_editor_callback, pattern="^edit_"),
            ],
            State.EDIT_WAIT_TITLE:  [MessageHandler(filters.TEXT & ~filters.COMMAND, tag_got_title)],
            State.EDIT_WAIT_ARTIST: [MessageHandler(filters.TEXT & ~filters.COMMAND, tag_got_artist)],
            State.EDIT_WAIT_COVER:  [MessageHandler(filters.PHOTO | filters.Document.ALL, tag_got_cover)],
        },
        fallbacks=[CommandHandler("cancel", tag_cancel)],
        allow_reentry=True,
    )

    # Регистрируем ConversationHandler ПЕРВЫМ
    app.add_handler(tag_conv)

    # Команды
    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("help",      cmd_help))
    app.add_handler(CommandHandler("support",   cmd_support))
    app.add_handler(CommandHandler("admin",     cmd_admin))

    # Callback-и
    app.add_handler(CallbackQueryHandler(mp3_choice_callback, pattern="^mp3_(to_voice|cancel)$"))
    app.add_handler(CallbackQueryHandler(video_choice_callback, pattern="^video_(to_circle|to_audio|cancel)$"))

    # Медиа (вне Conversation)
    app.add_handler(MessageHandler(filters.VIDEO,        handle_video))
    app.add_handler(MessageHandler(filters.VIDEO_NOTE,   handle_video_note))
    app.add_handler(MessageHandler(filters.VOICE,        handle_voice))
    app.add_handler(MessageHandler(filters.AUDIO,        handle_audio_to_voice))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    # Текст / кнопки меню
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu_handler))
    return app

BOT_APP: Application | None = None
BOT_LOOP: asyncio.AbstractEventLoop | None = None
BOT_THREAD: threading.Thread | None = None

def build_flask_app() -> Flask:
    flask_app = Flask(__name__)

    @flask_app.route("/")
    def health():
        return "OK", 200

    @flask_app.post(f"/{WEBHOOK_PATH}")
    def telegram_webhook():
        if WEBHOOK_SECRET:
            incoming_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
            if incoming_secret != WEBHOOK_SECRET:
                return Response("forbidden", status=403)

        if BOT_APP is None or BOT_LOOP is None:
            return Response("bot not ready", status=503)

        payload = request.get_json(silent=True)
        if not payload:
            return Response("bad request", status=400)

        update = Update.de_json(payload, BOT_APP.bot)
        if update is None:
            return Response("ok", status=200)

        try:
            future = asyncio.run_coroutine_threadsafe(BOT_APP.update_queue.put(update), BOT_LOOP)
            future.result(timeout=10)
        except FutureTimeoutError:
            logger.error("Timed out while enqueuing incoming update.")
            return Response("timeout", status=504)
        except Exception as e:
            logger.error("Webhook update enqueue error: %s", e)
            return Response("error", status=500)

        return Response("ok", status=200)

    return flask_app

def start_bot_runtime(app: Application):
    global BOT_APP, BOT_LOOP, BOT_THREAD

    BOT_APP = app
    BOT_LOOP = asyncio.new_event_loop()

    def _run_loop():
        asyncio.set_event_loop(BOT_LOOP)
        BOT_LOOP.run_forever()

    BOT_THREAD = threading.Thread(target=_run_loop, name="ptb-loop", daemon=True)
    BOT_THREAD.start()

    async def _startup():
        await BOT_APP.initialize()
        await BOT_APP.start()
        await BOT_APP.bot.set_webhook(
            url=WEBHOOK_URL,
            secret_token=WEBHOOK_SECRET,
            drop_pending_updates=True,
        )

    future = asyncio.run_coroutine_threadsafe(_startup(), BOT_LOOP)
    future.result(timeout=60)

def stop_bot_runtime():
    global BOT_APP, BOT_LOOP, BOT_THREAD

    app = BOT_APP
    loop = BOT_LOOP
    thread = BOT_THREAD

    if app is not None and loop is not None:
        async def _shutdown():
            try:
                await app.bot.delete_webhook()
            except Exception:
                pass
            await app.stop()
            await app.shutdown()

        try:
            future = asyncio.run_coroutine_threadsafe(_shutdown(), loop)
            future.result(timeout=30)
        except Exception as e:
            logger.error("Shutdown error: %s", e)

        try:
            loop.call_soon_threadsafe(loop.stop)
        except Exception:
            pass

    if thread and thread.is_alive():
        thread.join(timeout=5)

    BOT_APP = None
    BOT_LOOP = None
    BOT_THREAD = None

def main():
    if BOT_TOKEN == "ВСТАВЬ_СЮДА_СВОЙ_TOKEN":
        print("❌ Вставь токен бота в переменную BOT_TOKEN!")
        print("   Получи токен у @BotFather в Telegram")
        return

    if not WEBHOOK_URL:
        print("❌ Для webhook-режима нужен WEBHOOK_URL или RENDER_EXTERNAL_URL.")
        print("   Пример WEBHOOK_URL: https://your-service.onrender.com/webhook/<token>")
        return

    print("╔══════════════════════════════════════════╗")
    print("║  AL IHSAN | Мусульманский аудио-редактор ║")
    print("╚══════════════════════════════════════════╝")
    print(f"   Admins: {ADMIN_IDS}")
    print(f"   Mutagen: {'✅ OK' if MUTAGEN_OK else '❌ не установлен (pip install mutagen)'}")
    print(f"   FFmpeg: {'✅ OK' if shutil.which('ffmpeg') or IMAGEIO_FFMPEG_OK else '❌ не найден'}")
    print("   Run mode: webhook")
    print(f"   Webhook URL: {WEBHOOK_URL}")
    print(f"   Listen: {WEBHOOK_HOST}:{WEBHOOK_PORT}/{WEBHOOK_PATH}\n")

    bot_app = build_app()
    flask_app = build_flask_app()
    start_bot_runtime(bot_app)

    print("✅ Бот запущен в webhook-режиме! Ctrl+C — остановка.\n")
    try:
        flask_app.run(host=WEBHOOK_HOST, port=WEBHOOK_PORT, use_reloader=False)
    finally:
        stop_bot_runtime()


if __name__ == "__main__":
    main()
