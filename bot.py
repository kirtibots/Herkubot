import os
import re
import sqlite3
import secrets
import logging
from urllib.parse import quote, urlparse

import aiohttp
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters,
)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("heroku-controller")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)
SUPPORT_URL = os.getenv("SUPPORT_URL", "https://t.me/")
OWNER_URL = os.getenv("OWNER_URL", "https://t.me/")
DB_PATH = os.getenv("DB_PATH", "heroku_controller.db")

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
db.commit()

# Per-user temporary state. Never put secrets in Telegram messages/logs.
pending = {}


def get_account(user_id: int):
    row = db.execute(
        "SELECT token, heroku_user_id, email FROM accounts WHERE user_id=?",
        (user_id,),
    ).fetchone()
    return row


def save_account(user_id: int, token: str, heroku_user_id="", email=""):
    db.execute(
        """INSERT INTO accounts(user_id,token,heroku_user_id,email)
           VALUES(?,?,?,?)
           ON CONFLICT(user_id) DO UPDATE SET
             token=excluded.token,
             heroku_user_id=excluded.heroku_user_id,
             email=excluded.email""",
        (user_id, token, heroku_user_id, email),
    )
    db.commit()


def delete_account(user_id: int):
    db.execute("DELETE FROM accounts WHERE user_id=?", (user_id,))
    db.commit()


def mask_token(token: str) -> str:
    if len(token) < 10:
        return "••••••••"
    return token[:5] + "…" + token[-4:]


async def heroku_request(user_id: int, method: str, path: str, **kwargs):
    account = get_account(user_id)
    if not account:
        raise RuntimeError("NO_ACCOUNT")

    token = account[0]
    headers = dict(HEROKU_HEADERS)
    headers["Authorization"] = f"Bearer {token}"

    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.request(
            method, HEROKU_API + path, headers=headers, **kwargs
        ) as r:
            text = await r.text()
            if r.status >= 400:
                raise RuntimeError(f"HEROKU_{r.status}:{text[:500]}")
            if not text:
                return {}
            try:
                return await r.json()
            except Exception:
                return {"raw": text}


def main_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📱 Apps Manager", callback_data="apps"),
            InlineKeyboardButton("🚀 Quick Deploy", callback_data="deploy"),
        ],
        [
            InlineKeyboardButton("🔐 2FA Authenticator", callback_data="totp"),
            InlineKeyboardButton("📊 Dyno Quota", callback_data="quota"),
        ],
        [
            InlineKeyboardButton("⚙️ Settings", callback_data="settings"),
            InlineKeyboardButton("❓ Help & Commands", callback_data="help"),
        ],
        [
            InlineKeyboardButton("👑 Owner", url=OWNER_URL),
            InlineKeyboardButton("🆘 Support", url=SUPPORT_URL),
        ],
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    account = get_account(user.id)

    if account:
        account_text = (
            f"🟢 <b>Account Linked</b>\n"
            f"Heroku ID: <code>{account[1] or 'Unknown'}</code>\n"
            f"Token: <code>{mask_token(account[0])}</code>"
        )
    else:
        account_text = "🔑 <b>Active Account:</b> <i>No Account Linked</i>"

    text = (
        "⚡ <b>HEROKU CLOUD CONTROLLER</b>\n"
        "<i>Enterprise-style multi-app management & deploy panel</i>\n\n"
        f"👤 <b>User ID:</b> <code>{user.id}</code>\n"
        f"{account_text}\n\n"
        "🚀 Manage apps, dynos, config vars and deployments "
        "directly from Telegram."
    )
    await update.message.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=main_keyboard()
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "❓ <b>HELP & COMMANDS</b>\n\n"
        "/start — Open control panel\n"
        "/help — Show this help\n"
        "/link — Link a Heroku API token\n"
        "/unlink — Remove your linked account\n"
        "/apps — List Heroku apps\n"
        "/deploy — Quick deploy from GitHub\n"
        "/settings — Account settings\n\n"
        "⚠️ Use only your own Heroku account. "
        "Never send passwords or recovery codes to this bot.",
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard(),
    )


async def link_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending[update.effective_user.id] = {"action": "link"}
    await update.message.reply_text(
        "🔐 <b>Link Heroku Account</b>\n\n"
        "Send your <b>Heroku API token</b> in the next message.\n"
        "Do not send your Heroku password.\n\n"
        "You can obtain the token from your own Heroku account/CLI.\n"
        "After validation, the message is deleted when Telegram allows it.\n\n"
        "Send /cancel to stop.",
        parse_mode=ParseMode.HTML,
    )


async def unlink_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    delete_account(update.effective_user.id)
    pending.pop(update.effective_user.id, None)
    await update.message.reply_text("✅ Heroku account unlinked.")


async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_settings(update.effective_user.id, update)


async def apps_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_apps(update.effective_user.id, update)


async def deploy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending[update.effective_user.id] = {"action": "deploy"}
    await update.message.reply_text(
        "🚀 Send a public GitHub repository URL.\n"
        "Example: <code>https://github.com/owner/repo</code>\n\n"
        "The bot uses Heroku's App Setups/Source Blob workflow for deployment.",
        parse_mode=ParseMode.HTML,
    )


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending.pop(update.effective_user.id, None)
    await update.message.reply_text("❌ Cancelled.")


async def show_apps(user_id, target):
    try:
        apps = await heroku_request(user_id, "GET", "/apps")
    except RuntimeError as e:
        if str(e) == "NO_ACCOUNT":
            text = "🔑 No Heroku account linked.\nUse /link first."
        else:
            text = "❌ Could not load apps. Check your token/account."
        await send_or_edit(target, text, main_keyboard())
        return

    if not apps:
        await send_or_edit(
            target, "📱 <b>Apps Manager</b>\n\nNo apps found.",
            main_keyboard()
        )
        return

    rows = []
    for app in apps[:40]:
        name = app.get("name", "unknown")
        rows.append([InlineKeyboardButton(
            f"📦 {name}", callback_data=f"app:{name}"
        )])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="home")])

    await send_or_edit(
        target,
        f"📱 <b>Apps Manager</b>\n\nFound <b>{len(apps)}</b> app(s).",
        InlineKeyboardMarkup(rows),
    )


async def show_app(user_id, app_name, target):
    try:
        app = await heroku_request(user_id, "GET", f"/apps/{quote(app_name)}")
        formations = await heroku_request(
            user_id, "GET", f"/apps/{quote(app_name)}/formation"
        )
    except Exception:
        await send_or_edit(target, "❌ Failed to load app.", main_keyboard())
        return

    lines = [
        f"📦 <b>{app.get('name')}</b>",
        f"🌎 Region: <code>{app.get('region', {}).get('name', '?')}</code>",
        f"🧱 Stack: <code>{app.get('build_stack', {}).get('name', '?')}</code>",
        f"🕒 Updated: <code>{app.get('updated_at', '?')}</code>",
        "",
        "⚙️ <b>Formation</b>",
    ]
    for f in formations:
        lines.append(
            f"• {f.get('type')}: {f.get('quantity')} × {f.get('size')}"
        )

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 Restart", callback_data=f"restart:{app_name}"),
            InlineKeyboardButton("⚙️ Scale", callback_data=f"scale:{app_name}"),
        ],
        [
            InlineKeyboardButton("🔑 Config Vars", callback_data=f"config:{app_name}"),
            InlineKeyboardButton("📜 Logs", callback_data=f"logs:{app_name}"),
        ],
        [InlineKeyboardButton("🗑️ Delete App", callback_data=f"delete:{app_name}")],
        [InlineKeyboardButton("⬅️ Apps", callback_data="apps")],
    ])
    await send_or_edit(target, "\n".join(lines), kb)


async def show_settings(user_id, target):
    account = get_account(user_id)
    if not account:
        body = "🔑 <b>Active Account:</b> No Account Linked"
    else:
        body = (
            "🟢 <b>Active Account</b>\n"
            f"Heroku ID: <code>{account[1] or 'Unknown'}</code>\n"
            f"Token: <code>{mask_token(account[0])}</code>"
        )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔐 Link / Replace Token", callback_data="link")],
        [InlineKeyboardButton("🗑️ Unlink Account", callback_data="unlink")],
        [InlineKeyboardButton("⬅️ Back", callback_data="home")],
    ])
    await send_or_edit(target, body, kb)


async def show_quota(user_id, target):
    try:
        apps = await heroku_request(user_id, "GET", "/apps")
        total = 0
        details = []
        for app in apps[:40]:
            name = app.get("name", "?")
            formation = await heroku_request(
                user_id, "GET", f"/apps/{quote(name)}/formation"
            )
            count = sum(int(x.get("quantity", 0)) for x in formation)
            total += count
            if count:
                details.append(f"• {name}: {count} dyno(s)")
        text = (
            "📊 <b>DYNO QUOTA / USAGE VIEW</b>\n\n"
            f"Apps: <b>{len(apps)}</b>\n"
            f"Configured dyno processes: <b>{total}</b>\n\n"
            + ("\n".join(details) if details else "No dynos configured.")
            + "\n\n<i>Billing/plan quota is account-dependent; "
              "this panel shows your current app formations.</i>"
        )
    except Exception:
        text = "❌ Unable to read dyno information."
    await send_or_edit(target, text, main_keyboard())


async def quick_deploy(user_id, repo_url, target):
    parsed = urlparse(repo_url)
    if parsed.scheme not in ("http", "https") or parsed.netloc.lower() != "github.com":
        await send_or_edit(
            target, "❌ Only public GitHub repository URLs are supported.",
            main_keyboard()
        )
        return

    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2:
        await send_or_edit(target, "❌ Invalid GitHub URL.", main_keyboard())
        return

    owner, repo = parts[0], parts[1].removesuffix(".git")
    tar_url = f"https://github.com/{owner}/{repo}/tarball/HEAD"

    app_name = re.sub(r"[^a-z0-9-]", "-", repo.lower())[:25].strip("-")
    app_name = f"{app_name}-{secrets.token_hex(2)}"

    try:
        data = {"source_blob": {"url": tar_url}, "app": {"name": app_name}}
        result = await heroku_request(
            user_id, "POST", "/app-setups",
            json=data,
        )
        setup_id = result.get("id", "unknown")
        await send_or_edit(
            target,
            "🚀 <b>Quick Deploy Started</b>\n\n"
            f"Repository: <code>{owner}/{repo}</code>\n"
            f"App: <code>{app_name}</code>\n"
            f"Setup ID: <code>{setup_id}</code>\n\n"
            "Heroku is processing the build/deploy.",
            main_keyboard(),
        )
    except Exception as e:
        log.warning("deploy failed: %s", str(e)[:200])
        await send_or_edit(
            target,
            "❌ Quick deploy failed.\n"
            "Check that the repository is public and contains a valid Heroku app.",
            main_keyboard(),
        )



async def show_help_center(target):
    text = (
        "❓ <b>HEROKU BOT HELP & COMMAND CENTER</b>\n\n"
        "💡 <i>Welcome to the Help Center! Tap any category button below "
        "to explore commands, features, and usage guides.</i>"
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📱 Apps", callback_data="helpcat:apps"),
            InlineKeyboardButton("⚡ Dynos", callback_data="helpcat:dynos"),
            InlineKeyboardButton("📋 Logs", callback_data="helpcat:logs"),
        ],
        [
            InlineKeyboardButton("⚙️ Vars", callback_data="helpcat:vars"),
            InlineKeyboardButton("🌐 Domains", callback_data="helpcat:domains"),
            InlineKeyboardButton("🔑 Account", callback_data="helpcat:account"),
        ],
        [
            InlineKeyboardButton("🔐 2FA Tools", callback_data="helpcat:2fa"),
            InlineKeyboardButton("👑 Admin Suite", callback_data="helpcat:admin"),
        ],
        [
            InlineKeyboardButton("📖 Interactive Use", callback_data="helpcat:interactive"),
            InlineKeyboardButton("⬅️ Back to Dashboard", callback_data="home"),
        ],
    ])
    await send_or_edit(target, text, kb)


async def show_help_category(target, category):
    pages = {
        "apps": (
            "📱 <b>APPS</b>\n\n"
            "/apps — List your Heroku apps\n"
            "📦 Tap an app to open its management panel.\n"
            "⚠️ Delete is irreversible and requires confirmation."
        ),
        "dynos": (
            "⚡ <b>DYNOS</b>\n\n"
            "📊 Dyno Quota shows current app formations.\n"
            "🔄 Restart stops running dynos so Heroku can replace them.\n"
            "⚙️ Scale accepts values such as <code>web=1</code>."
        ),
        "logs": (
            "📋 <b>LOGS</b>\n\n"
            "Open an app → 📜 Logs to request recent log output.\n"
            "The panel displays a limited amount of recent output."
        ),
        "vars": (
            "⚙️ <b>CONFIG VARS</b>\n\n"
            "Open an app → 🔑 Config Vars.\n"
            "Variable names are shown, while values are intentionally masked."
        ),
        "domains": (
            "🌐 <b>DOMAINS</b>\n\n"
            "Domain management is reserved for a future module in this build.\n"
            "No domain credentials or DNS secrets are collected."
        ),
        "account": (
            "🔑 <b>ACCOUNT</b>\n\n"
            "/link — Link a Heroku API token\n"
            "/unlink — Remove your stored token\n"
            "/settings — View account status\n\n"
            "⚠️ Never send your Heroku password, recovery codes, or login OTP."
        ),
        "2fa": (
            "🔐 <b>2FA TOOLS</b>\n\n"
            "This bot does not collect passwords, recovery codes, or login OTPs.\n"
            "Use your own authenticator app for account 2FA."
        ),
        "admin": (
            "👑 <b>ADMIN SUITE</b>\n\n"
            "Owner and Support buttons are available from the dashboard.\n"
            "Destructive owner-only commands are not exposed to normal users."
        ),
        "interactive": (
            "📖 <b>INTERACTIVE USE</b>\n\n"
            "1. Open /start.\n"
            "2. Tap a dashboard category.\n"
            "3. Follow the buttons and prompts.\n"
            "4. Use /cancel whenever an input flow is active.\n\n"
            "Commands: /start /help /link /unlink /apps /deploy /settings /cancel"
        ),
    }
    text = pages.get(category, "❌ Help category not found.")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Back to Help Center", callback_data="help")],
        [InlineKeyboardButton("🏠 Back to Dashboard", callback_data="home")],
    ])
    await send_or_edit(target, text, kb)


async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_id = q.from_user.id
    data = q.data

    if data == "home":
        account = get_account(user_id)
        status = "🟢 Account Linked" if account else "🔑 No Account Linked"
        await q.edit_message_text(
            "⚡ <b>HEROKU CLOUD CONTROLLER</b>\n\n"
            f"👤 User ID: <code>{user_id}</code>\n"
            f"🔑 Active Account: <b>{status}</b>\n\n"
            "Choose an option below.",
            parse_mode=ParseMode.HTML,
            reply_markup=main_keyboard(),
        )
    elif data == "help":
        await show_help_center(q)
    elif data.startswith("helpcat:"):
        await show_help_category(q, data.split(":", 1)[1])
    elif data == "apps":
        await show_apps(user_id, q)
    elif data == "deploy":
        pending[user_id] = {"action": "deploy"}
        await q.edit_message_text(
            "🚀 Send a public GitHub repository URL.\n"
            "Use /cancel to stop.",
        )
    elif data == "link":
        pending[user_id] = {"action": "link"}
        await q.edit_message_text(
            "🔐 Send your Heroku API token.\n"
            "Never send your Heroku password or recovery codes.\n\n"
            "Use /cancel to stop."
        )
    elif data == "unlink":
        delete_account(user_id)
        await q.edit_message_text("✅ Account unlinked.", reply_markup=main_keyboard())
    elif data == "settings":
        await show_settings(user_id, q)
    elif data == "quota":
        await show_quota(user_id, q)
    elif data == "totp":
        await q.edit_message_text(
            "🔐 <b>2FA Authenticator</b>\n\n"
            "This build intentionally does not collect or store Heroku "
            "passwords, recovery codes, or login OTPs.\n\n"
            "For account security, use your normal authenticator app. "
            "If you need a private TOTP manager for secrets you own, "
            "keep it local rather than sending the secret to a bot.",
            parse_mode=ParseMode.HTML,
            reply_markup=main_keyboard(),
        )
    elif data.startswith("app:"):
        await show_app(user_id, data.split(":", 1)[1], q)
    elif data.startswith("restart:"):
        app = data.split(":", 1)[1]
        try:
            dynos = await heroku_request(
                user_id, "GET", f"/apps/{quote(app)}/dynos"
            )
            restarted = 0
            for dyno in dynos:
                dyno_id = dyno.get("id") or dyno.get("name")
                if dyno_id:
                    await heroku_request(
                        user_id, "DELETE",
                        f"/apps/{quote(app)}/dynos/{quote(str(dyno_id))}"
                    )
                    restarted += 1
            await q.edit_message_text(
                f"🔄 Restart request sent for <b>{app}</b>. "
                f"Stopped <b>{restarted}</b> dyno(s); scaled dynos will be replaced.",
                parse_mode=ParseMode.HTML,
                reply_markup=main_keyboard(),
            )
        except Exception:
            await q.edit_message_text(
                "❌ Restart failed.", reply_markup=main_keyboard()
            )
    elif data.startswith("scale:"):
        app = data.split(":", 1)[1]
        pending[user_id] = {"action": "scale", "app": app}
        await q.edit_message_text(
            f"⚙️ Send scale value for <b>{app}</b>.\n"
            "Example: <code>web=1</code> or <code>worker=2</code>.\n"
            "Use /cancel to stop.",
            parse_mode=ParseMode.HTML,
        )
    elif data.startswith("config:"):
        app = data.split(":", 1)[1]
        try:
            cfg = await heroku_request(
                user_id, "GET", f"/apps/{quote(app)}/config-vars"
            )
            if not cfg:
                text = f"🔑 <b>{app}</b>\n\nNo config vars."
            else:
                # Values are deliberately masked.
                lines = [f"🔑 <b>{app} Config Vars</b>\n"]
                for key in sorted(cfg):
                    lines.append(f"• <code>{key}</code> = <i>••••••</i>")
                text = "\n".join(lines)
            await q.edit_message_text(
                text, parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ App", callback_data=f"app:{app}")]
                ])
            )
        except Exception:
            await q.edit_message_text(
                "❌ Could not read config vars.", reply_markup=main_keyboard()
            )
    elif data.startswith("logs:"):
        app = data.split(":", 1)[1]
        try:
            payload = {
                "dyno_name": "",
                "lines": 50,
                "source": "app",
                "tail": False,
            }
            result = await heroku_request(
                user_id, "POST", f"/apps/{quote(app)}/log-sessions",
                json=payload
            )
            log_url = result.get("logplex_url")
            if not log_url:
                raise RuntimeError("NO_LOG_URL")
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            ) as session:
                async with session.get(log_url) as r:
                    body = await r.text()
            body = body[-3500:] if body else "No logs returned."
            await q.edit_message_text(
                f"📜 <b>{app} Logs</b>\n<pre>{escape_html(body)}</pre>",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ App", callback_data=f"app:{app}")]
                ])
            )
        except Exception:
            await q.edit_message_text(
                "❌ Could not fetch logs.", reply_markup=main_keyboard()
            )
    elif data.startswith("delete:"):
        app = data.split(":", 1)[1]
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("⚠️ DELETE", callback_data=f"confirmdelete:{app}"),
                InlineKeyboardButton("Cancel", callback_data=f"app:{app}"),
            ]
        ])
        await q.edit_message_text(
            f"⚠️ Delete <b>{app}</b>? This is irreversible.",
            parse_mode=ParseMode.HTML, reply_markup=kb
        )
    elif data.startswith("confirmdelete:"):
        app = data.split(":", 1)[1]
        try:
            await heroku_request(user_id, "DELETE", f"/apps/{quote(app)}")
            await q.edit_message_text(
                f"🗑️ App <b>{app}</b> deleted.",
                parse_mode=ParseMode.HTML, reply_markup=main_keyboard()
            )
        except Exception:
            await q.edit_message_text(
                "❌ Delete failed.", reply_markup=main_keyboard()
            )


def escape_html(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


async def send_or_edit(target, text, keyboard):
    if hasattr(target, "edit_message_text"):
        try:
            await target.edit_message_text(
                text, parse_mode=ParseMode.HTML, reply_markup=keyboard
            )
            return
        except Exception:
            pass
    if hasattr(target, "message") and target.message:
        await target.message.reply_text(
            text, parse_mode=ParseMode.HTML, reply_markup=keyboard
        )
    else:
        await target.reply_text(
            text, parse_mode=ParseMode.HTML, reply_markup=keyboard
        )


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    state = pending.get(user_id)
    if not state:
        return

    text = (update.message.text or "").strip()
    if not text:
        return

    action = state.get("action")

    if action == "link":
        # Accept API tokens only; no passwords.
        if len(text) < 20 or any(c.isspace() for c in text):
            await update.message.reply_text(
                "❌ That doesn't look like a valid API token."
            )
            return

        try:
            # Validate the supplied token directly before saving it.
            headers = dict(HEROKU_HEADERS)
            headers["Authorization"] = f"Bearer {text}"
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            ) as session:
                async with session.get(
                    HEROKU_API + "/account", headers=headers
                ) as r:
                    if r.status != 200:
                        raise ValueError
                    me = await r.json()
        except Exception:
            await update.message.reply_text(
                "❌ Token validation failed. Check the token and try again."
            )
            return

        save_account(
            user_id,
            text,
            me.get("id", ""),
            me.get("email", ""),
        )
        pending.pop(user_id, None)

        try:
            await update.message.delete()
        except Exception:
            pass

        await context.bot.send_message(
            user_id,
            "✅ <b>Heroku account linked successfully.</b>\n\n"
            f"Account: <code>{escape_html(me.get('email','Unknown'))}</code>\n"
            f"Heroku ID: <code>{escape_html(me.get('id','Unknown'))}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=main_keyboard(),
        )

    elif action == "deploy":
        pending.pop(user_id, None)
        await quick_deploy(user_id, text, update.message)

    elif action == "scale":
        app = state.get("app")
        if not re.fullmatch(r"[a-zA-Z0-9_-]+=\d{1,3}", text):
            await update.message.reply_text(
                "❌ Format: <code>web=1</code>",
                parse_mode=ParseMode.HTML
            )
            return
        proc, qty = text.split("=")
        try:
            await heroku_request(
                user_id, "PATCH", f"/apps/{quote(app)}/formation/{quote(proc)}",
                json={"quantity": int(qty)}
            )
            pending.pop(user_id, None)
            await update.message.reply_text(
                f"✅ Scaled <b>{proc}</b> to <b>{qty}</b> on <b>{app}</b>.",
                parse_mode=ParseMode.HTML,
                reply_markup=main_keyboard(),
            )
        except Exception:
            await update.message.reply_text("❌ Scaling failed.")


def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("link", link_cmd))
    app.add_handler(CommandHandler("unlink", unlink_cmd))
    app.add_handler(CommandHandler("apps", apps_cmd))
    app.add_handler(CommandHandler("deploy", deploy_cmd))
    app.add_handler(CommandHandler("settings", settings_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    log.info("Heroku Controller started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
