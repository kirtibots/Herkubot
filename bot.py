import os
import re
import json
import sqlite3
import secrets
import logging
from urllib.parse import quote, urlparse
from html import escape

import aiohttp

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError, BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)

log = logging.getLogger("heroku-controller")


# =========================================================
# ENVIRONMENT
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

try:
    OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)
except ValueError:
    OWNER_ID = 0

SUPPORT_URL = os.getenv(
    "SUPPORT_URL",
    "https://t.me/"
).strip()

OWNER_URL = os.getenv(
    "OWNER_URL",
    "https://t.me/"
).strip()

DB_PATH = os.getenv(
    "DB_PATH",
    "heroku_controller.db"
).strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required")


# =========================================================
# HEROKU API
# =========================================================

HEROKU_API = "https://api.heroku.com"

HEROKU_HEADERS = {
    "Accept": "application/vnd.heroku+json; version=3",
    "Content-Type": "application/json",
}


# =========================================================
# DATABASE
# =========================================================

db = sqlite3.connect(
    DB_PATH,
    check_same_thread=False
)

db.execute(
    """
    CREATE TABLE IF NOT EXISTS accounts (
        user_id INTEGER PRIMARY KEY,
        token TEXT NOT NULL,
        heroku_user_id TEXT,
        email TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """
)

db.commit()


# =========================================================
# TEMPORARY USER STATE
# =========================================================

pending = {}


# =========================================================
# DATABASE FUNCTIONS
# =========================================================

def get_account(user_id: int):
    return db.execute(
        """
        SELECT token, heroku_user_id, email
        FROM accounts
        WHERE user_id=?
        """,
        (user_id,),
    ).fetchone()


def save_account(
    user_id: int,
    token: str,
    heroku_user_id: str = "",
    email: str = "",
):
    db.execute(
        """
        INSERT INTO accounts
        (
            user_id,
            token,
            heroku_user_id,
            email
        )
        VALUES (?, ?, ?, ?)

        ON CONFLICT(user_id)
        DO UPDATE SET
            token=excluded.token,
            heroku_user_id=excluded.heroku_user_id,
            email=excluded.email
        """,
        (
            user_id,
            token,
            heroku_user_id,
            email,
        ),
    )

    db.commit()


def delete_account(user_id: int):
    db.execute(
        "DELETE FROM accounts WHERE user_id=?",
        (user_id,),
    )
    db.commit()


def mask_token(token: str) -> str:
    if not token:
        return "••••••••"

    if len(token) < 10:
        return "••••••••"

    return token[:5] + "…" + token[-4:]


# =========================================================
# HEROKU REQUEST
# =========================================================

async def heroku_request(
    user_id: int,
    method: str,
    path: str,
    **kwargs
):
    account = get_account(user_id)

    if not account:
        raise RuntimeError("NO_ACCOUNT")

    token = account[0]

    headers = dict(HEROKU_HEADERS)
    headers["Authorization"] = f"Bearer {token}"

    timeout = aiohttp.ClientTimeout(total=40)

    async with aiohttp.ClientSession(
        timeout=timeout
    ) as session:

        async with session.request(
            method,
            HEROKU_API + path,
            headers=headers,
            **kwargs
        ) as response:

            text = await response.text()

            if response.status >= 400:
                raise RuntimeError(
                    f"HEROKU_{response.status}:{text[:1000]}"
                )

            if not text:
                return {}

            try:
                return json.loads(text)
            except Exception:
                return {
                    "raw": text
                }


# =========================================================
# SAFE TEXT
# =========================================================

def safe(value) -> str:
    return escape(str(value or ""))


# =========================================================
# MAIN KEYBOARD
# =========================================================

def main_keyboard():

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📱 ᴀᴘᴘs ᴍᴀɴᴀɢᴇʀ",
                callback_data="apps"
            ),
            InlineKeyboardButton(
                "🚀 ǫᴜɪᴄᴋ ᴅᴇᴘʟᴏʏ",
                callback_data="deploy"
            ),
        ],

        [
            InlineKeyboardButton(
                "🔐 𝟸ғᴀ ᴀᴜᴛʜ",
                callback_data="totp"
            ),
            InlineKeyboardButton(
                "📊 ᴅʏɴᴏ ǫᴜᴏᴛᴀ",
                callback_data="quota"
            ),
        ],

        [
            InlineKeyboardButton(
                "⚙️ sᴇᴛᴛɪɴɢs",
                callback_data="settings"
            ),
            InlineKeyboardButton(
                "❓ ʜᴇʟᴘ & ᴄᴏᴍᴍᴀɴᴅs",
                callback_data="help"
            ),
        ],

        [
            InlineKeyboardButton(
                "👑 ᴏᴡɴᴇʀ",
                url=OWNER_URL
            ),
            InlineKeyboardButton(
                "🆘 sᴜᴘᴘᴏʀᴛ",
                url=SUPPORT_URL
            ),
        ],
    ])


# =========================================================
# SEND OR EDIT
# =========================================================

async def send_or_edit(
    target,
    text,
    reply_markup=None
):
    try:

        if hasattr(target, "edit_message_text"):

            await target.edit_message_text(
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
            )

        else:

            await target.message.reply_text(
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
            )

    except BadRequest as e:

        if "Message is not modified" not in str(e):

            try:

                await target.message.reply_text(
                    text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup,
                )

            except Exception:
                pass

    except Exception as e:

        log.warning(
            "send_or_edit failed: %s",
            str(e)[:200]
        )


# =========================================================
# START
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    account = get_account(user.id)

    if account:

        account_status = (
            "🟢 <b>ᴄᴏɴɴᴇᴄᴛᴇᴅ</b>\n"
            f"╰─ ʜᴇʀᴏᴋᴜ ɪᴅ : "
            f"<code>{safe(account[1] or 'Unknown')}</code>\n"
            f"╰─ ᴛᴏᴋᴇɴ : "
            f"<code>{mask_token(account[0])}</code>"
        )

    else:

        account_status = (
            "🔴 <b>ɴᴏ ᴀᴄᴄᴏᴜɴᴛ ʟɪɴᴋᴇᴅ</b>\n"
            "╰─ ᴜsᴇ <code>/link</code> ᴛᴏ ᴄᴏɴɴᴇᴄᴛ"
        )

    name = safe(user.first_name or "User")

    text = (
        "╭━━━━━━━━━━━━━━━━━━━━╮\n"
        "      ⚡ <b>ʜᴇʀᴏᴋᴜ ᴄʟᴏᴜᴅ</b>\n"
        "        <b>ᴄᴏɴᴛʀᴏʟʟᴇʀ</b> 🚀\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"

        "✨ <b>ᴡᴇʟᴄᴏᴍᴇ ᴛᴏ ʏᴏᴜʀ ᴄʟᴏᴜᴅ ᴘᴀɴᴇʟ</b>\n"
        "Manage your Heroku applications directly "
        "from Telegram using a fast interactive dashboard.\n\n"

        "┌─ 👤 <b>ᴜsᴇʀ ɪɴғᴏ</b>\n"
        f"├─ ɪᴅ : <code>{user.id}</code>\n"
        f"└─ ɴᴀᴍᴇ : <b>{name}</b>\n\n"

        "┌─ 🔐 <b>ᴀᴄᴄᴏᴜɴᴛ sᴛᴀᴛᴜs</b>\n"
        f"└─ {account_status}\n\n"

        "╭─ 🚀 <b>ᴀᴠᴀɪʟᴀʙʟᴇ ғᴇᴀᴛᴜʀᴇs</b>\n"
        "├ 📱 ᴀᴘᴘ ᴍᴀɴᴀɢᴇᴍᴇɴᴛ\n"
        "├ 🚀 ɢɪᴛʜᴜʙ ǫᴜɪᴄᴋ ᴅᴇᴘʟᴏʏ\n"
        "├ ⚡ ᴅʏɴᴏ ᴍᴀɴᴀɢᴇᴍᴇɴᴛ\n"
        "├ 🔑 ᴄᴏɴғɪɢ ᴠᴀʀs\n"
        "├ 📜 ᴀᴘᴘ ʟᴏɢs\n"
        "└ ⚙️ ᴀᴄᴄᴏᴜɴᴛ sᴇᴛᴛɪɴɢs\n"
        "╰────────────────────╯\n\n"

        "💡 <i>Choose an option below to continue.</i>"
    )

    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard()
    )


# =========================================================
# HELP COMMAND
# =========================================================

async def help_cmd(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    text = (
        "╭━━━━━━━━━━━━━━━━━━━━╮\n"
        "       ❓ <b>ʜᴇʟᴘ & ᴄᴏᴍᴍᴀɴᴅs</b>\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"

        "📌 <b>ᴄᴏʀᴇ ᴄᴏᴍᴍᴀɴᴅs</b>\n"
        "├ /start — ᴏᴘᴇɴ ᴅᴀsʜʙᴏᴀʀᴅ\n"
        "├ /help — ᴏᴘᴇɴ ʜᴇʟᴘ ᴄᴇɴᴛᴇʀ\n"
        "├ /link — ʟɪɴᴋ ʜᴇʀᴏᴋᴜ ᴀᴄᴄᴏᴜɴᴛ\n"
        "├ /unlink — ʀᴇᴍᴏᴠᴇ ᴀᴄᴄᴏᴜɴᴛ\n"
        "├ /apps — ʟɪsᴛ ʜᴇʀᴏᴋᴜ ᴀᴘᴘs\n"
        "├ /deploy — ǫᴜɪᴄᴋ ɢɪᴛʜᴜʙ ᴅᴇᴘʟᴏʏ\n"
        "├ /settings — ᴀᴄᴄᴏᴜɴᴛ sᴇᴛᴛɪɴɢs\n"
        "└ /cancel — ᴄᴀɴᴄᴇʟ ᴀᴄᴛɪᴠᴇ ᴘʀᴏᴄᴇss\n\n"

        "╭─ ⚡ <b>ғᴇᴀᴛᴜʀᴇs</b>\n"
        "├ 📱 ᴀᴘᴘs ᴍᴀɴᴀɢᴇʀ\n"
        "├ 🚀 ǫᴜɪᴄᴋ ᴅᴇᴘʟᴏʏ\n"
        "├ ⚙️ ᴄᴏɴғɪɢ ᴠᴀʀs\n"
        "├ 📜 ʟᴏɢs\n"
        "├ 🔄 ᴅʏɴᴏ ʀᴇsᴛᴀʀᴛ\n"
        "├ ⚙️ ᴅʏɴᴏ sᴄᴀʟɪɴɢ\n"
        "└ 📊 ᴅʏɴᴏ ᴜsᴀɢᴇ\n"
        "╰────────────────────╯\n\n"

        "🔐 <b>sᴇᴄᴜʀɪᴛʏ</b>\n"
        "Never send your Heroku password, recovery "
        "codes or login OTP to this bot."
    )

    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard()
    )


# =========================================================
# LINK
# =========================================================

async def link_cmd(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user_id = update.effective_user.id

    pending[user_id] = {
        "action": "link"
    }

    await update.message.reply_text(
        "╭━━━━━━━━━━━━━━━━━━━━╮\n"
        "       🔐 <b>ʟɪɴᴋ ʜᴇʀᴏᴋᴜ</b>\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
        "Send your <b>Heroku API token</b> in the next message.\n\n"
        "⚠️ Never send your Heroku password,\n"
        "recovery codes or login OTP.\n\n"
        "The token will be validated before saving.\n\n"
        "❌ Send /cancel to stop.",
        parse_mode=ParseMode.HTML,
    )


# =========================================================
# UNLINK
# =========================================================

async def unlink_cmd(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user_id = update.effective_user.id

    delete_account(user_id)

    pending.pop(user_id, None)

    await update.message.reply_text(
        "✅ <b>Heroku account unlinked.</b>\n\n"
        "Your stored token has been removed.",
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard()
    )


# =========================================================
# SETTINGS
# =========================================================

async def settings_cmd(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await show_settings(
        update.effective_user.id,
        update
    )


async def show_settings(
    user_id,
    target
):

    account = get_account(user_id)

    if not account:

        body = (
            "╭━━━━━━━━━━━━━━━━━━━━╮\n"
            "       ⚙️ <b>sᴇᴛᴛɪɴɢs</b>\n"
            "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
            "🔴 <b>Active Account:</b>\n"
            "No Heroku account linked."
        )

    else:

        body = (
            "╭━━━━━━━━━━━━━━━━━━━━╮\n"
            "       ⚙️ <b>sᴇᴛᴛɪɴɢs</b>\n"
            "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
            "🟢 <b>ᴀᴄᴄᴏᴜɴᴛ ʟɪɴᴋᴇᴅ</b>\n\n"
            f"👤 Heroku ID:\n"
            f"<code>{safe(account[1] or 'Unknown')}</code>\n\n"
            f"🔑 Token:\n"
            f"<code>{mask_token(account[0])}</code>\n\n"
            f"📧 Email:\n"
            f"<code>{safe(account[2] or 'Unknown')}</code>"
        )

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🔐 ʟɪɴᴋ / ʀᴇᴘʟᴀᴄᴇ",
                callback_data="link"
            )
        ],
        [
            InlineKeyboardButton(
                "🗑️ ᴜɴʟɪɴᴋ",
                callback_data="unlink"
            )
        ],
        [
            InlineKeyboardButton(
                "⬅️ ʙᴀᴄᴋ",
                callback_data="home"
            )
        ],
    ])

    await send_or_edit(
        target,
        body,
        kb
    )


# =========================================================
# APPS COMMAND
# =========================================================

async def apps_cmd(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await show_apps(
        update.effective_user.id,
        update
    )


async def show_apps(
    user_id,
    target
):

    try:

        apps = await heroku_request(
            user_id,
            "GET",
            "/apps"
        )

    except RuntimeError as e:

        if str(e) == "NO_ACCOUNT":

            text = (
                "🔑 <b>No Heroku account linked.</b>\n\n"
                "Use /link first."
            )

        else:

            text = (
                "❌ <b>Unable to load apps.</b>\n\n"
                "Check your Heroku token/account."
            )

        await send_or_edit(
            target,
            text,
            main_keyboard()
        )

        return

    except Exception:

        await send_or_edit(
            target,
            "❌ Failed to load Heroku apps.",
            main_keyboard()
        )

        return

    if not apps:

        await send_or_edit(
            target,
            "📱 <b>ᴀᴘᴘs ᴍᴀɴᴀɢᴇʀ</b>\n\n"
            "No apps found.",
            main_keyboard()
        )

        return

    rows = []

    for app in apps[:50]:

        name = app.get(
            "name",
            "unknown"
        )

        rows.append([
            InlineKeyboardButton(
                f"📦 {name}",
                callback_data=f"app:{name}"
            )
        ])

    rows.append([
        InlineKeyboardButton(
            "⬅️ ʙᴀᴄᴋ",
            callback_data="home"
        )
    ])

    await send_or_edit(
        target,
        "📱 <b>ᴀᴘᴘs ᴍᴀɴᴀɢᴇʀ</b>\n\n"
        f"Found <b>{len(apps)}</b> app(s).\n\n"
        "Select an application:",
        InlineKeyboardMarkup(rows)
    )


# =========================================================
# SHOW APP
# =========================================================

async def show_app(
    user_id,
    app_name,
    target
):

    try:

        app = await heroku_request(
            user_id,
            "GET",
            f"/apps/{quote(app_name, safe='')}"
        )

        formation = await heroku_request(
            user_id,
            "GET",
            f"/apps/{quote(app_name, safe='')}/formation"
        )

    except Exception as e:

        log.warning(
            "show app failed: %s",
            str(e)[:200]
        )

        await send_or_edit(
            target,
            "❌ Failed to load app information.",
            main_keyboard()
        )

        return

    region = (
        app.get("region") or {}
    ).get(
        "name",
        "?"
    )

    stack = (
        app.get("build_stack") or {}
    ).get(
        "name",
        "?"
    )

    lines = [
        "╭━━━━━━━━━━━━━━━━━━━━╮",
        f"       📦 <b>{safe(app.get('name'))}</b>",
        "╰━━━━━━━━━━━━━━━━━━━━╯",
        "",
        f"🌎 Region: <code>{safe(region)}</code>",
        f"🧱 Stack: <code>{safe(stack)}</code>",
        f"🕒 Updated: <code>{safe(app.get('updated_at', '?'))}</code>",
        "",
        "⚙️ <b>ғᴏʀᴍᴀᴛɪᴏɴ</b>",
    ]

    for item in formation:

        lines.append(
            f"• <b>{safe(item.get('type'))}</b>: "
            f"{item.get('quantity', 0)} × "
            f"{safe(item.get('size', '?'))}"
        )

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🔄 ʀᴇsᴛᴀʀᴛ",
                callback_data=f"restart:{app_name}"
            ),
            InlineKeyboardButton(
                "⚙️ sᴄᴀʟᴇ",
                callback_data=f"scale:{app_name}"
            ),
        ],
        [
            InlineKeyboardButton(
                "🔑 ᴄᴏɴғɪɢ ᴠᴀʀs",
                callback_data=f"config:{app_name}"
            ),
            InlineKeyboardButton(
                "📜 ʟᴏɢs",
                callback_data=f"logs:{app_name}"
            ),
        ],
        [
            InlineKeyboardButton(
                "🗑️ ᴅᴇʟᴇᴛᴇ ᴀᴘᴘ",
                callback_data=f"delete:{app_name}"
            )
        ],
        [
            InlineKeyboardButton(
                "⬅️ ᴀᴘᴘs",
                callback_data="apps"
            )
        ],
    ])

    await send_or_edit(
        target,
        "\n".join(lines),
        kb
    )


# =========================================================
# QUOTA
# =========================================================

async def show_quota(
    user_id,
    target
):

    try:

        apps = await heroku_request(
            user_id,
            "GET",
            "/apps"
        )

        total = 0

        details = []

        for app in apps[:40]:

            name = app.get(
                "name",
                "?"
            )

            formation = await heroku_request(
                user_id,
                "GET",
                f"/apps/{quote(name, safe='')}/formation"
            )

            count = sum(
                int(item.get("quantity", 0))
                for item in formation
            )

            total += count

            if count:

                details.append(
                    f"• <code>{safe(name)}</code>: "
                    f"{count} dyno(s)"
                )

        text = (
            "╭━━━━━━━━━━━━━━━━━━━━╮\n"
            "       📊 <b>ᴅʏɴᴏ ᴜsᴀɢᴇ</b>\n"
            "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
            f"📱 Apps: <b>{len(apps)}</b>\n"
            f"⚡ Configured processes: <b>{total}</b>\n\n"
            +
            (
                "\n".join(details)
                if details
                else "No dynos configured."
            )
            +
            "\n\n"
            "ℹ️ This shows current app formations, "
            "not your billing-plan quota."
        )

    except Exception:

        text = (
            "❌ Unable to read dyno information."
        )

    await send_or_edit(
        target,
        text,
        main_keyboard()
    )


# =========================================================
# QUICK DEPLOY
# =========================================================

async def deploy_cmd(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    pending[
        update.effective_user.id
    ] = {
        "action": "deploy"
    }

    await update.message.reply_text(
        "╭━━━━━━━━━━━━━━━━━━━━╮\n"
        "       🚀 <b>ǫᴜɪᴄᴋ ᴅᴇᴘʟᴏʏ</b>\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
        "Send a <b>public GitHub repository URL</b>.\n\n"
        "Example:\n"
        "<code>https://github.com/owner/repo</code>\n\n"
        "❌ /cancel to stop.",
        parse_mode=ParseMode.HTML
    )


async def quick_deploy(
    user_id,
    repo_url,
    target
):

    parsed = urlparse(
        repo_url.strip()
    )

    if (
        parsed.scheme not in
        ("http", "https")
        or
        parsed.netloc.lower()
        not in
        ("github.com", "www.github.com")
    ):

        await send_or_edit(
            target,
            "❌ Only public GitHub repository URLs are supported.",
            main_keyboard()
        )

        return

    parts = [
        p
        for p in parsed.path.split("/")
        if p
    ]

    if len(parts) < 2:

        await send_or_edit(
            target,
            "❌ Invalid GitHub repository URL.",
            main_keyboard()
        )

        return

    owner = parts[0]

    repo = parts[1].removesuffix(
        ".git"
    )

    if not re.fullmatch(
        r"[A-Za-z0-9_.-]+",
        owner
    ):

        await send_or_edit(
            target,
            "❌ Invalid GitHub owner.",
            main_keyboard()
        )

        return

    if not re.fullmatch(
        r"[A-Za-z0-9_.-]+",
        repo
    ):

        await send_or_edit(
            target,
            "❌ Invalid GitHub repository.",
            main_keyboard()
        )

        return

    tar_url = (
        f"https://github.com/"
        f"{owner}/{repo}/tarball/HEAD"
    )

    app_name = re.sub(
        r"[^a-z0-9-]",
        "-",
        repo.lower()
    )[:25].strip("-")

    if not app_name:
        app_name = "app"

    app_name = (
        f"{app_name}-"
        f"{secrets.token_hex(2)}"
    )

    try:

        data = {
            "source_blob": {
                "url": tar_url
            },
            "app": {
                "name": app_name
            }
        }

        result = await heroku_request(
            user_id,
            "POST",
            "/app-setups",
            json=data
        )

        setup_id = result.get(
            "id",
            "unknown"
        )

        await send_or_edit(
            target,
            "╭━━━━━━━━━━━━━━━━━━━━╮\n"
            "       🚀 <b>ᴅᴇᴘʟᴏʏ sᴛᴀʀᴛᴇᴅ</b>\n"
            "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
            f"📦 Repository:\n"
            f"<code>{safe(owner)}/{safe(repo)}</code>\n\n"
            f"🌐 App:\n"
            f"<code>{safe(app_name)}</code>\n\n"
            f"🆔 Setup ID:\n"
            f"<code>{safe(setup_id)}</code>\n\n"
            "⏳ Heroku is processing the deployment.",
            main_keyboard()
        )

    except Exception as e:

        log.warning(
            "deploy failed: %s",
            str(e)[:300]
        )

        await send_or_edit(
            target,
            "❌ <b>Quick deploy failed.</b>\n\n"
            "Check that the repository is public and "
            "contains a valid Heroku application.",
            main_keyboard()
        )


# =========================================================
# SCALE
# =========================================================

async def scale_app(
    user_id,
    app_name,
    value,
    target
):

    value = value.strip()

    match = re.fullmatch(
        r"([a-zA-Z0-9_-]+)\s*=\s*(\d+)",
        value
    )

    if not match:

        await send_or_edit(
            target,
            "❌ Invalid scale format.\n\n"
            "Example:\n"
            "<code>web=1</code>\n"
            "<code>worker=2</code>",
            main_keyboard()
        )

        return

    process_type = match.group(1)
    quantity = int(match.group(2))

    if quantity < 0 or quantity > 100:

        await send_or_edit(
            target,
            "❌ Quantity must be between 0 and 100.",
            main_keyboard()
        )

        return

    try:

        payload = [
            {
                "type": process_type,
                "quantity": quantity
            }
        ]

        await heroku_request(
            user_id,
            "PATCH",
            f"/apps/{quote(app_name, safe='')}/formation",
            json=payload
        )

        await send_or_edit(
            target,
            "✅ <b>Dyno scaling updated.</b>\n\n"
            f"📦 App: <code>{safe(app_name)}</code>\n"
            f"⚙️ Process: <code>{safe(process_type)}</code>\n"
            f"🔢 Quantity: <b>{quantity}</b>",
            main_keyboard()
        )

    except Exception as e:

        log.warning(
            "scale failed: %s",
            str(e)[:200]
        )

        await send_or_edit(
            target,
            "❌ Failed to scale dyno.",
            main_keyboard()
        )


# =========================================================
# CONFIG VARS
# =========================================================

async def show_config(
    user_id,
    app_name,
    target
):

    try:

        config = await heroku_request(
            user_id,
            "GET",
            f"/apps/{quote(app_name, safe='')}/config-vars"
        )

    except Exception:

        await send_or_edit(
            target,
            "❌ Failed to load Config Vars.",
            main_keyboard()
        )

        return

    lines = [
        "╭━━━━━━━━━━━━━━━━━━━━╮",
        "       🔑 <b>ᴄᴏɴғɪɢ ᴠᴀʀs</b>",
        "╰━━━━━━━━━━━━━━━━━━━━╯",
        "",
        f"📦 App: <code>{safe(app_name)}</code>",
        "",
    ]

    if not config:

        lines.append(
            "No Config Vars found."
        )

    else:

        for key in sorted(config.keys()):

            lines.append(
                f"🔹 <code>{safe(key)}</code> = "
                "<code>••••••••</code>"
            )

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "➕ ᴀᴅᴅ / ᴜᴘᴅᴀᴛᴇ",
                callback_data=f"setvar:{app_name}"
            )
        ],
        [
            InlineKeyboardButton(
                "⬅️ ᴀᴘᴘ",
                callback_data=f"app:{app_name}"
            )
        ],
    ])

    await send_or_edit(
        target,
        "\n".join(lines),
        kb
    )


# =========================================================
# SET CONFIG VAR
# =========================================================

async def set_config_var(
    user_id,
    app_name,
    key_value,
    target
):

    if "=" not in key_value:

        await send_or_edit(
            target,
            "❌ Invalid format.\n\n"
            "Use:\n"
            "<code>KEY=value</code>",
            main_keyboard()
        )

        return

    key, value = key_value.split(
        "=",
        1
    )

    key = key.strip()

    if not re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_]*",
        key
    ):

        await send_or_edit(
            target,
            "❌ Invalid Config Var name.",
            main_keyboard()
        )

        return

    try:

        payload = {
            key: value
        }

        await heroku_request(
            user_id,
            "PATCH",
            f"/apps/{quote(app_name, safe='')}/config-vars",
            json=payload
        )

        await send_or_edit(
            target,
            "✅ <b>Config Var updated.</b>\n\n"
            f"📦 App: <code>{safe(app_name)}</code>\n"
            f"🔑 Key: <code>{safe(key)}</code>\n"
            "🔒 Value: <code>••••••••</code>",
            main_keyboard()
        )

    except Exception:

        await send_or_edit(
            target,
            "❌ Failed to update Config Var.",
            main_keyboard()
        )


# =========================================================
# LOGS
# =========================================================

async def show_logs(
    user_id,
    app_name,
    target
):

    try:

        # Create a short-lived log session.
        session = await heroku_request(
            user_id,
            "POST",
            f"/apps/{quote(app_name, safe='')}/log-sessions",
            json={
                "lines": 80,
                "tail": True
            }
        )

        log_url = session.get(
            "logplex_url"
        )

        if not log_url:

            raise RuntimeError(
                "NO_LOG_URL"
            )

        timeout = aiohttp.ClientTimeout(
            total=30
        )

        async with aiohttp.ClientSession(
            timeout=timeout
        ) as http:

            async with http.get(
                log_url
            ) as response:

                content = await response.text()

        if not content.strip():

            content = "No recent logs available."

        content = content[-6000:]

        text = (
            "╭━━━━━━━━━━━━━━━━━━━━╮\n"
            "       📜 <b>ᴀᴘᴘ ʟᴏɢs</b>\n"
            "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
            f"📦 <b>{safe(app_name)}</b>\n\n"
            f"<pre>{safe(content)}</pre>"
        )

        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🔄 Refresh",
                    callback_data=f"logs:{app_name}"
                )
            ],
            [
                InlineKeyboardButton(
                    "⬅️ App",
                    callback_data=f"app:{app_name}"
                )
            ],
        ])

        await send_or_edit(
            target,
            text,
            kb
        )

    except Exception as e:

        log.warning(
            "logs failed: %s",
            str(e)[:200]
        )

        await send_or_edit(
            target,
            "❌ Unable to retrieve recent logs.\n\n"
            "The app may not currently have a log session available.",
            main_keyboard()
        )


# =========================================================
# HELP CENTER
# =========================================================

async def show_help_center(
    target
):

    text = (
        "╭━━━━━━━━━━━━━━━━━━━━╮\n"
        "     ❓ <b>ʜᴇʟᴘ & ᴄᴏᴍᴍᴀɴᴅ ᴄᴇɴᴛᴇʀ</b>\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"

        "💡 <i>Choose a category below to explore "
        "features and usage instructions.</i>"
    )

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📱 ᴀᴘᴘs",
                callback_data="helpcat:apps"
            ),
            InlineKeyboardButton(
                "⚡ ᴅʏɴᴏs",
                callback_data="helpcat:dynos"
            ),
            InlineKeyboardButton(
                "📋 ʟᴏɢs",
                callback_data="helpcat:logs"
            ),
        ],

        [
            InlineKeyboardButton(
                "⚙️ ᴠᴀʀs",
                callback_data="helpcat:vars"
            ),
            InlineKeyboardButton(
                "🌐 ᴅᴏᴍᴀɪɴs",
                callback_data="helpcat:domains"
            ),
            InlineKeyboardButton(
                "🔑 ᴀᴄᴄᴏᴜɴᴛ",
                callback_data="helpcat:account"
            ),
        ],

        [
            InlineKeyboardButton(
                "🔐 𝟸ғᴀ",
                callback_data="helpcat:2fa"
            ),
            InlineKeyboardButton(
                "👑 ᴀᴅᴍɪɴ",
                callback_data="helpcat:admin"
            ),
        ],

        [
            InlineKeyboardButton(
                "📖 ᴜsᴀɢᴇ",
                callback_data="helpcat:interactive"
            ),
        ],

        [
            InlineKeyboardButton(
                "⬅️ ʙᴀᴄᴋ ᴛᴏ ᴅᴀsʜʙᴏᴀʀᴅ",
                callback_data="home"
            )
        ],
    ])

    await send_or_edit(
        target,
        text,
        kb
    )


# =========================================================
# HELP CATEGORY
# =========================================================

async def show_help_category(
    target,
    category
):

    pages = {

        "apps": (
            "📱 <b>ᴀᴘᴘs</b>\n\n"
            "/apps — List your Heroku apps.\n\n"
            "Tap any app to open its management panel.\n\n"
            "⚠️ Delete App is irreversible."
        ),

        "dynos": (
            "⚡ <b>ᴅʏɴᴏs</b>\n\n"
            "📊 Dyno Quota displays configured formations.\n\n"
            "🔄 Restart removes current dynos so Heroku "
            "can recreate them.\n\n"
            "⚙️ Scale accepts:\n"
            "<code>web=1</code>\n"
            "<code>worker=2</code>"
        ),

        "logs": (
            "📋 <b>ʟᴏɢs</b>\n\n"
            "Open an app and select 📜 Logs.\n\n"
            "The bot requests a limited recent log session."
        ),

        "vars": (
            "⚙️ <b>ᴄᴏɴғɪɢ ᴠᴀʀs</b>\n\n"
            "Open an app → Config Vars.\n\n"
            "Variable names are displayed while values "
            "are intentionally masked."
        ),

        "domains": (
            "🌐 <b>ᴅᴏᴍᴀɪɴs</b>\n\n"
            "Domain management is not enabled in this build."
        ),

        "account": (
            "🔑 <b>ᴀᴄᴄᴏᴜɴᴛ</b>\n\n"
            "/link — Link a Heroku API token.\n"
            "/unlink — Remove your stored token.\n"
            "/settings — View account status.\n\n"
            "⚠️ Never send your password, recovery codes "
            "or login OTP."
        ),

        "2fa": (
            "🔐 <b>𝟸ғᴀ ᴛᴏᴏʟs</b>\n\n"
            "This bot does not collect Heroku passwords, "
            "recovery codes or login OTPs.\n\n"
            "Use your own authenticator application."
        ),

        "admin": (
            "👑 <b>ᴀᴅᴍɪɴ sᴜɪᴛᴇ</b>\n\n"
            "Owner and Support buttons are available "
            "from the main dashboard.\n\n"
            "Destructive owner commands are not exposed "
            "to normal users."
        ),

        "interactive": (
            "📖 <b>ɪɴᴛᴇʀᴀᴄᴛɪᴠᴇ ᴜsᴀɢᴇ</b>\n\n"
            "1. Open /start.\n"
            "2. Link your Heroku API token.\n"
            "3. Open Apps Manager.\n"
            "4. Select an app.\n"
            "5. Use the management buttons.\n\n"
            "Use /cancel whenever an input flow is active."
        ),
    }

    text = pages.get(
        category,
        "❌ Help category not found."
    )

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "⬅️ ʜᴇʟᴘ ᴄᴇɴᴛᴇʀ",
                callback_data="help"
            )
        ],
        [
            InlineKeyboardButton(
                "🏠 ᴅᴀsʜʙᴏᴀʀᴅ",
                callback_data="home"
            )
        ],
    ])

    await send_or_edit(
        target,
        text,
        kb
    )


# =========================================================
# CANCEL
# =========================================================

async def cancel_cmd(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user_id = update.effective_user.id

    pending.pop(
        user_id,
        None
    )

    await update.message.reply_text(
        "❌ <b>Operation cancelled.</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard()
    )


# =========================================================
# CALLBACK HANDLER
# =========================================================

async def callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()

    user_id = query.from_user.id

    data = query.data or ""

    # -----------------------------------------------------
    # HOME
    # -----------------------------------------------------

    if data == "home":

        account = get_account(
            user_id
        )

        if account:

            status = (
                "🟢 ᴀᴄᴄᴏᴜɴᴛ ʟɪɴᴋᴇᴅ"
            )

        else:

            status = (
                "🔴 ɴᴏ ᴀᴄᴄᴏᴜɴᴛ ʟɪɴᴋᴇᴅ"
            )

        text = (
            "╭━━━━━━━━━━━━━━━━━━━━╮\n"
            "      ⚡ <b>ʜᴇʀᴏᴋᴜ ᴄʟᴏᴜᴅ</b>\n"
            "        <b>ᴄᴏɴᴛʀᴏʟʟᴇʀ</b> 🚀\n"
            "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
            f"👤 User ID: <code>{user_id}</code>\n"
            f"🔐 Status: <b>{status}</b>\n\n"
            "Choose an option below."
        )

        await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=main_keyboard()
        )

        return

    # -----------------------------------------------------
    # HELP
    # -----------------------------------------------------

    if data == "help":

        await show_help_center(
            query
        )

        return

    # -----------------------------------------------------
    # HELP CATEGORY
    # -----------------------------------------------------

    if data.startswith(
        "helpcat:"
    ):

        category = data.split(
            ":",
            1
        )[1]

        await show_help_category(
            query,
            category
        )

        return

    # -----------------------------------------------------
    # APPS
    # -----------------------------------------------------

    if data == "apps":

        await show_apps(
            user_id,
            query
        )

        return

    # -----------------------------------------------------
    # DEPLOY
    # -----------------------------------------------------

    if data == "deploy":

        pending[user_id] = {
            "action": "deploy"
        }

        await query.edit_message_text(
            "🚀 <b>ǫᴜɪᴄᴋ ᴅᴇᴘʟᴏʏ</b>\n\n"
            "Send a public GitHub repository URL.\n\n"
            "Example:\n"
            "<code>https://github.com/owner/repo</code>\n\n"
            "Use /cancel to stop.",
            parse_mode=ParseMode.HTML
        )

        return

    # -----------------------------------------------------
    # LINK
    # -----------------------------------------------------

    if data == "link":

        pending[user_id] = {
            "action": "link"
        }

        await query.edit_message_text(
            "🔐 <b>ʟɪɴᴋ ʜᴇʀᴏᴋᴜ ᴀᴄᴄᴏᴜɴᴛ</b>\n\n"
            "Send your Heroku API token.\n\n"
            "⚠️ Never send your password, recovery "
            "codes or login OTP.\n\n"
            "Use /cancel to stop.",
            parse_mode=ParseMode.HTML
        )

        return

    # -----------------------------------------------------
    # UNLINK
    # -----------------------------------------------------

    if data == "unlink":

        delete_account(
            user_id
        )

        pending.pop(
            user_id,
            None
        )

        await query.edit_message_text(
            "✅ <b>Account unlinked.</b>\n\n"
            "Your stored Heroku token has been removed.",
            parse_mode=ParseMode.HTML,
            reply_markup=main_keyboard()
        )

        return

    # -----------------------------------------------------
    # SETTINGS
    # -----------------------------------------------------

    if data == "settings":

        await show_settings(
            user_id,
            query
        )

        return

    # -----------------------------------------------------
    # QUOTA
    # -----------------------------------------------------

    if data == "quota":

        await show_quota(
            user_id,
            query
        )

        return

    # -----------------------------------------------------
    # 2FA
    # -----------------------------------------------------

    if data == "totp":

        await query.edit_message_text(
            "╭━━━━━━━━━━━━━━━━━━━━╮\n"
            "       🔐 <b>𝟸ғᴀ ᴀᴜᴛʜᴇɴᴛɪᴄᴀᴛᴏʀ</b>\n"
            "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
            "This build intentionally does not collect "
            "or store Heroku passwords, recovery codes "
            "or login OTPs.\n\n"
            "For account security, use your own "
            "authenticator application.\n\n"
            "🔒 Keep your authentication secrets private.",
            parse_mode=ParseMode.HTML,
            reply_markup=main_keyboard()
        )

        return

    # -----------------------------------------------------
    # APP
    # -----------------------------------------------------

    if data.startswith(
        "app:"
    ):

        app_name = data.split(
            ":",
            1
        )[1]

        await show_app(
            user_id,
            app_name,
            query
        )

        return

    # -----------------------------------------------------
    # RESTART
    # -----------------------------------------------------

    if data.startswith(
        "restart:"
    ):

        app_name = data.split(
            ":",
            1
        )[1]

        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "✅ Confirm Restart",
                    callback_data=f"confirmrestart:{app_name}"
                )
            ],
            [
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data=f"app:{app_name}"
                )
            ],
        ])

        await query.edit_message_text(
            "⚠️ <b>Confirm Dyno Restart</b>\n\n"
            f"App: <code>{safe(app_name)}</code>\n\n"
            "All currently running dynos will be stopped "
            "and Heroku can recreate the scaled processes.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb
        )

        return

    # -----------------------------------------------------
    # CONFIRM RESTART
    # -----------------------------------------------------

    if data.startswith(
        "confirmrestart:"
    ):

        app_name = data.split(
            ":",
            1
        )[1]

        try:

            dynos = await heroku_request(
                user_id,
                "GET",
                f"/apps/{quote(app_name, safe='')}/dynos"
            )

            restarted = 0

            for dyno in dynos:

                dyno_id = (
                    dyno.get("id")
                    or
                    dyno.get("name")
                )

                if dyno_id:

                    await heroku_request(
                        user_id,
                        "DELETE",
                        f"/apps/{quote(app_name, safe='')}"
                        f"/dynos/{quote(str(dyno_id), safe='')}"
                    )

                    restarted += 1

            await query.edit_message_text(
                "🔄 <b>Restart request sent.</b>\n\n"
                f"📦 App: <code>{safe(app_name)}</code>\n"
                f"⚡ Dynos stopped: <b>{restarted}</b>\n\n"
                "Heroku can replace scaled dynos automatically.",
                parse_mode=ParseMode.HTML,
                reply_markup=main_keyboard()
            )

        except Exception as e:

            log.warning(
                "restart failed: %s",
                str(e)[:200]
            )

            await query.edit_message_text(
                "❌ <b>Restart failed.</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=main_keyboard()
            )

        return

    # -----------------------------------------------------
    # SCALE
    # -----------------------------------------------------

    if data.startswith(
        "scale:"
    ):

        app_name = data.split(
            ":",
            1
        )[1]

        pending[user_id] = {
            "action": "scale",
            "app": app_name
        }

        await query.edit_message_text(
            f"⚙️ <b>sᴄᴀʟᴇ ᴅʏɴᴏ</b>\n\n"
            f"App: <code>{safe(app_name)}</code>\n\n"
            "Send the process and quantity.\n\n"
            "Examples:\n"
            "<code>web=1</code>\n"
            "<code>worker=2</code>\n\n"
            "Use /cancel to stop.",
            parse_mode=ParseMode.HTML
        )

        return

    # -----------------------------------------------------
    # CONFIG
    # -----------------------------------------------------

    if data.startswith(
        "config:"
    ):

        app_name = data.split(
            ":",
            1
        )[1]

        await show_config(
            user_id,
            app_name,
            query
        )

        return

    # -----------------------------------------------------
    # SET CONFIG VAR
    # -----------------------------------------------------

    if data.startswith(
        "setvar:"
    ):

        app_name = data.split(
            ":",
            1
        )[1]

        pending[user_id] = {
            "action": "setvar",
            "app": app_name
        }

        await query.edit_message_text(
            f"🔑 <b>ᴄᴏɴғɪɢ ᴠᴀʀ</b>\n\n"
            f"App: <code>{safe(app_name)}</code>\n\n"
            "Send:\n"
            "<code>KEY=value</code>\n\n"
            "Example:\n"
            "<code>API_URL=https://example.com</code>\n\n"
            "⚠️ Never post secrets in public chats.\n"
            "Use /cancel to stop.",
            parse_mode=ParseMode.HTML
        )

        return

    # -----------------------------------------------------
    # LOGS
    # -----------------------------------------------------

    if data.startswith(
        "logs:"
    ):

        app_name = data.split(
            ":",
            1
        )[1]

        await show_logs(
            user_id,
            app_name,
            query
        )

        return

    # -----------------------------------------------------
    # DELETE
    # -----------------------------------------------------

    if data.startswith(
        "delete:"
    ):

        app_name = data.split(
            ":",
            1
        )[1]

        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🗑️ YES, DELETE",
                    callback_data=f"confirmdelete:{app_name}"
                )
            ],
            [
                InlineKeyboardButton(
                    "❌ CANCEL",
                    callback_data=f"app:{app_name}"
                )
            ],
        ])

        await query.edit_message_text(
            "🚨 <b>DELETE APPLICATION?</b>\n\n"
            f"App: <code>{safe(app_name)}</code>\n\n"
            "This action is <b>irreversible</b>.\n"
            "The Heroku application and its configuration "
            "will be removed.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb
        )

        return

    # -----------------------------------------------------
    # CONFIRM DELETE
    # -----------------------------------------------------

    if data.startswith(
        "confirmdelete:"
    ):

        app_name = data.split(
            ":",
            1
        )[1]

        try:

            await heroku_request(
                user_id,
                "DELETE",
                f"/apps/{quote(app_name, safe='')}"
            )

            await query.edit_message_text(
                "🗑️ <b>Application deleted.</b>\n\n"
                f"<code>{safe(app_name)}</code> "
                "has been removed.",
                parse_mode=ParseMode.HTML,
                reply_markup=main_keyboard()
            )

        except Exception as e:

            log.warning(
                "delete failed: %s",
                str(e)[:200]
            )

            await query.edit_message_text(
                "❌ <b>Failed to delete application.</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=main_keyboard()
            )

        return

    # -----------------------------------------------------
    # UNKNOWN
    # -----------------------------------------------------

    await query.edit_message_text(
        "❌ Unknown action.",
        reply_markup=main_keyboard()
    )


# =========================================================
# TEXT MESSAGE HANDLER
# =========================================================

async def text_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user_id = update.effective_user.id

    state = pending.get(
        user_id
    )

    if not state:

        return

    text = (
        update.message.text or ""
    ).strip()

    action = state.get(
        "action"
    )

    # =====================================================
    # LINK TOKEN
    # =====================================================

    if action == "link":

        token = text.strip()

        if len(token) < 10:

            await update.message.reply_text(
                "❌ That does not look like a valid Heroku API token.\n\n"
                "Send the token again or use /cancel."
            )

            return

        # Try validation before saving.
        try:

            account_info = await heroku_request_with_token(
                token,
                "GET",
                "/account"
            )

            heroku_id = account_info.get(
                "id",
                ""
            )

            email = account_info.get(
                "email",
                ""
            )

            save_account(
                user_id,
                token,
                heroku_id,
                email
            )

            pending.pop(
                user_id,
                None
            )

            # Best effort delete the token message.
            try:

                await update.message.delete()

            except Exception:

                pass

            await update.effective_chat.send_message(
                "✅ <b>Heroku account linked successfully.</b>\n\n"
                f"👤 Account: <code>{safe(email or 'Unknown')}</code>\n"
                f"🆔 ID: <code>{safe(heroku_id or 'Unknown')}</code>\n\n"
                "🔐 Token saved securely in the bot database "
                "and masked in the interface.",
                parse_mode=ParseMode.HTML,
                reply_markup=main_keyboard()
            )

        except Exception as e:

            log.warning(
                "token validation failed: %s",
                str(e)[:200]
            )

            await update.message.reply_text(
                "❌ <b>Token validation failed.</b>\n\n"
                "Make sure you supplied a valid Heroku API token.\n\n"
                "Use /cancel to stop.",
                parse_mode=ParseMode.HTML
            )

        return

    # =====================================================
    # DEPLOY
    # =====================================================

    if action == "deploy":

        pending.pop(
            user_id,
            None
        )

        await quick_deploy(
            user_id,
            text,
            update
        )

        return

    # =====================================================
    # SCALE
    # =====================================================

    if action == "scale":

        app_name = state.get(
            "app"
        )

        pending.pop(
            user_id,
            None
        )

        await scale_app(
            user_id,
            app_name,
            text,
            update
        )

        return

    # =====================================================
    # CONFIG VAR
    # =====================================================

    if action == "setvar":

        app_name = state.get(
            "app"
        )

        pending.pop(
            user_id,
            None
        )

        await set_config_var(
            user_id,
            app_name,
            text,
            update
        )

        return


# =========================================================
# HEROKU REQUEST USING RAW TOKEN
# =========================================================

async def heroku_request_with_token(
    token: str,
    method: str,
    path: str,
    **kwargs
):

    headers = dict(
        HEROKU_HEADERS
    )

    headers["Authorization"] = (
        f"Bearer {token}"
    )

    timeout = aiohttp.ClientTimeout(
        total=30
    )

    async with aiohttp.ClientSession(
        timeout=timeout
    ) as session:

        async with session.request(
            method,
            HEROKU_API + path,
            headers=headers,
            **kwargs
        ) as response:

            text = await response.text()

            if response.status >= 400:

                raise RuntimeError(
                    f"HEROKU_{response.status}:{text[:500]}"
                )

            if not text:

                return {}

            return json.loads(text)


# =========================================================
# ERROR HANDLER
# =========================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE
):

    error = context.error

    log.error(
        "Unhandled exception: %s",
        error
    )

    try:

        if isinstance(
            update,
            Update
        ):

            if update.effective_message:

                await update.effective_message.reply_text(
                    "❌ Something went wrong.\n"
                    "Please try again."
                )

    except Exception:

        pass


# =========================================================
# APPLICATION
# =========================================================

def build_application():

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # Commands
    application.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    application.add_handler(
        CommandHandler(
            "help",
            help_cmd
        )
    )

    application.add_handler(
        CommandHandler(
            "link",
            link_cmd
        )
    )

    application.add_handler(
        CommandHandler(
            "unlink",
            unlink_cmd
        )
    )

    application.add_handler(
        CommandHandler(
            "apps",
            apps_cmd
        )
    )

    application.add_handler(
        CommandHandler(
            "deploy",
            deploy_cmd
        )
    )

    application.add_handler(
        CommandHandler(
            "settings",
            settings_cmd
        )
    )

    application.add_handler(
        CommandHandler(
            "cancel",
            cancel_cmd
        )
    )

    # Callback buttons
    application.add_handler(
        CallbackQueryHandler(
            callback
        )
    )

    # Text input flows
    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            text_handler
        )
    )

    # Error handler
    application.add_error_handler(
        error_handler
    )

    return application


# =========================================================
# MAIN
# =========================================================

def main():

    application = build_application()

    log.info(
        "Heroku Controller starting..."
    )

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
