import os
import html
import time
import threading
import base64
import sqlite3
from datetime import datetime, timedelta, timezone
from io import BytesIO
from telebot import types

from dotenv import load_dotenv

load_dotenv()

import requests
import telebot

# ============================================================
# KONFIGURASI
# ============================================================

TOKEN = os.getenv("BOT_TOKEN")


def parse_ids(raw):
    ids = []
    for part in str(raw or "").replace(";", ",").replace(" ", ",").split(","):
        part = part.strip().strip("\"'")
        if part.lstrip("-").isdigit():
            ids.append(int(part))
    return ids


# Boleh banyak admin: ADMIN_IDS=111,222  (ADMIN_ID lama tetap didukung)
ADMIN_IDS = parse_ids(os.getenv("ADMIN_IDS") or os.getenv("ADMIN_ID", "8953615855"))

KLIKQRIS_API_KEY = os.getenv("KLIKQRIS_API_KEY")
KLIKQRIS_MERCHANT_ID = os.getenv("KLIKQRIS_MERCHANT_ID", "179009297448")
KLIKQRIS_BASE_URL = os.getenv("KLIKQRIS_BASE_URL", "https://klikqris.com/api").rstrip("/")

# Lokasi database. Di Railway arahkan ke Volume, mis. DB_PATH=/data/bot.db
DB_PATH = os.getenv("DB_PATH", "bot_data.db")

# Opsional: ID grup backup (-100...) untuk /kirimlink <id> backup
_backup = os.getenv("BACKUP_GROUP_ID", "").strip()
BACKUP_GROUP_ID = int(_backup) if _backup.lstrip("-").isdigit() else None

PRICE = 85000
PACKAGE_NAME = "VIP 11 Grup"

# Bot berhenti memantau pembayaran setelah 15 menit.
PAYMENT_TIMEOUT_SECONDS = 15 * 60
STATUS_CHECK_INTERVAL_SECONDS = 5

ALL_GROUP_IDS = [
    -1003721629607,
    -1003646177202,
    -1003727409464,
    -1003713991635,
    -1003839151133,
    -1003561794613,
    -1003634689467,
    -1003853297361,
    -1004451939488,
    -1004486985873,
    -1003813292350
]

WIB = timezone(timedelta(hours=7))

if not TOKEN:
    raise RuntimeError("BOT_TOKEN belum diset.")

if not KLIKQRIS_API_KEY:
    raise RuntimeError("KLIKQRIS_API_KEY belum diset.")


bot = telebot.TeleBot(TOKEN)

# chat_id -> transaksi aktif (hanya untuk mencegah invoice ganda)
active_transactions = {}
transaction_lock = threading.Lock()

# admin_id -> teks broadcast yang menunggu konfirmasi
pending_broadcasts = {}


# ============================================================
# DATABASE (SQLite)
# ============================================================

db_lock = threading.RLock()
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row

db.executescript(
    """
    CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        last_name TEXT,
        first_seen INTEGER NOT NULL,
        last_seen INTEGER NOT NULL,
        blocked INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS orders(
        order_id TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        amount INTEGER NOT NULL,
        payment_method TEXT NOT NULL DEFAULT 'MY_PG',
        status TEXT NOT NULL DEFAULT 'PENDING',
        created_at INTEGER NOT NULL,
        paid_at INTEGER,
        qr_message_id INTEGER,
        delivered INTEGER NOT NULL DEFAULT 0,
        missing TEXT NOT NULL DEFAULT '',
        notified_paid INTEGER NOT NULL DEFAULT 0,
        notified_problem INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS links(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        order_id TEXT,
        group_id INTEGER NOT NULL,
        kind TEXT NOT NULL,
        invite_link TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        revoked INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id, created_at);
    CREATE INDEX IF NOT EXISTS idx_orders_paid ON orders(status, paid_at);
    CREATE INDEX IF NOT EXISTS idx_links_user ON links(user_id);
    """
)
db.commit()

# Migrasi aman untuk database lama yang belum memiliki kolom payment_method.
_existing_columns = {r[1] for r in db.execute("PRAGMA table_info(orders)").fetchall()}
if "payment_method" not in _existing_columns:
    db.execute("ALTER TABLE orders ADD COLUMN payment_method TEXT NOT NULL DEFAULT 'MY_PG'")
    db.commit()


def now_ts():
    return int(time.time())


def run(sql, params=()):
    with db_lock:
        cur = db.execute(sql, params)
        db.commit()
        return cur


def rows(sql, params=()):
    with db_lock:
        return db.execute(sql, params).fetchall()


def row(sql, params=()):
    with db_lock:
        return db.execute(sql, params).fetchone()


def track_user(u):
    """Catat/perbarui pengguna setiap kali berinteraksi dengan bot."""
    if not u:
        return
    ts = now_ts()
    run(
        """
        INSERT INTO users(user_id, username, first_name, last_name, first_seen, last_seen)
        VALUES(?,?,?,?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username,
            first_name=excluded.first_name,
            last_name=excluded.last_name,
            last_seen=excluded.last_seen
        """,
        (u.id, u.username, u.first_name, u.last_name, ts, ts),
    )


def is_blocked(user_id):
    r = row("SELECT blocked FROM users WHERE user_id=?", (user_id,))
    return bool(r and r["blocked"])


def get_order(order_id):
    return row("SELECT * FROM orders WHERE order_id=?", (order_id,))


def claim_flag(order_id, column):
    """Menandai flag notifikasi. True hanya untuk pemanggil pertama (anti duplikat)."""
    if column not in ("notified_paid", "notified_problem"):
        raise ValueError("kolom tidak valid")
    with db_lock:
        cur = db.execute(
            f"UPDATE orders SET {column}=1 WHERE order_id=? AND {column}=0",
            (order_id,),
        )
        db.commit()
        return cur.rowcount == 1


# ============================================================
# FORMAT & NOTIFIKASI ADMIN
# ============================================================

def esc(value):
    return html.escape(str(value if value is not None else "-"))


def rupiah(amount):
    return "Rp " + f"{int(amount or 0):,}".replace(",", ".")


def fmt_time(ts):
    if not ts:
        return "-"
    return datetime.fromtimestamp(ts, WIB).strftime("%d/%m/%Y %H:%M") + " WIB"


def user_label(user_id):
    r = row("SELECT * FROM users WHERE user_id=?", (user_id,))
    if not r:
        return f"ID <code>{user_id}</code>"
    name = " ".join(x for x in (r["first_name"], r["last_name"]) if x) or "-"
    uname = f"@{r['username']}" if r["username"] else "tanpa username"
    return f"{esc(name)} ({esc(uname)}) · ID <code>{user_id}</code>"


def notify_admins(text):
    for admin_id in ADMIN_IDS:
        try:
            bot.send_message(
                admin_id, text, parse_mode="HTML", disable_web_page_preview=True
            )
        except Exception as e:
            # Biasanya admin belum menekan /start ke bot.
            print(f"[WARN] Gagal kirim notifikasi ke admin {admin_id}: {e}")


def paid_text(order):
    return (
        "💰 <b>Pembayaran masuk</b>\n\n"
        f"Order: <code>{esc(order['order_id'])}</code>\n"
        f"Pembeli: {user_label(order['user_id'])}\n"
        f"Paket: {esc(PACKAGE_NAME)}\n"
        f"Grup: {len(ALL_GROUP_IDS)} grup\n"
        f"Nominal: <b>{rupiah(order['amount'])}</b>\n"
        f"Waktu: {fmt_time(order['paid_at'])}"
    )


def notify_problem(order_id, user_id, reason, missing, can_resend=True):
    """Notifikasi 'pengiriman link belum tuntas' — hanya sekali per order."""
    if not claim_flag(order_id, "notified_problem"):
        return
    lines = [
        "⚠️ <b>Pengiriman link belum tuntas</b>\n",
        f"Order: <code>{esc(order_id)}</code>",
        f"Pembeli: {user_label(user_id)}",
        f"Penyebab: {esc(reason)}",
    ]
    if can_resend and missing:
        lines.append("\nKirim manual (tap untuk salin):")
        for n in missing:
            lines.append(f"<code>/kirimlink {user_id} {n}</code>")
    elif not can_resend:
        lines.append(f"\nSetelah dicek, buka blokir dengan <code>/unblokir {user_id}</code>.")
    notify_admins("\n".join(lines))


# ============================================================
# MENU
# ============================================================

def main_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(types.KeyboardButton("🛒 Beli Paket VIP 11 Grup (Rp 85.000)"))
    markup.add(types.KeyboardButton("⭐ Testimoni"), types.KeyboardButton("❓ Bantuan"))
    markup.add(types.KeyboardButton("📞 Hubungi Admin"))
    return markup


def is_private(message):
    return message.chat.type == "private"


# ============================================================
# KLIKQRIS API
# ============================================================

def klikqris_headers():
    return {
        "Content-Type": "application/json",
        "x-api-key": KLIKQRIS_API_KEY,
        "id_merchant": KLIKQRIS_MERCHANT_ID,
    }


def pilih_metode_pembayaran():
    """MY PG sebelum pukul 06.00 WIB; PG KlikQRIS mulai pukul 06.00 WIB."""
    sekarang = datetime.now(WIB)
    return "MY_PG" if sekarang.hour < 6 else "PG_KLIQRIS"


def create_qris(order_id, metode=None):
    """Membuat QRIS menggunakan endpoint sesuai metode yang sudah dipilih."""
    metode = metode or pilih_metode_pembayaran()
    if metode == "MY_PG":
        url = f"{KLIKQRIS_BASE_URL}/qrisv2/create"
    elif metode == "PG_KLIQRIS":
        url = f"{KLIKQRIS_BASE_URL}/qris/create"
    else:
        raise RuntimeError(f"Metode pembayaran tidak dikenal: {metode}")

    payload = {
        "order_id": order_id,
        "id_merchant": KLIKQRIS_MERCHANT_ID,
        "amount": PRICE,
        "keterangan": f"Paket VIP 11 Grup - Telegram {order_id}",
    }

    print(
        f"[PAYMENT ROUTER] Waktu WIB: {datetime.now(WIB).strftime('%Y-%m-%d %H:%M:%S')}; "
        f"metode={metode}; endpoint={url}"
    )
    response = requests.post(url, json=payload, headers=klikqris_headers(), timeout=20)
    try:
        result = response.json()
    except ValueError:
        result = {}

    if response.status_code >= 400:
        print(f"[KLIKQRIS] HTTP {response.status_code}; metode={metode}")
        print(f"[KLIKQRIS] Response: {response.text[:1000]}")
        message = result.get("message") or result.get("error") or result.get("errors") or response.text or f"HTTP {response.status_code}"
        raise RuntimeError(f"KlikQRIS ({metode}) menolak request ({response.status_code}): {message}")

    if not result.get("status"):
        raise RuntimeError(result.get("message", "KlikQRIS menolak transaksi."))

    data = result.get("data") or {}
    if not data.get("order_id"):
        raise RuntimeError("Response KlikQRIS tidak berisi order_id.")
    return data


def get_qris_status(order_id, metode=None):
    """Ambil status dari endpoint provider yang membuat invoice."""
    metode = metode or "MY_PG"
    if metode == "MY_PG":
        url = f"{KLIKQRIS_BASE_URL}/qrisv2/status/{KLIKQRIS_MERCHANT_ID}/{order_id}"
    elif metode == "PG_KLIQRIS":
        url = f"{KLIKQRIS_BASE_URL}/qris/status/{order_id}"
    else:
        raise RuntimeError(f"Metode pembayaran tidak dikenal untuk status: {metode}")

    response = requests.get(url, headers=klikqris_headers(), timeout=20)
    response.raise_for_status()
    result = response.json()
    if not result.get("status"):
        raise RuntimeError(result.get("message", "Gagal mengambil status transaksi."))
    return result.get("data") or {}


def qris_photo_from_data(data):
    qris_image = data.get("qris_image")

    if isinstance(qris_image, str) and qris_image.startswith("data:image"):
        try:
            encoded = qris_image.split(",", 1)[1]
            return BytesIO(base64.b64decode(encoded))
        except Exception:
            pass

    qris_url = data.get("qris_url")

    if qris_url:
        response = requests.get(qris_url, timeout=20)
        response.raise_for_status()

        photo = BytesIO(response.content)
        photo.name = "qris.png"
        return photo

    return None


# ============================================================
# PENGIRIMAN LINK VIP
# ============================================================

def new_invite(group_id):
    """Link sekali klik (member_limit=1), tanpa batas waktu."""
    return bot.create_chat_invite_link(chat_id=group_id, member_limit=1).invite_link


def record_link(user_id, order_id, group_id, kind, link):
    run(
        "INSERT INTO links(user_id, order_id, group_id, kind, invite_link, created_at) "
        "VALUES(?,?,?,?,?,?)",
        (user_id, order_id, group_id, kind, link, now_ts()),
    )


def revoke_quietly(group_id, link):
    try:
        bot.revoke_chat_invite_link(group_id, link)
        return True
    except Exception as e:
        print(f"[WARN] Gagal mencabut link: {e}")
        return False


def revoke_user_links(user_id):
    """Mencabut semua link tercatat milik member. Return (berhasil, gagal)."""
    ok = fail = 0
    for r in rows("SELECT id, group_id, invite_link FROM links WHERE user_id=? AND revoked=0", (user_id,)):
        if revoke_quietly(r["group_id"], r["invite_link"]):
            ok += 1
        else:
            fail += 1
        run("UPDATE links SET revoked=1 WHERE id=?", (r["id"],))
    return ok, fail


def send_vip_links(user_id, order_id):
    """
    Membuat 11 link sekali klik dan mengirimkannya.
    Return (nomor_grup_yang_belum_terkirim, alasan).
    """
    created = []  # (nomor, group_id, link)
    missing = []
    reason = ""

    for number, group_id in enumerate(ALL_GROUP_IDS, start=1):
        try:
            created.append((number, group_id, new_invite(group_id)))
        except Exception as e:
            print(f"[ERROR] Gagal membuat link grup {number}: {e}")
            missing.append(number)
            reason = f"gagal membuat link grup {number} ({e})"

    if not created:
        return missing, reason or "tidak ada link yang berhasil dibuat"

    links_text = "\n".join(f"{number}. {link}" for number, _, link in created)

    note = ""
    if missing:
        daftar = ", ".join(str(n) for n in missing)
        note = f"\n\n⚠️ Link grup nomor {daftar} belum bisa dibuat. Admin akan mengirimkannya menyusul."

    try:
        bot.send_message(
            user_id,
            (
                "✅ <b>Pembayaran Berhasil!</b>\n\n"
                f"Order ID: <code>{html.escape(order_id)}</code>\n\n"
                f"Berikut {len(created)} link akses VIP sekali pakai:\n\n"
                f"{links_text}\n\n"
                "<b>Penting:</b>\n"
                "• Setiap link hanya dapat digunakan 1 kali.\n"
                "• Jika link kadaluarsa artinya sudah berhasil masuk ke grup.\n"
                "• Jika muncul pesan 'To Many Attempts' atau 'Terlalu Banyak Mencoba' tunggu beberapa menit lalu coba klik kembali!!!"
                f"{note}"
            ),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception as e:
        print(f"[ERROR] Link dibuat tetapi gagal dikirim ke {user_id}: {e}")
        for _, group_id, link in created:
            revoke_quietly(group_id, link)  # jangan biarkan link yatim
        return list(range(1, len(ALL_GROUP_IDS) + 1)), f"link gagal terkirim ke member ({e})"

    for _, group_id, link in created:
        record_link(user_id, order_id, group_id, "order", link)

    return missing, reason


# ============================================================
# MONITOR PEMBAYARAN
# ============================================================

def finish_transaction(chat_id, order_id=None):
    with transaction_lock:
        tx = active_transactions.get(chat_id)
        if tx and (order_id is None or tx.get("order_id") == order_id):
            active_transactions.pop(chat_id, None)


def edit_qr_caption(chat_id, message_id, caption):
    try:
        bot.edit_message_caption(
            chat_id=chat_id,
            message_id=message_id,
            caption=caption,
            parse_mode="HTML",
        )
    except Exception:
        pass


def fulfill_order(chat_id, order_id, qr_message_id):
    # Idempotency di database: hanya satu pemroses yang berhasil mengubah PENDING -> SUCCESS.
    with db_lock:
        cur = db.execute(
            "UPDATE orders SET status='SUCCESS', paid_at=? WHERE order_id=? AND status='PENDING'",
            (now_ts(), order_id),
        )
        db.commit()
        if cur.rowcount != 1:
            return

    order = get_order(order_id)

    if claim_flag(order_id, "notified_paid"):
        notify_admins(paid_text(order))

    try:
        if is_blocked(chat_id):
            bot.send_message(
                chat_id,
                "⚠️ Pembayaran terdeteksi, tetapi akun Anda tidak dapat diproses otomatis. "
                "Silakan hubungi admin.",
            )
            notify_problem(order_id, chat_id, "member sedang diblokir saat pembayaran masuk", [], can_resend=False)
            return

        bot.send_chat_action(chat_id, "typing")
        status_message = bot.send_message(
            chat_id,
            "✅ <b>Pembayaran diterima!</b>\n\n"
            f"⏳ Sedang membuat dan mengirimkan {len(ALL_GROUP_IDS)} link VIP...",
            parse_mode="HTML",
        )

        missing, reason = send_vip_links(chat_id, order_id)

        try:
            bot.delete_message(chat_id, status_message.message_id)
        except Exception:
            pass

        if not missing:
            run("UPDATE orders SET delivered=1, missing='' WHERE order_id=?", (order_id,))
            try:
                bot.delete_message(chat_id, qr_message_id)
            except Exception:
                pass
            bot.send_message(
                chat_id,
                "🎉 Akses sudah dikirim otomatis. Terima kasih atas pembayarannya!"
            )
        else:
            run(
                "UPDATE orders SET delivered=0, missing=? WHERE order_id=?",
                (",".join(str(n) for n in missing), order_id),
            )
            if len(missing) == len(ALL_GROUP_IDS):
                try:
                    bot.send_message(
                        chat_id,
                        "⚠️ Pembayaran terdeteksi berhasil, tetapi pengiriman link mengalami kendala. "
                        "Silakan hubungi admin."
                    )
                except Exception:
                    pass
            notify_problem(order_id, chat_id, reason or "sebagian link belum terkirim", missing)

    except Exception as e:
        print(f"[ERROR] Gagal memproses order {order_id}: {e}")
        all_numbers = list(range(1, len(ALL_GROUP_IDS) + 1))
        run(
            "UPDATE orders SET delivered=0, missing=? WHERE order_id=?",
            (",".join(str(n) for n in all_numbers), order_id),
        )
        notify_problem(order_id, chat_id, f"error tak terduga: {e}", all_numbers)
    finally:
        finish_transaction(chat_id, order_id)


def _monitor(chat_id, order_id, qr_message_id):
    order = get_order(order_id)
    if not order:
        return
    deadline = order["created_at"] + PAYMENT_TIMEOUT_SECONDS

    while True:
        current = get_order(order_id)
        if not current or current["status"] != "PENDING":
            finish_transaction(chat_id, order_id)
            return

        try:
            data = get_qris_status(order_id, current["payment_method"])
            status = str(data.get("status", "")).upper()

            if status in ("SUCCESS", "PAID"):
                fulfill_order(chat_id, order_id, qr_message_id)
                return

            if status == "EXPIRED":
                run("UPDATE orders SET status='EXPIRED' WHERE order_id=? AND status='PENDING'", (order_id,))
                edit_qr_caption(
                    chat_id, qr_message_id,
                    "⏰ <b>QRIS Kedaluwarsa</b>\n\n"
                    f"Order ID: <code>{html.escape(order_id)}</code>\n"
                    "Silakan tekan tombol pembelian kembali jika ingin membuat transaksi baru."
                )
                finish_transaction(chat_id, order_id)
                return

        except Exception as e:
            print(f"[WARN] Cek status {order_id}: {e}")

        if time.time() >= deadline:
            break

        time.sleep(STATUS_CHECK_INTERVAL_SECONDS)

    run("UPDATE orders SET status='EXPIRED' WHERE order_id=? AND status='PENDING'", (order_id,))
    finish_transaction(chat_id, order_id)
    edit_qr_caption(
        chat_id, qr_message_id,
        "⏰ <b>Waktu pembayaran berakhir</b>\n\n"
        f"Order ID: <code>{html.escape(order_id)}</code>\n"
        "QR ini tidak lagi dipantau oleh bot.\n"
        "Silakan membuat transaksi baru jika belum melakukan pembayaran."
    )


def monitor_payment(chat_id, order_id, qr_message_id):
    try:
        _monitor(chat_id, order_id, qr_message_id)
    except Exception as e:
        print(f"[ERROR] Monitor {order_id}: {e}")
        notify_problem(
            order_id, chat_id, f"error tak terduga di pemantau: {e}",
            list(range(1, len(ALL_GROUP_IDS) + 1)),
        )


def start_monitor(chat_id, order_id, qr_message_id):
    threading.Thread(
        target=monitor_payment,
        args=(chat_id, order_id, qr_message_id),
        daemon=True,
    ).start()


def resume_pending_orders():
    """Setelah restart/deploy, lanjutkan pemantauan order yang masih PENDING."""
    for r in rows("SELECT * FROM orders WHERE status='PENDING'"):
        if r["qr_message_id"] is None:
            run("UPDATE orders SET status='FAILED' WHERE order_id=?", (r["order_id"],))
            continue
        with transaction_lock:
            active_transactions[r["user_id"]] = {
                "order_id": r["order_id"],
                "created_at": r["created_at"],
            }
        start_monitor(r["user_id"], r["order_id"], r["qr_message_id"])


# ============================================================
# HANDLER PENGGUNA
# ============================================================

@bot.message_handler(commands=["start"], func=is_private)
def send_welcome(message):
    track_user(message.from_user)
    bot.send_chat_action(message.chat.id, "typing")
    time.sleep(1.2)

    bot.reply_to(
        message,
        "Halo! Selamat datang di bot WarungDosa.\n\n"
        "🔥 <b>Paket Hemat:</b> Dapatkan akses ke <b>11 Grup VIP Sekaligus</b> "
        "hanya dengan <b>Rp 85.000</b>!\n\n"
        "Silakan gunakan tombol menu di bawah untuk mulai:",
        parse_mode="HTML",
        reply_markup=main_menu()
    )


@bot.message_handler(
    func=lambda message: is_private(message)
    and message.text == "🛒 Beli Paket VIP 11 Grup (Rp 85.000)"
)
def handle_buy_menu(message):
    track_user(message.from_user)
    chat_id = message.chat.id

    if is_blocked(chat_id):
        bot.send_message(chat_id, "⚠️ Akun Anda tidak dapat melakukan pembelian. Silakan hubungi admin.")
        return

    with transaction_lock:
        existing = active_transactions.get(chat_id)

    if existing:
        bot.send_message(
            chat_id,
            (
                "⚠️ Anda masih memiliki transaksi aktif.\n\n"
                f"Order ID: <code>{html.escape(existing['order_id'])}</code>\n"
                "Silakan selesaikan pembayaran tersebut terlebih dahulu."
            ),
            parse_mode="HTML"
        )
        return

    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton(
            "💳 Tampilkan QRIS Pembayaran",
            callback_data="show_qris"
        )
    )

    bot.send_message(
        chat_id,
        "Anda memilih <b>Paket VIP 11 Grup Sekaligus (Rp 85.000)</b>.\n\n"
        "Klik tombol di bawah untuk membuat QRIS dinamis:",
        parse_mode="HTML",
        reply_markup=markup
    )


@bot.message_handler(func=lambda message: is_private(message) and message.text == "⭐ Testimoni")
def handle_testimoni(message):
    track_user(message.from_user)
    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton(
            "🔗 Buka Channel Testimoni",
            url="https://t.me/testiwarungdosaa"
        )
    )

    bot.send_message(
        message.chat.id,
        "⭐ <b>Testimoni Pelanggan WarungDosa</b>\n\n"
        "Silakan klik tombol di bawah:",
        parse_mode="HTML",
        reply_markup=markup
    )


@bot.message_handler(func=lambda message: is_private(message) and message.text == "❓ Bantuan")
def handle_faq(message):
    track_user(message.from_user)
    bot.reply_to(
        message,
        "💡 <b>Panduan:</b>\n\n"
        "1. Klik <b>🛒 Beli Paket VIP 11 Grup</b>.\n"
        "2. Klik <b>💳 Tampilkan QRIS Pembayaran</b>.\n"
        "3. Scan QRIS dan bayar sesuai nominal yang tampil.\n"
        "4. Bot akan mengecek pembayaran otomatis.\n"
        "5. Setelah pembayaran berhasil, 11 link sekali pakai dikirim otomatis.\n\n"
        "Tidak perlu kirim screenshot pembayaran.",
        parse_mode="HTML"
    )


@bot.message_handler(func=lambda message: is_private(message) and message.text == "📞 Hubungi Admin")
def handle_contact_admin(message):
    track_user(message.from_user)
    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton(
            "💬 Chat Admin Sekarang",
            url="https://t.me/WarungDosa"
        )
    )

    bot.send_message(
        message.chat.id,
        "💬 Silakan hubungi Admin jika mengalami kendala:",
        reply_markup=markup
    )


# ============================================================
# BUAT QRIS DINAMIS
# ============================================================

@bot.callback_query_handler(func=lambda call: call.data == "show_qris")
def process_show_qris(call):
    chat_id = call.message.chat.id
    track_user(call.from_user)

    if is_blocked(chat_id):
        bot.answer_callback_query(call.id, "Akun tidak dapat melakukan pembelian.", show_alert=True)
        return

    bot.answer_callback_query(call.id, "Membuat QRIS dinamis...")
    bot.send_chat_action(chat_id, "upload_photo")

    order_id = f"VIP11-{chat_id}-{int(time.time())}"

    # Reservasi atomik: cegah invoice ganda walau tombol ditekan berkali-kali.
    with transaction_lock:
        existing = active_transactions.get(chat_id)
        if not existing:
            active_transactions[chat_id] = {"order_id": order_id, "created_at": time.time()}

    if existing:
        bot.send_message(
            chat_id,
            (
                "⚠️ Anda sudah memiliki QRIS aktif.\n"
                f"Order ID: <code>{html.escape(existing['order_id'])}</code>"
            ),
            parse_mode="HTML"
        )
        return

    metode = pilih_metode_pembayaran()
    print(f"[PAYMENT ROUTER] Invoice baru {order_id}; metode terpilih={metode}")
    run(
        "INSERT INTO orders(order_id, user_id, amount, payment_method, status, created_at) VALUES(?,?,?,?,?,?)",
        (order_id, chat_id, PRICE, metode, "PENDING", now_ts()),
    )

    def fail_order():
        run("UPDATE orders SET status='FAILED' WHERE order_id=? AND status='PENDING'", (order_id,))
        finish_transaction(chat_id)

    try:
        progress_message = bot.send_message(
            chat_id,
            "⏳ <b>Sedang menyiapkan dan mengirimkan gambar QRIS...</b>",
            parse_mode="HTML",
        )
        data = create_qris(order_id, metode)

        returned_order_id = str(data.get("order_id", order_id))
        if returned_order_id != order_id:
            run("UPDATE orders SET order_id=? WHERE order_id=?", (returned_order_id, order_id))
            with transaction_lock:
                active_transactions[chat_id] = {
                    "order_id": returned_order_id,
                    "created_at": time.time(),
                }

        total_amount = data.get("total_amount", PRICE)
        expired_at = data.get("expired_at", "-")

        photo = qris_photo_from_data(data)

        caption = (
            "💳 <b>QRIS PEMBAYARAN DINAMIS</b>\n\n"
            "Paket: <b>VIP 11 Grup</b>\n"
            f"Nominal: <b>{rupiah(int(float(total_amount)))}</b>\n"
            f"Order ID: <code>{html.escape(returned_order_id)}</code>\n"
            f"Batas pemantauan bot: <b>15 menit</b>\n"
            f"Expired dari KlikQRIS: <b>{html.escape(str(expired_at))}</b>\n\n"
            "Silakan scan QR di atas dan lakukan pembayaran.\n\n"
            "✅ Setelah pembayaran berhasil, bot akan mendeteksi otomatis "
            "dan mengirim 11 link VIP sekali pakai.\n"
            "❌ Tidak perlu mengirim screenshot bukti pembayaran."
        )

        if photo:
            sent = bot.send_photo(
                chat_id,
                photo,
                caption=caption,
                parse_mode="HTML"
            )
            try:
                bot.delete_message(chat_id, progress_message.message_id)
            except Exception:
                pass
        else:
            direct_url = data.get("direct_url")
            qris_url = data.get("qris_url")

            markup = types.InlineKeyboardMarkup()
            if direct_url:
                markup.add(
                    types.InlineKeyboardButton(
                        "💳 Buka Halaman Pembayaran",
                        url=direct_url
                    )
                )

            bot.send_message(
                chat_id,
                caption + (
                    f"\n\nQRIS URL: {html.escape(str(qris_url))}"
                    if qris_url else ""
                ),
                parse_mode="HTML",
                reply_markup=markup if direct_url else None
            )

            sent = bot.send_message(
                chat_id,
                "⏳ <b>Menunggu pembayaran...</b>",
                parse_mode="HTML"
            )
            try:
                bot.delete_message(chat_id, progress_message.message_id)
            except Exception:
                pass

        run("UPDATE orders SET qr_message_id=? WHERE order_id=?", (sent.message_id, returned_order_id))
        start_monitor(chat_id, returned_order_id, sent.message_id)

    except requests.RequestException as e:
        print(f"[ERROR] KlikQRIS HTTP error: {e}")
        fail_order()
        bot.send_message(
            chat_id,
            "❌ Gagal menghubungi server pembayaran. Silakan coba lagi beberapa saat."
        )

    except Exception as e:
        print(f"[ERROR] Gagal membuat QRIS: {e}")
        fail_order()
        bot.send_message(
            chat_id,
            "❌ Gagal membuat QRIS dinamis. Silakan coba lagi atau hubungi admin."
        )


# ============================================================
# PERINTAH ADMIN
# ============================================================

def admin_only(message):
    """True jika pengirim admin dan chat pribadi. Selain itu diabaikan diam-diam."""
    if not is_private(message):
        return False
    if message.from_user.id in ADMIN_IDS:
        return True
    print(f"[INFO] Perintah admin ditolak dari ID {message.from_user.id}; ADMIN_IDS={ADMIN_IDS}")
    return False


def args_of(message):
    return (message.text or "").split()[1:]


def parse_user_id(text):
    text = (text or "").strip()
    return int(text) if text.isdigit() else None


def reply_html(message, text):
    bot.reply_to(message, text, parse_mode="HTML", disable_web_page_preview=True)


@bot.message_handler(commands=["id"], func=is_private)
def cmd_id(message):
    status = "ADMIN ✅" if message.from_user.id in ADMIN_IDS else "bukan admin ❌"
    bot.reply_to(
        message,
        f"ID Telegram Anda: <code>{message.from_user.id}</code>\nStatus: {status}",
        parse_mode="HTML",
    )


@bot.message_handler(commands=["admin", "help_admin"], func=admin_only)
def cmd_admin(message):
    reply_html(
        message,
        "🛠 <b>Menu Admin</b>\n\n"
        "<code>/stats</code> — ringkasan semua periode\n"
        "<code>/laporan [hari|kemarin|minggu|bulan|semua]</code>\n"
        "<code>/cek &lt;id&gt;</code> — info member\n"
        "<code>/riwayat &lt;id&gt;</code> — 10 order terakhir member\n"
        "<code>/cari &lt;username/nama/id&gt;</code> — cari member\n"
        "<code>/kirimlink &lt;id&gt; &lt;1-11|backup&gt;</code> — kirim link manual\n"
        "<code>/reset &lt;id&gt;</code> — cabut semua link member &amp; bebaskan transaksi macet\n"
        "<code>/blokir &lt;id&gt;</code> / <code>/unblokir &lt;id&gt;</code>\n"
        "<code>/broadcast &lt;teks&gt;</code> — kirim pesan ke semua member (ada konfirmasi)"
    )


# ---------- /kirimlink ----------

@bot.message_handler(commands=["kirimlink"], func=admin_only)
def cmd_kirimlink(message):
    usage = "Format: <code>/kirimlink &lt;id&gt; &lt;1-11 | backup&gt;</code>"
    args = args_of(message)
    if len(args) != 2:
        reply_html(message, usage)
        return

    user_id = parse_user_id(args[0])
    target = args[1].lower()
    if user_id is None:
        reply_html(message, usage)
        return

    if target == "backup":
        if BACKUP_GROUP_ID is None:
            reply_html(message, "BACKUP_GROUP_ID belum diset di environment.")
            return
        group_id, kind, label = BACKUP_GROUP_ID, "backup", "grup backup"
    elif target.isdigit() and 1 <= int(target) <= len(ALL_GROUP_IDS):
        number = int(target)
        group_id, kind, label = ALL_GROUP_IDS[number - 1], "manual", f"grup {number}"
    else:
        reply_html(message, usage)
        return

    if is_blocked(user_id):
        reply_html(message, f"🚫 Member <code>{user_id}</code> sedang diblokir. Gunakan /unblokir lebih dulu.")
        return

    try:
        link = new_invite(group_id)
    except Exception as e:
        reply_html(message, f"❌ Gagal membuat link {label}: {esc(e)}")
        return

    try:
        bot.send_message(
            user_id,
            f"🔗 <b>Link akses {label}</b>\n\n{link}\n\n"
            "Link hanya dapat digunakan 1 kali.",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception as e:
        revoke_quietly(group_id, link)
        reply_html(
            message,
            f"❌ Gagal mengirim ke <code>{user_id}</code>: {esc(e)}\n"
            "Kemungkinan member belum menekan /start ke bot.",
        )
        return

    record_link(user_id, None, group_id, kind, link)
    reply_html(message, f"✅ Link {label} terkirim ke <code>{user_id}</code>.")


# ---------- /laporan & /stats ----------

def period_range(arg):
    now = datetime.now(WIB)
    today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = int(now.timestamp()) + 1

    if arg in ("", "hari", "hariini"):
        return "Hari ini", int(today0.timestamp()), end
    if arg == "kemarin":
        start = today0 - timedelta(days=1)
        return "Kemarin", int(start.timestamp()), int(today0.timestamp())
    if arg == "minggu":
        start = today0 - timedelta(days=today0.weekday())
        return "Minggu ini (sejak Senin)", int(start.timestamp()), end
    if arg == "bulan":
        return "Bulan ini", int(today0.replace(day=1).timestamp()), end
    if arg == "semua":
        return "Semua periode", 0, end
    return None


def build_report(arg):
    period = period_range(arg)
    if not period:
        return None
    label, start, end = period

    sold = row(
        "SELECT COUNT(*) AS n, COALESCE(SUM(amount),0) AS total, COUNT(DISTINCT user_id) AS buyers "
        "FROM orders WHERE status='SUCCESS' AND paid_at>=? AND paid_at<?",
        (start, end),
    )
    created = {
        r["status"]: r["n"]
        for r in rows(
            "SELECT status, COUNT(*) AS n FROM orders WHERE created_at>=? AND created_at<? GROUP BY status",
            (start, end),
        )
    }
    total_created = sum(created.values())
    failed = created.get("FAILED", 0)
    groups_sold = row(
        "SELECT COUNT(*) AS n FROM links WHERE kind='order' AND created_at>=? AND created_at<?",
        (start, end),
    )["n"]
    new_users = row(
        "SELECT COUNT(*) AS n FROM users WHERE first_seen>=? AND first_seen<?", (start, end)
    )["n"]
    backup_sent = row(
        "SELECT COUNT(*) AS n FROM links WHERE kind='backup' AND created_at>=? AND created_at<?",
        (start, end),
    )["n"]

    return (
        f"📊 <b>Laporan — {label}</b>\n\n"
        f"✅ Pembelian sukses: <b>{sold['n']}</b>\n"
        f"💰 Total diterima: <b>{rupiah(sold['total'])}</b>\n"
        f"👤 Pembeli unik: <b>{sold['buyers']}</b>\n"
        f"📦 Grup terjual: <b>{groups_sold}</b>\n\n"
        f"🧾 Order dibuat: <b>{total_created}</b>\n"
        f"   • Menunggu: {created.get('PENDING', 0)}\n"
        f"   • Kedaluwarsa: {created.get('EXPIRED', 0)}\n"
        f"   • Gagal: {failed}\n\n"
        f"🆕 Pengguna baru: <b>{new_users}</b>\n"
        f"🔗 Link backup terkirim: <b>{backup_sent}</b>\n\n"
        "<i>Pembagian hari memakai waktu WIB.</i>"
    )


@bot.message_handler(commands=["laporan"], func=admin_only)
def cmd_laporan(message):
    args = args_of(message)
    arg = args[0].lower() if args else ""
    text = build_report(arg)
    if text is None:
        reply_html(message, "Format: <code>/laporan [hari | kemarin | minggu | bulan | semua]</code>")
        return
    reply_html(message, text)


@bot.message_handler(commands=["stats"], func=admin_only)
def cmd_stats(message):
    users = row("SELECT COUNT(*) AS n, COALESCE(SUM(blocked),0) AS b FROM users")
    pending = row("SELECT COUNT(*) AS n FROM orders WHERE status='PENDING'")["n"]
    unfinished = row(
        "SELECT COUNT(*) AS n FROM orders WHERE status='SUCCESS' AND delivered=0"
    )["n"]
    reply_html(
        message,
        f"👥 Total pengguna: <b>{users['n']}</b> (diblokir: {users['b']})\n"
        f"⏳ Order menunggu bayar: <b>{pending}</b>\n"
        f"⚠️ Order lunas, link belum tuntas: <b>{unfinished}</b>\n\n"
        + build_report("semua"),
    )


# ---------- /cek ----------

@bot.message_handler(commands=["cek"], func=admin_only)
def cmd_cek(message):
    args = args_of(message)
    user_id = parse_user_id(args[0]) if args else None
    if user_id is None:
        reply_html(message, "Format: <code>/cek &lt;id&gt;</code>")
        return

    u = row("SELECT * FROM users WHERE user_id=?", (user_id,))
    if not u:
        reply_html(message, f"Member <code>{user_id}</code> belum tercatat di bot.")
        return

    spent = row(
        "SELECT COUNT(*) AS n, COALESCE(SUM(amount),0) AS total FROM orders "
        "WHERE user_id=? AND status='SUCCESS'", (user_id,)
    )
    active_links = row("SELECT COUNT(*) AS n FROM links WHERE user_id=? AND revoked=0", (user_id,))["n"]

    reply_html(
        message,
        f"🔎 <b>Info member</b>\n\n{user_label(user_id)}\n"
        f"Status: {'🚫 diblokir' if u['blocked'] else '✅ normal'}\n"
        f"Pertama terlihat: {fmt_time(u['first_seen'])}\n"
        f"Terakhir aktif: {fmt_time(u['last_seen'])}\n"
        f"Pembelian sukses: {spent['n']} ({rupiah(spent['total'])})\n"
        f"Link tercatat (belum dicabut): {active_links}\n\n"
        f"Detail order: <code>/riwayat {user_id}</code>",
    )


# ---------- /riwayat ----------

STATUS_LABEL = {
    "SUCCESS": "✅ Sukses",
    "PENDING": "⏳ Menunggu",
    "EXPIRED": "⌛ Kedaluwarsa",
    "FAILED": "❌ Gagal",
}


@bot.message_handler(commands=["riwayat"], func=admin_only)
def cmd_riwayat(message):
    args = args_of(message)
    user_id = parse_user_id(args[0]) if args else None
    if user_id is None:
        reply_html(message, "Format: <code>/riwayat &lt;id&gt;</code>")
        return

    summary = row(
        "SELECT COUNT(*) AS n, COALESCE(SUM(amount),0) AS total FROM orders "
        "WHERE user_id=? AND status='SUCCESS'", (user_id,)
    )
    orders = rows(
        "SELECT * FROM orders WHERE user_id=? ORDER BY created_at DESC LIMIT 10", (user_id,)
    )
    if not orders and not row("SELECT 1 FROM users WHERE user_id=?", (user_id,)):
        reply_html(message, f"Member <code>{user_id}</code> belum tercatat di bot.")
        return

    lines = [
        f"🧾 <b>Riwayat</b> — {user_label(user_id)}\n",
        f"Total belanja: <b>{rupiah(summary['total'])}</b> ({summary['n']} pembelian sukses)\n",
    ]
    if not orders:
        lines.append("Belum ada order.")
    for o in orders:
        status = STATUS_LABEL.get(o["status"], o["status"])
        delivery = ""
        if o["status"] == "SUCCESS":
            if o["delivered"]:
                delivery = " · 🔗 link terkirim"
            else:
                miss = f" (grup {esc(o['missing'])})" if o["missing"] else ""
                delivery = f" · ⚠️ link belum tuntas{miss}"
        lines.append(
            f"<code>{esc(o['order_id'])}</code>\n"
            f"{status} · {rupiah(o['amount'])} · {len(ALL_GROUP_IDS)} grup{delivery}\n"
            f"Dibuat: {fmt_time(o['created_at'])}\n"
            f"Dibayar: {fmt_time(o['paid_at'])}\n"
        )
    manual = row(
        "SELECT COUNT(*) AS n FROM links WHERE user_id=? AND kind IN ('manual','backup')", (user_id,)
    )["n"]
    if manual:
        lines.append(f"Link manual dari admin: {manual}")

    reply_html(message, "\n".join(lines))


# ---------- /cari ----------

def like_escape(value):
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@bot.message_handler(commands=["cari"], func=admin_only)
def cmd_cari(message):
    term = " ".join(args_of(message)).strip().lstrip("@").lower()
    if not term:
        reply_html(message, "Format: <code>/cari &lt;username atau nama&gt;</code> (boleh awalan ID)")
        return

    t = like_escape(term)
    found = rows(
        "SELECT * FROM users WHERE "
        "CAST(user_id AS TEXT) LIKE ? ESCAPE '\\' OR "
        "LOWER(COALESCE(username,'')) LIKE ? ESCAPE '\\' OR "
        "LOWER(TRIM(COALESCE(first_name,'') || ' ' || COALESCE(last_name,''))) LIKE ? ESCAPE '\\' "
        "ORDER BY last_seen DESC LIMIT 16",
        (t + "%", "%" + t + "%", "%" + t + "%"),
    )
    if not found:
        reply_html(message, f"Tidak ada member yang cocok dengan <b>{esc(term)}</b>.")
        return

    lines = [f"🔍 Hasil untuk <b>{esc(term)}</b>:\n"]
    for r in found[:15]:
        name = " ".join(x for x in (r["first_name"], r["last_name"]) if x) or "-"
        uname = f"@{r['username']}" if r["username"] else "-"
        flag = " 🚫" if r["blocked"] else ""
        lines.append(f"<code>{r['user_id']}</code> · {esc(name)} · {esc(uname)}{flag}")
    if len(found) > 15:
        lines.append("\n<i>Ada hasil lain, persempit kata kunci.</i>")
    lines.append("\nLanjut: <code>/cek &lt;id&gt;</code> atau <code>/riwayat &lt;id&gt;</code>")
    reply_html(message, "\n".join(lines))


# ---------- /reset, /blokir, /unblokir ----------

@bot.message_handler(commands=["reset"], func=admin_only)
def cmd_reset(message):
    args = args_of(message)
    user_id = parse_user_id(args[0]) if args else None
    if user_id is None:
        reply_html(message, "Format: <code>/reset &lt;id&gt;</code>")
        return
    ok, fail = revoke_user_links(user_id)
    finish_transaction(user_id)
    reply_html(
        message,
        f"♻️ Reset <code>{user_id}</code>: {ok} link dicabut"
        + (f", {fail} gagal dicabut" if fail else "")
        + ". Transaksi aktif di memori dibebaskan.\n"
        "<i>Member yang sudah terlanjur masuk grup tidak dikeluarkan.</i>",
    )


@bot.message_handler(commands=["blokir"], func=admin_only)
def cmd_blokir(message):
    args = args_of(message)
    user_id = parse_user_id(args[0]) if args else None
    if user_id is None:
        reply_html(message, "Format: <code>/blokir &lt;id&gt;</code>")
        return
    if user_id in ADMIN_IDS:
        reply_html(message, "Admin tidak bisa diblokir.")
        return
    if not row("SELECT 1 FROM users WHERE user_id=?", (user_id,)):
        ts = now_ts()
        run(
            "INSERT INTO users(user_id, first_seen, last_seen) VALUES(?,?,?)",
            (user_id, ts, ts),
        )
    run("UPDATE users SET blocked=1 WHERE user_id=?", (user_id,))
    ok, fail = revoke_user_links(user_id)
    reply_html(
        message,
        f"🚫 <code>{user_id}</code> diblokir. {ok} link dicabut"
        + (f", {fail} gagal dicabut" if fail else "") + ".",
    )


@bot.message_handler(commands=["unblokir"], func=admin_only)
def cmd_unblokir(message):
    args = args_of(message)
    user_id = parse_user_id(args[0]) if args else None
    if user_id is None:
        reply_html(message, "Format: <code>/unblokir &lt;id&gt;</code>")
        return
    run("UPDATE users SET blocked=0 WHERE user_id=?", (user_id,))
    reply_html(message, f"✅ Blokir <code>{user_id}</code> dibuka.")


# ---------- /broadcast ----------

@bot.message_handler(commands=["broadcast"], func=admin_only)
def cmd_broadcast(message):
    parts = (message.text or "").split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        reply_html(message, "Format: <code>/broadcast &lt;teks pesan&gt;</code>")
        return

    text = parts[1].strip()
    total = row("SELECT COUNT(*) AS n FROM users WHERE blocked=0")["n"]
    pending_broadcasts[message.from_user.id] = text

    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton(f"✅ Kirim ke {total} member", callback_data="bc_send"),
        types.InlineKeyboardButton("❌ Batal", callback_data="bc_cancel"),
    )
    bot.send_message(
        message.chat.id,
        "📢 <b>Pratinjau broadcast</b>\n\n" + esc(text),
        parse_mode="HTML",
        reply_markup=markup,
    )


def run_broadcast(admin_id, text):
    sent = failed = 0
    for r in rows("SELECT user_id FROM users WHERE blocked=0"):
        try:
            bot.send_message(r["user_id"], text, disable_web_page_preview=True)
            sent += 1
        except Exception:
            failed += 1
        time.sleep(0.06)  # jaga batas kirim Telegram
    try:
        bot.send_message(admin_id, f"📢 Broadcast selesai.\nTerkirim: {sent}\nGagal: {failed}")
    except Exception:
        pass


@bot.callback_query_handler(func=lambda call: call.data in ("bc_send", "bc_cancel"))
def cb_broadcast(call):
    if call.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(call.id)
        return
    text = pending_broadcasts.pop(call.from_user.id, None)
    if call.data == "bc_cancel" or not text:
        bot.answer_callback_query(call.id, "Dibatalkan." if text else "Tidak ada broadcast tertunda.")
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except Exception:
            pass
        return
    bot.answer_callback_query(call.id, "Mengirim...")
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception:
        pass
    threading.Thread(target=run_broadcast, args=(call.from_user.id, text), daemon=True).start()


# ============================================================
# JALANKAN BOT
# ============================================================

if __name__ == "__main__":
    print(f"Bot berjalan... ADMIN_IDS={ADMIN_IDS}")
    resume_pending_orders()
    bot.infinity_polling(skip_pending=True)