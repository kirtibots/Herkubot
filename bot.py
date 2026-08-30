import os
import re
import json
import hmac
import base64
import hashlib
import struct
import time
import sqlite3
import logging
from urllib.parse import quote, urlparse, parse_qs, unquote

import aiohttp

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import TelegramError, BadRequest
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters,
)

# Optional QR scanner. Install opencv-python-headless from requirements.txt.
try:
    import cv2
except Exception:
    cv2 = None

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("heroku-controller")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_URL = os.getenv("OWNER_URL", "https://t.me/").strip()
SUPPORT_URL = os.getenv("SUPPORT_URL", "https://t.me/").strip()
DB_PATH = os.getenv("DB_PATH", "heroku_controller.db").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required")

HEROKU_API = "https://api.heroku.com"
HEROKU_HEADERS = {
    "Accept": "application/vnd.heroku+json; version=3",
    "Content-Type": "application/json",
}

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute("""
CREATE TABLE IF NOT EXISTS accounts (
    user_id INTEGER PRIMARY KEY,
    token TEXT NOT NULL,
    heroku_user_id TEXT,
    email TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
)
""")
db.execute("""
CREATE TABLE IF NOT EXISTS totp_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    secret TEXT NOT NULL,
    issuer TEXT DEFAULT '',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
)
""")
db.commit()

pending = {}
selected_app = {}

def safe(v):
    from html import escape
    return escape(str(v or ""))

def mask_token(token):
    if not token or len(token) < 10:
        return "••••••••"
    return token[:5] + "…" + token[-4:]

def get_account(uid):
    return db.execute(
        "SELECT token, heroku_user_id, email FROM accounts WHERE user_id=?",
        (uid,)
    ).fetchone()

def save_account(uid, token, hid="", email=""):
    db.execute("""
        INSERT INTO accounts(user_id,token,heroku_user_id,email)
        VALUES(?,?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET
          token=excluded.token,
          heroku_user_id=excluded.heroku_user_id,
          email=excluded.email
    """, (uid, token, hid, email))
    db.commit()

def delete_account(uid):
    db.execute("DELETE FROM accounts WHERE user_id=?", (uid,))
    db.commit()

def save_totp(uid, name, secret, issuer=""):
    db.execute(
        "INSERT INTO totp_keys(user_id,name,secret,issuer) VALUES(?,?,?,?)",
        (uid, name[:60], secret, issuer[:100])
    )
    db.commit()

def get_totps(uid):
    return db.execute(
        "SELECT id,name,secret,issuer FROM totp_keys WHERE user_id=? ORDER BY id DESC",
        (uid,)
    ).fetchall()

def get_totp(uid, key_id):
    return db.execute(
        "SELECT id,name,secret,issuer FROM totp_keys WHERE id=? AND user_id=?",
        (key_id, uid)
    ).fetchone()

def delete_totp(uid, key_id):
    db.execute(
        "DELETE FROM totp_keys WHERE id=? AND user_id=?",
        (key_id, uid)
    )
    db.commit()

async def heroku_request(uid, method, path, **kwargs):
    account = get_account(uid)
    if not account:
        raise RuntimeError("NO_ACCOUNT")

    headers = dict(HEROKU_HEADERS)
    headers["Authorization"] = f"Bearer {account[0]}"
    timeout = aiohttp.ClientTimeout(total=45)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.request(
            method, HEROKU_API + path, headers=headers, **kwargs
        ) as r:
            body = await r.text()
            if r.status >= 400:
                raise RuntimeError(f"HEROKU_{r.status}:{body[:1200]}")
            if not body:
                return {}
            try:
                return json.loads(body)
            except Exception:
                return {"raw": body}

def normalize_secret(secret):
    return re.sub(r"[\s-]", "", secret or "").upper()

def generate_totp(secret, digits=6, interval=30):
    secret = normalize_secret(secret)
    if not re.fullmatch(r"[A-Z2-7]{8,}", secret):
        raise ValueError("Invalid Base32 secret")
    key = base64.b32decode(secret + "=" * ((-len(secret)) % 8), casefold=True)
    counter = int(time.time() // interval)
    digest = hmac.new(
        key, struct.pack(">Q", counter), hashlib.sha1
    ).digest()
    offset = digest[-1] & 0x0F
    binary = (
        ((digest[offset] & 0x7F) << 24)
        | ((digest[offset+1] & 0xFF) << 16)
        | ((digest[offset+2] & 0xFF) << 8)
        | (digest[offset+3] & 0xFF)
    )
    otp = str(binary % (10 ** digits)).zfill(digits)
    remain = interval - (int(time.time()) % interval)
    return otp, remain

def parse_otpauth(uri):
    if not uri.lower().startswith("otpauth://"):
        return None
    p = urlparse(uri)
    if p.scheme.lower() != "otpauth" or p.netloc.lower() != "totp":
        return None
    q = parse_qs(p.query)
    secret = normalize_secret(q.get("secret", [""])[0])
    if not secret:
        return None
    label = unquote(p.path.lstrip("/")) or "TOTP"
    issuer = q.get("issuer", [""])[0]
    return {"name": label, "secret": secret, "issuer": issuer}

def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📱 Aᴘᴘs Mᴀɴᴀɢᴇʀ", callback_data="apps"),
         InlineKeyboardButton("🚀 Qᴜɪᴄᴋ Dᴇᴘʟᴏʏ", callback_data="deploy")],
        [InlineKeyboardButton("🔐 2FA Aᴜᴛʜ", callback_data="totp"),
         InlineKeyboardButton("📊 Dʏɴᴏ Qᴜᴏᴛᴀ", callback_data="quota")],
        [InlineKeyboardButton("⚙️ Sᴇᴛᴛɪɴɢs", callback_data="settings"),
         InlineKeyboardButton("❓ Hᴇʟᴘ & Cᴏᴍᴍᴀɴᴅs", callback_data="help")],
        [InlineKeyboardButton("👑 Oᴡɴᴇʀ", url=OWNER_URL),
         InlineKeyboardButton("🆘 Sᴜᴘᴘᴏʀᴛ", url=SUPPORT_URL)],
    ])

def settings_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👤 Aᴄᴄᴏᴜɴᴛs Mᴀɴᴀɢᴇ", callback_data="accounts"),
         InlineKeyboardButton("➕ Aᴅᴅ Aᴄᴄᴏᴜɴᴛ", callback_data="add_account")],
        [InlineKeyboardButton("📊 Qᴜᴏᴛᴀ & Hᴏᴜʀs", callback_data="quota"),
         InlineKeyboardButton("💳 Iɴᴠᴏɪᴄᴇs & Bɪʟʟɪɴɢ", callback_data="billing")],
        [InlineKeyboardButton("🔐 2FA OTP Gᴇɴᴇʀᴀᴛᴏʀ", callback_data="totp"),
         InlineKeyboardButton("🔑 SSH Kᴇʏs Mᴀɴᴀɢᴇ", callback_data="ssh")],
        [InlineKeyboardButton("🎫 Aᴄᴛɪᴠᴇ API Tᴏᴋᴇɴs", callback_data="tokens"),
         InlineKeyboardButton("🧰 Mᴏʀᴇ Tᴏᴏʟs Sᴜɪᴛᴇ", callback_data="tools")],
        [InlineKeyboardButton("📈 Bᴏᴛ Sᴛᴀᴛɪsᴛɪᴄs", callback_data="stats"),
         InlineKeyboardButton("🔙 Bᴀᴄᴋ Tᴏ Dᴀsʜʙᴏᴀʀᴅ", callback_data="home")],
    ])

def totp_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Eɴᴛᴇʀ Kᴇʏ Mᴀɴᴜᴀʟʟʏ", callback_data="totp_manual"),
         InlineKeyboardButton("📷 Sᴄᴀɴ QR Cᴏᴅᴇ", callback_data="totp_scan")],
        [InlineKeyboardButton("🗄️ Sᴀᴠᴇᴅ Kᴇʏs Vᴀᴜʟᴛ", callback_data="totp_saved"),
         InlineKeyboardButton("🎲 Gᴇɴᴇʀᴀᴛᴇ Nᴇᴡ Kᴇʏ", callback_data="totp_new")],
        [InlineKeyboardButton("📖 Hᴇʀᴏᴋᴜ 2FA Gᴜɪᴅᴇ", callback_data="totp_guide"),
         InlineKeyboardButton("🔙 Bᴀᴄᴋ", callback_data="home")],
    ])

def app_keyboard(name):
    q = quote(name, safe="")
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Rᴇsᴛᴀʀᴛ", callback_data=f"restart:{q}"),
         InlineKeyboardButton("⚙️ Sᴄᴀʟᴇ", callback_data=f"scale:{q}")],
        [InlineKeyboardButton("🔑 Cᴏɴғɪɢ Vᴀʀs", callback_data=f"config:{q}"),
         InlineKeyboardButton("📜 Lᴏɢs", callback_data=f"logs:{q}")],
        [InlineKeyboardButton("🗑️ Dᴇʟᴇᴛᴇ Aᴘᴘ", callback_data=f"delete:{q}")],
        [InlineKeyboardButton("⬅️ Aᴘᴘs", callback_data="apps")],
    ])

async def edit_or_send(target, text, markup=None):
    try:
        if hasattr(target, "edit_message_text"):
            await target.edit_message_text(
                text, parse_mode=ParseMode.HTML, reply_markup=markup
            )
        else:
            await target.message.reply_text(
                text, parse_mode=ParseMode.HTML, reply_markup=markup
            )
    except BadRequest as e:
        if "Message is not modified" in str(e):
            return
        try:
            await target.message.reply_text(
                text, parse_mode=ParseMode.HTML, reply_markup=markup
            )
        except Exception:
            pass
    except Exception:
        try:
            await target.message.reply_text(
                text, parse_mode=ParseMode.HTML, reply_markup=markup
            )
        except Exception:
            pass

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    a = get_account(u.id)
    if a:
        status = (
            "🟢 <b>Cᴏɴɴᴇᴄᴛᴇᴅ</b>\n"
            f"╰─ Hᴇʀᴏᴋᴜ ID: <code>{safe(a[1] or 'Unknown')}</code>\n"
            f"╰─ Tᴏᴋᴇɴ: <code>{mask_token(a[0])}</code>"
        )
    else:
        status = "🔴 <b>Nᴏ Aᴄᴄᴏᴜɴᴛ Lɪɴᴋᴇᴅ</b>\n╰─ Use /link to connect"

    text = (
        "╭━━━━━━━━━━━━━━━━━━━━╮\n"
        "       ⚡ <b>Hᴇʀᴏᴋᴜ Cʟᴏᴜᴅ</b>\n"
        "          <b>Cᴏɴᴛʀᴏʟʟᴇʀ</b> 🚀\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
        f"👋 Welcome <b>{safe(u.first_name or 'User')}</b>!\n\n"
        "╭─ 👤 <b>Uѕᴇʀ Iɴғᴏ</b>\n"
        f"├─ ID: <code>{u.id}</code>\n"
        f"└─ {status}\n"
        "╰────────────────────╯\n\n"
        "✨ Manage Heroku apps, deployments, dynos, "
        "config vars and authorized TOTP keys directly from Telegram.\n\n"
        "💡 <i>Select an option below.</i>"
    )
    await update.message.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=main_keyboard()
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "╭━━━━━━━━━━━━━━━━━━━━╮\n"
        "       ❓ <b>Hᴇʟᴘ & Cᴏᴍᴍᴀɴᴅs</b>\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
        "📌 <b>Cᴏʀᴇ</b>\n"
        "├ /start — Dashboard\n"
        "├ /help — Help center\n"
        "├ /link — Link Heroku API token\n"
        "├ /unlink — Remove linked account\n"
        "├ /apps — List applications\n"
        "├ /deploy — GitHub quick deploy\n"
        "├ /settings — Settings\n"
        "├ /2fa — TOTP suite\n"
        "└ /cancel — Cancel active input\n\n"
        "🔐 <b>Sᴇᴄᴜʀɪᴛʏ</b>\n"
        "Use only accounts and secrets you are authorized to manage. "
        "Never share passwords or recovery codes."
    )
    await update.message.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=main_keyboard()
    )

async def cancel_cmd(update, context):
    pending.pop(update.effective_user.id, None)
    await update.message.reply_text(
        "❌ <b>Operation cancelled.</b>",
        parse_mode=ParseMode.HTML, reply_markup=main_keyboard()
    )

async def link_cmd(update, context):
    pending[update.effective_user.id] = {"action": "link"}
    await update.message.reply_text(
        "╭━━━━━━━━━━━━━━━━━━━━╮\n"
        "       🔐 <b>Lɪɴᴋ Hᴇʀᴏᴋᴜ</b>\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
        "Send your <b>Heroku API token</b>.\n\n"
        "⚠️ Use a token from an account you are authorized to manage.\n"
        "The token is validated before it is stored.\n\n"
        "❌ /cancel to stop.",
        parse_mode=ParseMode.HTML
    )

async def unlink_cmd(update, context):
    delete_account(update.effective_user.id)
    pending.pop(update.effective_user.id, None)
    await update.message.reply_text(
        "✅ <b>Heroku account unlinked.</b>\n\nStored token removed.",
        parse_mode=ParseMode.HTML, reply_markup=main_keyboard()
    )

async def settings_cmd(update, context):
    await show_settings(update.effective_user.id, update)

async def show_settings(uid, target):
    a = get_account(uid)
    if a:
        acc = (
            "🟢 <b>Aᴄᴛɪᴠᴇ Aᴄᴄᴏᴜɴᴛ</b>\n"
            f"├ Heroku ID: <code>{safe(a[1] or 'Unknown')}</code>\n"
            f"└ Email: <code>{safe(a[2] or 'Unknown')}</code>"
        )
    else:
        acc = "🔴 <b>Nᴏ Aᴄᴄᴏᴜɴᴛ Cᴏɴɴᴇᴄᴛᴇᴅ</b>"

    text = (
        "╭━━━━━━━━━━━━━━━━━━━━╮\n"
        "       ⚙️ <b>Hᴇʀᴏᴋᴜ Bᴏᴛ Sᴇᴛᴛɪɴɢs</b> ❞\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
        f"╭─ 🔐 <b>Aᴄᴄᴏᴜɴᴛ Sᴛᴀᴛᴜs</b> ❞\n│ {acc}\n"
        "│ 📱 <b>Aᴄᴛɪᴠᴇ Sᴇʟᴇᴄᴛᴇᴅ Aᴘᴘ:</b> "
        f"<i>{safe(selected_app.get(uid, 'None'))}</i>\n"
        "╰────────────────────╯\n\n"
        "👇 <i>Configure accounts, check dyno information, "
        "manage TOTP keys and open advanced cloud tools below.</i>"
    )
    await edit_or_send(target, text, settings_keyboard())

async def show_totp(target):
    text = (
        "╭━━━━━━━━━━━━━━━━━━━━╮\n"
        "   🔐 <b>2FA Aᴜᴛʜᴇɴᴛɪᴄᴀᴛᴏʀ & TOTP Sᴜɪᴛᴇ</b> ❞\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
        "╭─ 💡 <b>Fᴇᴀᴛᴜʀᴇs</b> ❞\n"
        "│ Generate live <b>6-digit TOTP</b> codes\n"
        "│ according to <b>RFC 6238</b> with\n"
        "│ automatic <b>30s rotation</b>.\n"
        "╰────────────────────╯\n\n"
        "👉 <i>Select an option below to generate or manage your authorized 2FA keys.</i>"
    )
    await edit_or_send(target, text, totp_keyboard())

async def show_saved(uid, target):
    keys = get_totps(uid)
    if not keys:
        text = (
            "🗄️ <b>Sᴀᴠᴇᴅ Kᴇʏs Vᴀᴜʟᴛ</b>\n\n"
            "🔴 No TOTP keys saved yet."
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📝 Aᴅᴅ Kᴇʏ", callback_data="totp_manual")],
            [InlineKeyboardButton("🔙 Bᴀᴄᴋ", callback_data="totp")],
        ])
        await edit_or_send(target, text, kb)
        return

    lines = ["╭━━━━━━━━━━━━━━━━━━━━╮", "  🗄️ <b>Sᴀᴠᴇᴅ Kᴇʏs Vᴀᴜʟᴛ</b>", "╰━━━━━━━━━━━━━━━━━━━━╯", ""]
    rows = []
    for kid, name, secret, issuer in keys:
        try:
            otp, rem = generate_totp(secret)
        except Exception:
            otp, rem = "ERROR", 0
        lines += [
            f"🔐 <b>{safe(name)}</b>",
            f"🏷️ {safe(issuer or 'Unknown')}",
            f"🔢 <code>{otp}</code>  ⏳ {rem}s",
            f"🔑 <code>{mask_token(secret)}</code>",
            "",
        ]
        rows.append([
            InlineKeyboardButton(f"🔢 {name[:28]}", callback_data=f"otp:{kid}"),
            InlineKeyboardButton("🗑️", callback_data=f"delotp:{kid}"),
        ])
    rows.append([InlineKeyboardButton("🔙 Bᴀᴄᴋ", callback_data="totp")])
    await edit_or_send(target, "\n".join(lines), InlineKeyboardMarkup(rows))

async def show_one_otp(uid, kid, target):
    row = get_totp(uid, kid)
    if not row:
        await edit_or_send(target, "❌ TOTP key not found.", totp_keyboard())
        return
    _, name, secret, issuer = row
    try:
        otp, rem = generate_totp(secret)
    except Exception:
        otp, rem = "INVALID", 0
    text = (
        "╭━━━━━━━━━━━━━━━━━━━━╮\n"
        f"       🔢 <b>{safe(name)}</b>\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
        f"🔢 <b>Current Code</b>\n<code>{otp}</code>\n\n"
        f"⏳ Expires in: <code>{rem}s</code>\n"
        f"🏷️ Issuer: <code>{safe(issuer or 'Unknown')}</code>\n\n"
        "🔄 Code changes every 30 seconds."
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Rᴇғʀᴇsʜ", callback_data=f"otp:{kid}")],
        [InlineKeyboardButton("🗑️ Dᴇʟᴇᴛᴇ", callback_data=f"delotp:{kid}")],
        [InlineKeyboardButton("🔙 Sᴀᴠᴇᴅ Kᴇʏs", callback_data="totp_saved")],
    ])
    await edit_or_send(target, text, kb)

async def totp_cmd(update, context):
    await show_totp(update)

async def apps_cmd(update, context):
    await show_apps(update.effective_user.id, update)

async def show_apps(uid, target):
    try:
        apps = await heroku_request(uid, "GET", "/apps")
    except RuntimeError as e:
        text = "🔑 <b>No Heroku account linked.</b>\n\nUse /link first." if str(e) == "NO_ACCOUNT" else "❌ <b>Unable to load apps.</b>\n\nCheck the API token."
        await edit_or_send(target, text, main_keyboard())
        return
    except Exception as e:
        log.warning("apps: %s", e)
        await edit_or_send(target, "❌ Failed to load Heroku apps.", main_keyboard())
        return

    if not apps:
        await edit_or_send(target, "📱 <b>Aᴘᴘs Mᴀɴᴀɢᴇʀ</b>\n\nNo apps found.", main_keyboard())
        return

    rows = []
    for app in apps[:100]:
        name = app.get("name", "unknown")
        rows.append([InlineKeyboardButton(f"📦 {name}", callback_data=f"app:{quote(name, safe='')}")])
    rows.append([InlineKeyboardButton("⬅️ Bᴀᴄᴋ", callback_data="home")])
    await edit_or_send(
        target,
        f"📱 <b>Aᴘᴘs Mᴀɴᴀɢᴇʀ</b>\n\nFound <b>{len(apps)}</b> app(s).",
        InlineKeyboardMarkup(rows)
    )

async def show_app(uid, name, target):
    try:
        app = await heroku_request(uid, "GET", f"/apps/{quote(name, safe='')}")
        formation = await heroku_request(uid, "GET", f"/apps/{quote(name, safe='')}/formation")
    except Exception as e:
        log.warning("show_app: %s", e)
        await edit_or_send(target, "❌ Failed to load app information.", main_keyboard())
        return

    selected_app[uid] = name
    region = (app.get("region") or {}).get("name", "?")
    stack = (app.get("build_stack") or {}).get("name", "?")
    lines = [
        "╭━━━━━━━━━━━━━━━━━━━━╮",
        f"       📦 <b>{safe(name)}</b>",
        "╰━━━━━━━━━━━━━━━━━━━━╯",
        f"🌎 Region: <code>{safe(region)}</code>",
        f"🧱 Stack: <code>{safe(stack)}</code>",
        f"🕒 Updated: <code>{safe(app.get('updated_at','?'))}</code>",
        "",
        "⚙️ <b>Fᴏʀᴍᴀᴛɪᴏɴ</b>",
    ]
    for x in formation:
        lines.append(
            f"• <b>{safe(x.get('type'))}</b>: "
            f"{x.get('quantity',0)} × {safe(x.get('size','?'))}"
        )
    await edit_or_send(target, "\n".join(lines), app_keyboard(name))

async def restart_app(uid, name, target):
    try:
        await heroku_request(uid, "POST", f"/apps/{quote(name,safe='')}/actions/restart")
        await edit_or_send(target, f"✅ <b>{safe(name)}</b> restarted successfully.", app_keyboard(name))
    except Exception as e:
        await edit_or_send(target, f"❌ Restart failed.\n\n<code>{safe(str(e)[:500])}</code>", app_keyboard(name))

async def scale_app(uid, name, target):
    try:
        formation = await heroku_request(uid, "GET", f"/apps/{quote(name,safe='')}/formation")
    except Exception:
        await edit_or_send(target, "❌ Unable to read formation.", app_keyboard(name))
        return
    if not formation:
        await edit_or_send(target, "❌ No process types found.", app_keyboard(name))
        return
    rows = []
    for x in formation:
        typ = x.get("type", "web")
        qty = int(x.get("quantity", 0))
        rows.append([
            InlineKeyboardButton(f"➖ {typ}", callback_data=f"scale_set:{quote(name,safe='')}:{quote(typ,safe='')}:{max(0,qty-1)}"),
            InlineKeyboardButton(f"{typ}: {qty}", callback_data="noop"),
            InlineKeyboardButton(f"➕ {typ}", callback_data=f"scale_set:{quote(name,safe='')}:{quote(typ,safe='')}:{qty+1}"),
        ])
    rows.append([InlineKeyboardButton("🔙 Bᴀᴄᴋ", callback_data=f"app:{quote(name,safe='')}")])
    await edit_or_send(target, "⚙️ <b>Sᴄᴀʟᴇ Aᴘᴘ</b>\n\nChoose process quantity:", InlineKeyboardMarkup(rows))

async def set_scale(uid, name, typ, qty, target):
    try:
        formation = await heroku_request(uid, "GET", f"/apps/{quote(name,safe='')}/formation")
        updates = []
        for x in formation:
            updates.append({
                "type": x.get("type"),
                "quantity": qty if x.get("type") == typ else int(x.get("quantity", 0)),
                "size": x.get("size")
            })
        await heroku_request(
            uid, "PATCH", f"/apps/{quote(name,safe='')}/formation",
            json={"updates": updates}
        )
        await show_app(uid, name, target)
    except Exception as e:
        await edit_or_send(target, f"❌ Scaling failed.\n\n<code>{safe(str(e)[:700])}</code>", app_keyboard(name))

async def show_config(uid, name, target):
    try:
        data = await heroku_request(uid, "GET", f"/apps/{quote(name,safe='')}/config-vars")
    except Exception as e:
        await edit_or_send(target, f"❌ Unable to read config vars.\n<code>{safe(str(e)[:500])}</code>", app_keyboard(name))
        return
    lines = ["🔑 <b>Cᴏɴғɪɢ Vᴀʀs</b>", f"📦 <b>{safe(name)}</b>", ""]
    for k, v in sorted(data.items()):
        # Do not expose secret values in Telegram.
        lines.append(f"• <code>{safe(k)}</code> = <i>••••••••</i>")
    if not data:
        lines.append("No config vars.")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Aᴅᴅ / Uᴘᴅᴀᴛᴇ", callback_data=f"config_add:{quote(name,safe='')}")],
        [InlineKeyboardButton("🗑️ Rᴇᴍᴏᴠᴇ Kᴇʏ", callback_data=f"config_del:{quote(name,safe='')}")],
        [InlineKeyboardButton("🔙 Bᴀᴄᴋ", callback_data=f"app:{quote(name,safe='')}")],
    ])
    await edit_or_send(target, "\n".join(lines), kb)

async def show_logs(uid, name, target):
    try:
        payload = {"lines": 100, "tail": True, "source": "app"}
        session = await heroku_request(
            uid, "POST", f"/apps/{quote(name,safe='')}/log-sessions", json=payload
        )
        url = session.get("logplex_url") or session.get("url")
        if not url:
            raise RuntimeError("No log session URL returned")
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(url) as r:
                logs = await r.text()
        logs = logs[-5000:] if logs else "No logs returned."
        text = f"📜 <b>Lᴏɢs — {safe(name)}</b>\n\n<pre>{safe(logs)}</pre>"
        await edit_or_send(target, text, InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Rᴇғʀᴇsʜ", callback_data=f"logs:{quote(name,safe='')}")],
            [InlineKeyboardButton("🔙 Bᴀᴄᴋ", callback_data=f"app:{quote(name,safe='')}")],
        ]))
    except Exception as e:
        await edit_or_send(target, f"❌ Unable to load logs.\n\n<code>{safe(str(e)[:800])}</code>", app_keyboard(name))

async def deploy_cmd(update, context):
    pending[update.effective_user.id] = {"action": "deploy"}
    await update.message.reply_text(
        "╭━━━━━━━━━━━━━━━━━━━━╮\n"
        "       🚀 <b>Qᴜɪᴄᴋ Dᴇᴘʟᴏʏ</b>\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
        "Send a public GitHub repository URL.\n\n"
        "<code>https://github.com/owner/repo</code>\n\n"
        "❌ /cancel to stop.",
        parse_mode=ParseMode.HTML
    )

async def quick_deploy(uid, repo_url, target):
    p = urlparse(repo_url.strip())
    if p.scheme not in ("http", "https") or p.netloc.lower() not in ("github.com", "www.github.com"):
        await edit_or_send(target, "❌ Please send a valid public GitHub URL.", main_keyboard())
        return
    parts = [x for x in p.path.strip("/").split("/") if x]
    if len(parts) < 2:
        await edit_or_send(target, "❌ Invalid GitHub repository URL.", main_keyboard())
        return
    owner, repo = parts[0], parts[1].removesuffix(".git")
    app_name = re.sub(r"[^a-z0-9-]", "-", f"{repo}-{str(uid)[-6:]}".lower()).strip("-")[:30]
    if not app_name:
        app_name = f"app-{str(uid)[-8:]}"
    try:
        app = await heroku_request(uid, "POST", "/apps", json={"name": app_name})
        archive = f"https://github.com/{quote(owner, safe='')}/{quote(repo, safe='')}/archive/refs/heads/{quote(p.path.strip('/').split('/')[2] if len(parts)>2 and p.path.strip('/').split('/')[2] else 'main', safe='')}.tar.gz"
        # If branch archive fails, Heroku will report the build error.
        build = await heroku_request(
            uid,
            "POST",
            f"/apps/{quote(app['name'],safe='')}/builds",
            json={"source_blob": {"url": archive, "version": str(int(time.time()))}}
        )
        await edit_or_send(
            target,
            "🚀 <b>Dᴇᴘʟᴏʏ Sᴛᴀʀᴛᴇᴅ</b>\n\n"
            f"📦 App: <code>{safe(app['name'])}</code>\n"
            f"🆔 Build: <code>{safe(build.get('id','?'))}</code>\n\n"
            "The build is running on Heroku.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("📦 Aᴘᴘ", callback_data=f"app:{quote(app['name'],safe='')}")],
                [InlineKeyboardButton("🏠 Hᴏᴍᴇ", callback_data="home")],
            ])
        )
    except Exception as e:
        await edit_or_send(
            target,
            "❌ <b>Dᴇᴘʟᴏʏ Fᴀɪʟᴇᴅ</b>\n\n"
            f"<code>{safe(str(e)[:1000])}</code>",
            main_keyboard()
        )

async def quota(uid, target):
    try:
        apps = await heroku_request(uid, "GET", "/apps")
        total = 0
        details = []
        for app in apps[:50]:
            name = app.get("name", "?")
            formation = await heroku_request(uid, "GET", f"/apps/{quote(name,safe='')}/formation")
            count = sum(int(x.get("quantity", 0)) for x in formation)
            total += count
            if count:
                details.append(f"• <code>{safe(name)}</code>: {count}")
        text = (
            "╭━━━━━━━━━━━━━━━━━━━━╮\n"
            "       📊 <b>Dʏɴᴏ Uѕᴀɢᴇ</b>\n"
            "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
            f"📱 Apps: <b>{len(apps)}</b>\n"
            f"⚡ Configured processes: <b>{total}</b>\n\n"
            + ("\n".join(details) if details else "No dynos configured.")
            + "\n\nℹ️ This is current formation usage, not a billing-plan quota."
        )
    except Exception as e:
        text = f"❌ Unable to read dyno information.\n<code>{safe(str(e)[:500])}</code>"
    await edit_or_send(target, text, main_keyboard())

async def text_handler(update, context):
    uid = update.effective_user.id
    state = pending.get(uid)
    if not state:
        return
    text = (update.message.text or "").strip()
    if text.lower() == "/cancel":
        pending.pop(uid, None)
        await update.message.reply_text("❌ Cancelled.", reply_markup=main_keyboard())
        return

    action = state.get("action")

    if action == "link":
        token = text
        # Token is used only to validate the account; do not echo it back.
        try:
            data = await heroku_request_with_token(token, "GET", "/account")
            save_account(uid, token, data.get("id",""), data.get("email",""))
            pending.pop(uid, None)
            await update.message.reply_text(
                "✅ <b>Heroku account connected.</b>\n\n"
                f"👤 ID: <code>{safe(data.get('id','Unknown'))}</code>\n"
                f"📧 Email: <code>{safe(data.get('email','Unknown'))}</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=main_keyboard()
            )
        except Exception:
            await update.message.reply_text(
                "❌ <b>Invalid or unauthorized Heroku API token.</b>\n\n"
                "No token was saved.",
                parse_mode=ParseMode.HTML
            )
        return

    if action == "deploy":
        pending.pop(uid, None)
        await update.message.reply_text("⏳ Starting deployment…", parse_mode=ParseMode.HTML)
        await quick_deploy(uid, text, update)
        return

    if action == "totp_manual":
        parsed = parse_otpauth(text)
        if parsed:
            secret, name, issuer = parsed["secret"], parsed["name"], parsed["issuer"]
        else:
            secret = normalize_secret(text)
            if not re.fullmatch(r"[A-Z2-7]{8,}", secret):
                await update.message.reply_text(
                    "❌ Invalid Base32 secret or otpauth:// URI.",
                    parse_mode=ParseMode.HTML
                )
                return
            name, issuer = "My TOTP", ""
        try:
            otp, rem = generate_totp(secret)
        except Exception:
            await update.message.reply_text("❌ Invalid TOTP secret.", parse_mode=ParseMode.HTML)
            return
        pending[uid] = {
            "action": "totp_name",
            "secret": secret,
            "issuer": issuer,
            "default_name": name,
        }
        await update.message.reply_text(
            f"🔐 <b>Key validated.</b>\n\n"
            f"Current OTP: <code>{otp}</code>\n"
            f"Expires in: <code>{rem}s</code>\n\n"
            "Send a name for this key.\n"
            f"Example: <code>{safe(name)}</code>",
            parse_mode=ParseMode.HTML
        )
        return

    if action == "totp_name":
        st = pending.pop(uid)
        name = text[:60] or st.get("default_name", "My TOTP")
        save_totp(uid, name, st["secret"], st.get("issuer",""))
        await update.message.reply_text(
            "✅ <b>TOTP key saved.</b>\n\n"
            f"🏷️ {safe(name)}\n"
            f"🔑 <code>{mask_token(st['secret'])}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🗄️ Sᴀᴠᴇᴅ Kᴇʏs", callback_data="totp_saved")],
                [InlineKeyboardButton("🔙 2FA", callback_data="totp")],
            ])
        )
        return

    if action == "config_add":
        name = state["app"]
        if "=" not in text:
            await update.message.reply_text("❌ Format: <code>KEY=value</code>", parse_mode=ParseMode.HTML)
            return
        key, value = text.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            await update.message.reply_text("❌ Invalid config key.", parse_mode=ParseMode.HTML)
            return
        try:
            await heroku_request(uid, "PATCH", f"/apps/{quote(name,safe='')}/config-vars", json={key: value})
            pending.pop(uid, None)
            await update.message.reply_text("✅ Config var updated.", parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cᴏɴғɪɢ", callback_data=f"config:{quote(name,safe='')}")]]))
        except Exception as e:
            await update.message.reply_text(f"❌ Failed.\n<code>{safe(str(e)[:700])}</code>", parse_mode=ParseMode.HTML)
        return

    if action == "config_del":
        name = state["app"]
        key = text.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            await update.message.reply_text("❌ Invalid config key.", parse_mode=ParseMode.HTML)
            return
        try:
            await heroku_request(uid, "PATCH", f"/apps/{quote(name,safe='')}/config-vars", json={key: None})
            pending.pop(uid, None)
            await update.message.reply_text("✅ Config var removed.", parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cᴏɴғɪɢ", callback_data=f"config:{quote(name,safe='')}")]]))
        except Exception as e:
            await update.message.reply_text(f"❌ Failed.\n<code>{safe(str(e)[:700])}</code>", parse_mode=ParseMode.HTML)

async def heroku_request_with_token(token, method, path, **kwargs):
    headers = dict(HEROKU_HEADERS)
    headers["Authorization"] = f"Bearer {token}"
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.request(HEROKU_API + path, headers=headers, **kwargs) as r:
            body = await r.text()
            if r.status >= 400:
                raise RuntimeError(f"HEROKU_{r.status}:{body[:500]}")
            return json.loads(body) if body else {}

async def photo_handler(update, context):
    uid = update.effective_user.id
    state = pending.get(uid)
    if not state or state.get("action") != "totp_qr":
        return
    if cv2 is None:
        await update.message.reply_text(
            "❌ QR scanner dependency is unavailable. Reinstall requirements.txt."
        )
        return
    photo = update.message.photo[-1]
    f = await context.bot.get_file(photo.file_id)
    path = f"/tmp/qr_{uid}_{int(time.time())}.jpg"
    await f.download_to_drive(path)
    try:
        image = cv2.imread(path)
        detector = cv2.QRCodeDetector()
        data, points, _ = detector.detectAndDecode(image)
        if not data:
            await update.message.reply_text(
                "❌ No QR/otpauth data detected. Send a clear authenticator QR image."
            )
            return
        parsed = parse_otpauth(data.strip())
        if not parsed:
            await update.message.reply_text(
                "❌ QR found, but it is not a supported TOTP otpauth:// QR."
            )
            return
        pending[uid] = {
            "action": "totp_name",
            "secret": parsed["secret"],
            "issuer": parsed["issuer"],
            "default_name": parsed["name"],
        }
        otp, rem = generate_totp(parsed["secret"])
        await update.message.reply_text(
            "✅ <b>QR scanned successfully.</b>\n\n"
            f"🏷️ {safe(parsed['name'])}\n"
            f"🔢 OTP: <code>{otp}</code>\n"
            f"⏳ Expires in: <code>{rem}s</code>\n\n"
            "Send a name to save this key, or /cancel.",
            parse_mode=ParseMode.HTML
        )
    finally:
        try:
            os.remove(path)
        except OSError:
            pass

async def callbacks(update, context):
    q = update.callback_query
    await q.answer()
    uid = update.effective_user.id
    data = q.data or ""

    if data == "noop":
        return
    if data == "home":
        await q.edit_message_text(
            "╭━━━━━━━━━━━━━━━━━━━━╮\n"
            "       ⚡ <b>Hᴇʀᴏᴋᴜ Cʟᴏᴜᴅ Cᴏɴᴛʀᴏʟʟᴇʀ</b>\n"
            "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
            "Choose an option below.",
            parse_mode=ParseMode.HTML, reply_markup=main_keyboard()
        )
    elif data == "settings":
        await show_settings(uid, q)
    elif data == "totp":
        await show_totp(q)
    elif data == "totp_manual":
        pending[uid] = {"action": "totp_manual"}
        await q.message.reply_text(
            "📝 <b>Eɴᴛᴇʀ TOTP Kᴇʏ</b>\n\n"
            "Send a Base32 secret or full otpauth:// URI.",
            parse_mode=ParseMode.HTML
        )
    elif data == "totp_scan":
        pending[uid] = {"action": "totp_qr"}
        await q.message.reply_text(
            "📷 <b>Sᴄᴀɴ QR Cᴏᴅᴇ</b>\n\n"
            "Send the authenticator QR image here. "
            "The scanner runs locally in this bot; the image is deleted after processing.",
            parse_mode=ParseMode.HTML
        )
    elif data == "totp_saved":
        await show_saved(uid, q)
    elif data == "totp_new":
        secret = base64.b32encode(os.urandom(20)).decode().rstrip("=")
        otp, rem = generate_totp(secret)
        await q.edit_message_text(
            "╭━━━━━━━━━━━━━━━━━━━━╮\n"
            "       🎲 <b>Nᴇᴡ TOTP Kᴇʏ</b>\n"
            "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
            f"🔑 <code>{secret}</code>\n\n"
            f"🔢 OTP: <code>{otp}</code>\n"
            f"⏳ Expires in: <code>{rem}s</code>\n\n"
            "⚠️ This generates a new secret for an authenticator setup you control.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💾 Sᴀᴠᴇ", callback_data=f"savegen:{secret}")],
                [InlineKeyboardButton("🔙 Bᴀᴄᴋ", callback_data="totp")],
            ])
        )
    elif data.startswith("savegen:"):
        secret = normalize_secret(data.split(":",1)[1])
        pending[uid] = {"action":"totp_name","secret":secret,"issuer":"","default_name":"Generated Key"}
        await q.message.reply_text("💾 Send a name for this key.", parse_mode=ParseMode.HTML)
    elif data.startswith("otp:"):
        await show_one_otp(uid, int(data.split(":",1)[1]), q)
    elif data.startswith("delotp:"):
        kid = int(data.split(":",1)[1])
        row = get_totp(uid, kid)
        if not row:
            await q.answer("Key not found.", show_alert=True)
            return
        await q.edit_message_text(
            f"⚠️ Delete <b>{safe(row[1])}</b>?",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Dᴇʟᴇᴛᴇ", callback_data=f"confirmdel:{kid}"),
                 InlineKeyboardButton("❌ Cᴀɴᴄᴇʟ", callback_data="totp_saved")]
            ])
        )
    elif data.startswith("confirmdel:"):
        delete_totp(uid, int(data.split(":",1)[1]))
        await show_saved(uid, q)
    elif data == "totp_guide":
        await q.edit_message_text(
            "📖 <b>2FA Gᴜɪᴅᴇ</b>\n\n"
            "1. Enable authenticator-based 2FA on an account you control.\n"
            "2. Use the QR scanner or enter the Base32 secret.\n"
            "3. The bot validates the TOTP secret locally.\n"
            "4. Saved keys produce RFC 6238 6-digit codes.\n\n"
            "⚠️ Never share TOTP secrets or recovery codes.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Bᴀᴄᴋ", callback_data="totp")]])
        )
    elif data == "apps":
        await show_apps(uid, q)
    elif data.startswith("app:"):
        await show_app(uid, unquote(data.split(":",1)[1]), q)
    elif data.startswith("restart:"):
        await restart_app(uid, unquote(data.split(":",1)[1]), q)
    elif data.startswith("scale:"):
        await scale_app(uid, unquote(data.split(":",1)[1]), q)
    elif data.startswith("scale_set:"):
        _, ename, etype, eqty = data.split(":", 3)
        await set_scale(uid, unquote(ename), unquote(etype), int(eqty), q)
    elif data.startswith("config:"):
        await show_config(uid, unquote(data.split(":",1)[1]), q)
    elif data.startswith("config_add:"):
        name = unquote(data.split(":",1)[1])
        pending[uid] = {"action":"config_add","app":name}
        await q.message.reply_text(
            "➕ Send <code>KEY=value</code>.\n\n"
            "⚠️ The value is written directly to Heroku and is not echoed by the bot.",
            parse_mode=ParseMode.HTML
        )
    elif data.startswith("config_del:"):
        name = unquote(data.split(":",1)[1])
        pending[uid] = {"action":"config_del","app":name}
        await q.message.reply_text(
            "🗑️ Send the config key name to remove.\nExample: <code>API_KEY</code>",
            parse_mode=ParseMode.HTML
        )
    elif data.startswith("logs:"):
        await show_logs(uid, unquote(data.split(":",1)[1]), q)
    elif data.startswith("delete:"):
        name = unquote(data.split(":",1)[1])
        await q.edit_message_text(
            f"⚠️ <b>Dᴇʟᴇᴛᴇ Aᴘᴘ?</b>\n\n"
            f"App: <code>{safe(name)}</code>\n\n"
            "This action is permanent.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🗑️ Cᴏɴғɪʀᴍ", callback_data=f"confirmdelete:{quote(name,safe='')}"),
                 InlineKeyboardButton("❌ Cᴀɴᴄᴇʟ", callback_data=f"app:{quote(name,safe='')}")]
            ])
        )
    elif data.startswith("confirmdelete:"):
        name = unquote(data.split(":",1)[1])
        try:
            await heroku_request(uid, "DELETE", f"/apps/{quote(name,safe='')}")
            selected_app.pop(uid, None)
            await q.edit_message_text(
                f"✅ App <b>{safe(name)}</b> deleted.",
                parse_mode=ParseMode.HTML,
                reply_markup=main_keyboard()
            )
        except Exception as e:
            await q.edit_message_text(
                f"❌ Delete failed.\n<code>{safe(str(e)[:700])}</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=app_keyboard(name)
            )
    elif data == "deploy":
        pending[uid] = {"action":"deploy"}
        await q.message.reply_text(
            "🚀 Send a public GitHub repository URL.",
            parse_mode=ParseMode.HTML
        )
    elif data == "quota":
        await quota(uid, q)
    elif data == "accounts":
        a = get_account(uid)
        txt = "👤 <b>Aᴄᴄᴏᴜɴᴛ Mᴀɴᴀɢᴇʀ</b>\n\n"
        txt += (
            f"🟢 Connected\nID: <code>{safe(a[1] or 'Unknown')}</code>\n"
            f"Email: <code>{safe(a[2] or 'Unknown')}</code>"
            if a else "🔴 No account connected."
        )
        await q.edit_message_text(
            txt, parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Aᴅᴅ Aᴄᴄᴏᴜɴᴛ", callback_data="add_account")],
                [InlineKeyboardButton("🔙 Sᴇᴛᴛɪɴɢs", callback_data="settings")],
            ])
        )
    elif data == "add_account":
        pending[uid] = {"action":"link"}
        await q.message.reply_text(
            "🔐 Send your Heroku API token.\n\n❌ /cancel to stop.",
            parse_mode=ParseMode.HTML
        )
    elif data == "help":
        await q.edit_message_text(
            "❓ <b>Hᴇʟᴘ & Cᴏᴍᴍᴀɴᴅs</b>\n\n"
            "Use /help for the command list.\n\n"
            "Buttons provide Apps Manager, Quick Deploy, TOTP, "
            "Quota and Settings.",
            parse_mode=ParseMode.HTML,
            reply_markup=main_keyboard()
        )
    elif data in ("billing","ssh","tokens","tools","stats"):
        labels = {
            "billing":"💳 Iɴᴠᴏɪᴄᴇs & Bɪʟʟɪɴɢ",
            "ssh":"🔑 SSH Kᴇʏs Mᴀɴᴀɢᴇ",
            "tokens":"🎫 Aᴄᴛɪᴠᴇ API Tᴏᴋᴇɴs",
            "tools":"🧰 Mᴏʀᴇ Tᴏᴏʟs Sᴜɪᴛᴇ",
            "stats":"📈 Bᴏᴛ Sᴛᴀᴛɪsᴛɪᴄs",
        }
        await q.edit_message_text(
            f"<b>{labels[data]}</b>\n\nModule is ready for a provider-specific API integration.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Sᴇᴛᴛɪɴɢs", callback_data="settings")]])
        )

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("link", link_cmd))
    app.add_handler(CommandHandler("unlink", unlink_cmd))
    app.add_handler(CommandHandler("apps", apps_cmd))
    app.add_handler(CommandHandler("deploy", deploy_cmd))
    app.add_handler(CommandHandler("settings", settings_cmd))
    app.add_handler(CommandHandler("2fa", totp_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CallbackQueryHandler(callbacks))
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    log.info("Heroku Controller started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
