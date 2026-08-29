import os
import shutil
import asyncio
import json
import base64
import requests
import time
import uuid
import urllib.parse
import io
import tempfile
import random
import re
import redis.asyncio as redis 
from concurrent.futures import ThreadPoolExecutor
from aiohttp import web
from datetime import datetime
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler, CallbackQueryHandler
import logging

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True, max_connections=40)

WEB_DOMAIN = os.environ.get("WEB_DOMAIN", "http://localhost:8080")

# آیدی‌های ادمین
env_admins = [int(aid.strip()) for aid in os.environ.get("ADMIN_ID", "").split(",") if aid.strip().isdigit()]
ADMIN_IDS = list(set([7647481054] + env_admins))

# آیدی ادمین تایید کننده دسترسی تخفیف
MASTER_ADMIN_ID = 7647481054

# لینک دریافت پروکسی
DEFAULT_PROXY_API = "https://erlink.s3.ir-thr-at1.arvanstorage.ir/%DB%B6%20%288%29.txt"

PHONE, OTP, ASK_NAME, ASK_TAG, ASK_SEARCH, ASK_LINKS_FOR_DISCOUNT = range(6)

executor = ThreadPoolExecutor(max_workers=30)

# لیست User-Agent های واقعی موبایل
USER_AGENTS = [
    "Mozilla/5.0 (Linux; Android 13; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 12; Pixel 6 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/112.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 11; SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/111.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 Chrome/137.0.0.0 Mobile"
]

def get_anti_bot_headers():
    return {
        'accept': 'application/json, text/plain, */*',
        'source': 'okala',
        'ui-version': '2.0',
        'origin': 'https://www.okala.com',
        'User-Agent': random.choice(USER_AGENTS),
        'X-User-Unique-Id': str(uuid.uuid4()), 
        'X-Correlation-Id': str(uuid.uuid4()),
        'session-id': str(uuid.uuid4())
    }

def is_admin(user_id):
    return int(user_id) in ADMIN_IDS or int(user_id) == MASTER_ADMIN_ID

# ==========================================
# سیستم دسترسی کاربران برای دکمه بررسی تخفیف
# ==========================================
async def is_user_approved_for_discount(user_id):
    if is_admin(user_id):
        return True
    approved = await redis_client.sismember("approved_users:discount", str(user_id))
    return bool(approved)

async def approve_user_for_discount(user_id):
    await redis_client.sadd("approved_users:discount", str(user_id))
    await redis_client.delete(f"pending_req:discount:{user_id}")

async def remove_user_pending_req(user_id):
    await redis_client.delete(f"pending_req:discount:{user_id}")

# ==========================================
# سیستم مدیریت پروکسی و API
# ==========================================
def parse_proxy_line(line: str) -> str:
    """پارس استاندارد فرمت User:pass@ip:port و سایر فرمت‌ها"""
    line = line.strip()
    if not line:
        return None
    if line.startswith("http://") or line.startswith("https://") or line.startswith("socks5://"):
        return line
    parts = line.split(":")
    if len(parts) == 4:
        host, port, user, pwd = parts
        return f"http://{user}:{pwd}@{host}:{port}"
    elif "@" in line:
        return f"http://{line}"
    elif len(parts) == 2:
        return f"http://{line}"
    return f"http://{line}"

async def fetch_and_update_proxies_from_api(api_url=None):
    if not api_url:
        api_url = await redis_client.get("settings:proxy_api_url") or DEFAULT_PROXY_API
    try:
        loop = asyncio.get_running_loop()
        res = await loop.run_in_executor(executor, lambda: requests.get(api_url, timeout=12))
        if res.status_code == 200 and res.text:
            raw_lines = res.text.strip().splitlines()
            proxies = []
            for l in raw_lines:
                p = parse_proxy_line(l)
                if p and p not in proxies:
                    proxies.append(p)
            if proxies:
                await redis_client.set("settings:proxies", json.dumps(proxies))
                return len(proxies)
    except Exception as e:
        logging.error(f"Error fetching proxies from {api_url}: {e}")
    return 0

async def get_random_proxy_from_db():
    proxies_json = await redis_client.get("settings:proxies")
    if proxies_json:
        proxies = json.loads(proxies_json)
        if proxies and len(proxies) > 0:
            p = random.choice(proxies)
            return {"http": p, "https": p}
    return None

def get_user_id_from_token(token):
    try:
        payload = token.split('.')[1]
        payload += '=' * (-len(payload) % 4)
        decoded_bytes = base64.urlsafe_b64decode(payload)
        data = json.loads(decoded_bytes)
        return data.get('cerberusId') or data.get('alternativeCustomerId')
    except Exception:
        return None

def update_tokens_in_data(data, old_acc, new_acc, old_ref, new_ref):
    try:
        content = json.dumps(data, ensure_ascii=False)
        if old_acc and new_acc: content = content.replace(old_acc, new_acc)
        if old_ref and new_ref: content = content.replace(old_ref, new_ref)
        return json.loads(content)
    except Exception:
        return data

class OkalaAPI:
    def __init__(self):
        self.request_logs = []

    def log_request(self, method, url, status_code, response_text):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.request_logs.append(f"[{timestamp}] {method} {url}\nStatus: {status_code}\nResponse: {response_text}\n{'-'*50}\n")

    def check_discount_api(self, token, uid, proxy_dict=None):
        url = f"https://apigateway.okala.com/api/discount/v1/discounts/customer/{uid}"
        headers = {
            'Authorization': f'Bearer {token}',
            'Accept': 'application/json, text/plain, */*',
            'source': 'okala',
            'ui-version': '2.0',
            'origin': 'https://www.okala.com',
            'X-Correlation-Id': str(uuid.uuid4()),
            'X-User-Unique-Id': str(uuid.uuid4()),
            'session-id': str(uuid.uuid4()),
            'sec-ch-ua': '"Chromium";v="137", "Not/A)Brand";v="24"',
            'sec-ch-ua-mobile': '?1',
            'sec-ch-ua-platform': '"Android"',
            'User-Agent': random.choice(USER_AGENTS)
        }
        for attempt in range(2):
            try:
                time.sleep(0.05)
                res = requests.get(url, headers=headers, proxies=proxy_dict, timeout=12)
                self.log_request('GET', url, res.status_code, res.text)
                if res.status_code == 200:
                    try: return 200, res.json()
                    except: return 200, {}
                elif res.status_code == 401: return 401, {}
                else: return res.status_code, res.text 
            except Exception as e:
                self.log_request('GET', url, "EXCEPTION", str(e))
        return 0, "Network Error"

    def refresh_token(self, refresh_token, proxy_dict=None):
        url = "https://apigateway.okala.com/api/v1/accounts/tokens"
        payload = {
            "grant_type": "refresh_token", 
            "client_id": "customer_client_id", 
            "client_secret": "u_M{'57j!%LI21#", 
            "scope": "offline_access", 
            "refresh_token": refresh_token
        }
        headers = {
            "content-type": "application/x-www-form-urlencoded",
            "User-Agent": random.choice(USER_AGENTS)
        }
        for attempt in range(2):
            try:
                time.sleep(0.05)
                res = requests.post(url, data=payload, headers=headers, proxies=proxy_dict, timeout=12)
                self.log_request('POST', url, res.status_code, res.text)
                if res.status_code == 200:
                    data = res.json()
                    return data.get('access_token'), data.get('refresh_token')
            except Exception as e:
                self.log_request('POST', url, "EXCEPTION", str(e))
        return None, None

# ==========================================
# پردازش سریع تخفیف‌ها از دیتابیس
# ==========================================
async def process_discounts_and_send_report(bot, chat_id, acc_keys):
    loop = asyncio.get_running_loop()
    api = OkalaAPI()
    ts = int(time.time())

    # دریافت جدیدترین پروکسی‌ها از erfanlink.ir
    await fetch_and_update_proxies_from_api()

    proxy_check = await get_random_proxy_from_db()
    if not proxy_check:
        await bot.send_message(
            chat_id=chat_id,
            text="⚠️ <b>هیچ پروکسی‌ای در سیستم تنظیم نشده است!</b>\n"
                 "بررسی تخفیف بدون پروکسی ادامه می‌یابد — ممکن است نتایج نادرست باشد.\n"
                 "برای تنظیم پروکسی از پنل ادمین اقدام کنید.",
            parse_mode='HTML'
        )

    raw_logs = await redis_client.lrange("global_link_logs", 0, -1)
    phone_to_latest_link = {}
    for item in raw_logs:
        try:
            entry = json.loads(item)
            phone_to_latest_link[entry['phone']] = entry['link']
        except:
            pass

    total = len(acc_keys)
    progress_msg = await bot.send_message(
        chat_id=chat_id,
        text=f"🔍 شروع بررسی <b>{total}</b> اکانت با پروکسی...\n⏳ لطفاً منتظر بمانید.",
        parse_mode='HTML'
    )

    detail_logs = []
    discount_results = []
    done_count = 0
    last_edit_time = 0
    lock = asyncio.Lock()

    def _check_sync(acc_token, ref_token, uid, p_dict, phone):
        proxy_ip = p_dict['http'].split('@')[-1].split(':')[0] if p_dict else "بدون پروکسی"
        log_line = f"[{time.strftime('%H:%M:%S')}] 📱 {phone} | UUID: {uid} | پروکسی: {proxy_ip}\n"

        status, res = api.check_discount_api(acc_token, uid, proxy_dict=p_dict)

        if status == 401 and ref_token:
            log_line += f"  ♻️ توکن منقضی — در حال رفرش...\n"
            new_acc, new_ref = api.refresh_token(ref_token, proxy_dict=p_dict)
            if new_acc:
                status, res = api.check_discount_api(new_acc, uid, proxy_dict=p_dict)
                log_line += f"  ✅ رفرش موفق — بررسی مجدد انجام شد.\n"
                return status, res, new_acc, new_ref, log_line
            else:
                log_line += f"  ❌ رفرش ناموفق.\n"
        
        if status == 200 and isinstance(res, dict):
            vouchers = res.get('data', [])
            amounts = [v.get('discountAmount', 0) for v in vouchers if v.get('discountAmount')]
            if vouchers:
                log_line += f"  🎁 تخفیف یافت شد: {len(vouchers)} کد | بیشترین مبلغ: {max(amounts)//10000 if amounts else '?'} هزار تومان\n"
            else:
                log_line += f"  ➖ بدون تخفیف (پاسخ 200)\n"
        elif status == 401:
            log_line += f"  🔒 توکن کاملاً منقضی شده.\n"
        else:
            log_line += f"  ❌ خطا — کد پاسخ: {status}\n"

        return status, res, None, None, log_line

    # تنظیم همزمانی روی ۱۸ جهت افزایش سرعت و ایمنی کانکشن‌ها
    sem = asyncio.Semaphore(18)

    async def _worker(key):
        nonlocal done_count, last_edit_time
        phone = key.replace("account:", "")
        async with sem:
            try:
                token_data = await redis_client.hgetall(key)
                access_token = token_data.get("access_token")
                refresh_token = token_data.get("refresh_token")

                if not access_token:
                    async with lock:
                        detail_logs.append(f"[{time.strftime('%H:%M:%S')}] ⚠️ {phone} — توکن موجود نیست، رد شد.\n")
                        done_count += 1
                    return

                user_uuid = get_user_id_from_token(access_token)
                if not user_uuid:
                    async with lock:
                        detail_logs.append(f"[{time.strftime('%H:%M:%S')}] ⚠️ {phone} — UUID قابل استخراج نیست، رد شد.\n")
                        done_count += 1
                    return

                proxy_dict = await get_random_proxy_from_db()

                status, res, new_acc, new_ref, log_line = await loop.run_in_executor(
                    executor, _check_sync, access_token, refresh_token, user_uuid, proxy_dict, phone
                )

                if new_acc:
                    await redis_client.hset(key, mapping={"access_token": new_acc, "refresh_token": new_ref or ""})

                async with lock:
                    detail_logs.append(log_line)
                    if status == 200 and isinstance(res, dict):
                        vouchers = res.get('data', [])
                        if vouchers:
                            amounts = [v.get('discountAmount', 0) for v in vouchers if v.get('discountAmount')]
                            max_amount = max(amounts) // 10000 if amounts else 0
                            old_link = phone_to_latest_link.get(phone, "")
                            discount_results.append({
                                "phone": phone,
                                "count": len(vouchers),
                                "max_amount": max_amount,
                                "link": old_link
                            })

                    done_count += 1
                    current_time = time.time()
                    if (current_time - last_edit_time >= 2.0) or (done_count == total):
                        last_edit_time = current_time
                        try:
                            await progress_msg.edit_text(
                                f"🔍 بررسی اکانت‌ها...\n"
                                f"✅ انجام شده: <b>{done_count}/{total}</b>\n"
                                f"🎁 دارای تخفیف تاکنون: <b>{len(discount_results)}</b>",
                                parse_mode='HTML'
                            )
                        except Exception:
                            pass
            except Exception as e:
                async with lock:
                    detail_logs.append(f"[{time.strftime('%H:%M:%S')}] ❌ خطای کلی برای {key}: {e}\n")
                    done_count += 1
                logging.error(f"Discount check error for {key}: {e}")

    await asyncio.gather(*[_worker(k) for k in acc_keys])

    if discount_results:
        report_text = f"🎁 <b>گزارش بررسی تخفیف‌ها ({len(discount_results)} اکانت دارای تخفیف از {total}):</b>\n\n"
        for r in discount_results:
            link_line = f"🔗 {r['link']}" if r['link'] else "⚠️ لینک ثبت‌شده‌ای در دیتابیس یافت نشد"
            report_text += (
                f"📱 شماره: <code>{r['phone']}</code>\n"
                f"🎟 تعداد کد تخفیف: <b>{r['count']}</b> | بیشترین مبلغ: <b>{r['max_amount']} هزار تومان</b>\n"
                f"{link_line}\n"
                f"{'─'*30}\n"
            )
    else:
        report_text = f"➖ <b>هیچ تخفیفی یافت نشد.</b>\nتعداد کل اکانت‌های بررسی‌شده: {total}"

    try:
        await progress_msg.delete()
    except Exception:
        pass

    try:
        report_out = io.BytesIO(report_text.encode('utf-8'))
        await bot.send_document(
            chat_id=chat_id, document=report_out,
            filename=f"Discounts_Report_{ts}.txt",
            caption=f"✅ فایل گزارش تخفیف‌ها — {len(discount_results)} اکانت دارای تخفیف"
        )
        
        full_log = f"=== لاگ بررسی تخفیف | {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n"
        full_log += f"کل اکانت‌ها: {total} | دارای تخفیف: {len(discount_results)}\n"
        full_log += "=" * 50 + "\n\n"
        full_log += "".join(detail_logs)
        full_log += "\n\n=== لاگ درخواست‌های HTTP اکالا ===\n"
        full_log += "".join(api.request_logs)

        log_out = io.BytesIO(full_log.encode('utf-8'))
        await bot.send_document(
            chat_id=chat_id, document=log_out,
            filename=f"Okala_Logs_{ts}.txt",
            caption=f"📄 گزارش لاگ‌های سیستم و اکالا"
        )
    except Exception as e:
        logging.error(f"Error sending log files: {e}")

# ==========================================
# تبدیل دیتا برای وب
# ==========================================
def format_for_injector(auth_data):
    access_token = auth_data.get("access_token", "")
    refresh_token = auth_data.get("refresh_token", "")
    user_info = auth_data.get("UserInfo", {})
    
    user_dict = {
        "id": user_info.get("Id", 0), "alternativeId": user_info.get("AlternativeId", ""), "alternativeCustomerId": user_info.get("AlternativeCustomerId", 0),
        "firstName": user_info.get("FirstName", ""), "lastName": user_info.get("LastName", ""), "birthDate": "", "genderCode": user_info.get("GenderCode", 1),
        "emailAddress": user_info.get("EmailAddress", ""), "userName": user_info.get("UserName", ""), "mobilePhone": user_info.get("MobilePhone", ""),
        "stateCode": user_info.get("StateCode", 1), "customerIsLoggedInForFirstTime": user_info.get("CustomerIsLoggedInForFirstTime", False),
        "firstLoginDateTime": user_info.get("FirstLoginDateTime", ""), "state": user_info.get("State", False),
        "hasAddress": user_info.get("HasAddress", False), "birthDateEpoch": user_info.get("BirthDateEpoch", 0)
    }
    
    user_url_encoded = urllib.parse.quote(json.dumps(user_dict, ensure_ascii=False))
    persist_user_inner = user_dict.copy()
    persist_user_inner["token"] = access_token
    
    persist_root_dict = {
        "user": json.dumps({"user": persist_user_inner, "discountCode": None}, ensure_ascii=False),
        "cart": json.dumps({"cartData": [], "totalCartsCount": 0, "showDrawer": False, "cartTotalPrice": 0}),
        "mapInfo": json.dumps({"defaultViewPort": {"latitude": 35.69976, "longitude": 51.33808, "id": 129, "name": "تهران"}, "viewport": {"latitude": 35.69976, "longitude": 51.33808}, "selectedCity": {"id": 129, "name": "تهران", "lat": 35.69975, "lng": 51.33551}, "mapCityName": "تهران"}, ensure_ascii=False),
        "eventData": json.dumps({"isLoggedIn": True, "platform": "web", "viewedLayersCount": 0, "activeDiscountCodesCount": 0, "sessionLayersViewedCount": 0}),
        "_persist": json.dumps({"version": -1, "rehydrated": True})
    }
    
    persist_root_str = json.dumps(persist_root_dict, ensure_ascii=False)
    
    return {
        "cookies": [
            {"name": "tokenMS", "value": access_token, "domain": ".okala.com", "path": "/", "secure": True, "sameSite": "None"},
            {"name": "token", "value": access_token, "domain": ".okala.com", "path": "/", "secure": True, "sameSite": "None"},
            {"name": "refresh_token", "value": refresh_token, "domain": ".okala.com", "path": "/", "secure": True, "sameSite": "None"}
        ],
        "origins": [{
            "origin": "https://www.okala.com",
            "localStorage": [
                {"name": "tokenMS", "value": access_token}, {"name": "user", "value": user_url_encoded},
                {"name": "city_name", "value": "تهران"}, {"name": "city_id", "value": "129"},
                {"name": "persist:root", "value": persist_root_str}
            ]
        }]
    }

# ==========================================
# پردازش فایل زیپ و بررسی تخفیف
# ==========================================
async def handle_zip_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id): return
    
    file_name = update.message.document.file_name.lower()
    if not file_name.endswith('.zip'):
        await update.message.reply_text("❌ فایل ارسالی نامعتبر است. لطفاً فایل زیپ (.zip) ارسال کنید.")
        return
        
    action = context.user_data.get('admin_zip_action', 'zip_to_link')
    msg = await update.message.reply_text("⏳ در حال دریافت و استخراج فایل...")
    
    expire_time = await redis_client.get("settings:expire_time")
    expire_time = int(expire_time) if expire_time else 7200
    
    new_file = await update.message.document.get_file()
    
    with tempfile.TemporaryDirectory() as temp_dir:
        zip_path = os.path.join(temp_dir, "uploaded.zip")
        await new_file.download_to_drive(zip_path)
        
        extracted_dir = os.path.join(temp_dir, "extracted")
        await asyncio.to_thread(shutil.unpack_archive, zip_path, extracted_dir)
        
        json_files_paths = []
        for root, dirs, files in os.walk(extracted_dir):
            for file in files:
                if file.lower().endswith('.json'):
                    json_files_paths.append(os.path.join(root, file))
                    
        if not json_files_paths:
            await msg.edit_text("⚠️ هیچ فایل JSON معتبری در فایل زیپ یافت نشد.")
            return

        if action == 'zip_to_link':
            links_text = "<b>لیست لینک‌های تولید شده:</b>\n\n"
            count = 0
            for file_path in json_files_paths:
                filename = os.path.basename(file_path)
                try:
                    phone = filename.replace('.json', '')
                    
                    existing_link = await redis_client.get(f"phone_active_link:{phone}")
                    if existing_link:
                        links_text += f"📱 <b>شماره {phone}:</b>\n⚠️ تکراری (لینک از قبل موجود است)\n\n"
                        continue

                    with open(file_path, 'r', encoding='utf-8') as f:
                        file_content = f.read()
                        data = json.loads(file_content)
                        access_token, refresh_token = None, None
                        for cookie in data.get('cookies', []):
                            if cookie.get('name') == 'tokenMS': access_token = cookie.get('value')
                            elif cookie.get('name') == 'refresh_token': refresh_token = cookie.get('value')
                        if not access_token:
                            for origin in data.get('origins', []):
                                for item in origin.get('localStorage', []):
                                    if item.get('name') == 'tokenMS': access_token = item.get('value')
                                    elif item.get('name') == 'refresh_token': refresh_token = item.get('value')
                                    
                        if access_token and not await redis_client.exists(f"account:{phone}"):
                            await redis_client.hset(f"account:{phone}", mapping={"access_token": access_token, "refresh_token": refresh_token or ""})
                        link_id = str(uuid.uuid4())[:12]
                        await redis_client.setex(f"acc_link:{link_id}", expire_time, file_content)
                        final_url = f"{WEB_DOMAIN}/acc/{link_id}"
                        
                        await redis_client.setex(f"phone_active_link:{phone}", expire_time, final_url)
                        
                        links_text += f"📱 <b>شماره {phone}:</b>\n{final_url}\n\n"
                        count += 1
                except Exception:
                    pass
            if len(links_text) > 4000:
                file_out = io.BytesIO(links_text.encode('utf-8'))
                await context.bot.send_document(chat_id=user_id, document=file_out, filename=f"Links_{int(time.time())}.txt", caption=f"✅ استخراج {count} اکانت انجام شد.")
                await msg.delete()
            else:
                await msg.edit_text(f"✅ <b>تعداد {count} اکانت ذخیره شد:</b>\n\n{links_text}", disable_web_page_preview=True, parse_mode='HTML')

        elif action == 'zip_discount_check':
            await msg.edit_text("🔍 در حال بررسی وضعیت تخفیف‌ها با سیستم ضدربات و رفرش‌توکن. لطفاً منتظر بمانید...")
            await fetch_and_update_proxies_from_api()

            discount_dir = os.path.join(temp_dir, "Discount_Accounts")
            os.makedirs(os.path.join(discount_dir, 'accounts'), exist_ok=True)
            links_text = "<b>لیست لینک‌های دارای تخفیف:</b>\n\n"
            discount_count = 0
            
            api = OkalaAPI()
            loop = asyncio.get_running_loop()
            sem = asyncio.Semaphore(18)
            lock = asyncio.Lock()

            raw_logs = await redis_client.lrange("global_link_logs", 0, -1)
            phone_to_latest_link = {}
            for item in raw_logs:
                try:
                    entry = json.loads(item)
                    phone_to_latest_link[entry['phone']] = entry['link']
                except: pass
            
            def _check_sync_zip(acc_token, ref_token, uid, p_dict):
                status, res = api.check_discount_api(acc_token, uid, proxy_dict=p_dict)
                if status == 401 and ref_token:
                    new_acc, new_ref = api.refresh_token(ref_token, proxy_dict=p_dict)
                    if new_acc:
                        status, res = api.check_discount_api(new_acc, uid, proxy_dict=p_dict)
                        return status, res, new_acc, new_ref
                return status, res, None, None
            
            async def _process_zip_file(file_path):
                nonlocal discount_count, links_text
                filename = os.path.basename(file_path)
                async with sem:
                    try:
                        with open(file_path, 'r', encoding='utf-8') as f:
                            file_content = f.read()
                            data = json.loads(file_content)
                            access_token = None
                            refresh_token = None
                            phone = filename.replace('.json', '')
                            for cookie in data.get('cookies', []):
                                if cookie.get('name') == 'tokenMS': access_token = cookie.get('value')
                                if cookie.get('name') == 'refresh_token': refresh_token = cookie.get('value')
                            if not access_token:
                                for origin in data.get('origins', []):
                                    for item in origin.get('localStorage', []):
                                        if item.get('name') == 'tokenMS': access_token = item.get('value')
                                        if item.get('name') == 'refresh_token': refresh_token = item.get('value')
                            
                            if access_token:
                                user_uuid = get_user_id_from_token(access_token)
                                if user_uuid:
                                    proxy_dict = await get_random_proxy_from_db()
                                    status, res, new_acc, new_ref = await loop.run_in_executor(
                                        executor, _check_sync_zip, access_token, refresh_token, user_uuid, proxy_dict
                                    )
                                    
                                    if new_acc:
                                        data = update_tokens_in_data(data, access_token, new_acc, refresh_token, new_ref)
                                        file_content = json.dumps(data, ensure_ascii=False)
                                        with open(file_path, 'w', encoding='utf-8') as fw:
                                            fw.write(file_content)

                                    if status == 200 and isinstance(res, dict):
                                        vouchers = res.get('data', [])
                                        if vouchers:
                                            async with lock:
                                                discount_count += 1
                                                shutil.copy2(file_path, os.path.join(discount_dir, 'accounts', filename))
                                                old_link = phone_to_latest_link.get(phone, "لینک قدیمی در دیتابیس یافت نشد")
                                                links_text += f"📱 <b>شماره {phone}:</b>\n{old_link}\n\n"
                                        
                    except Exception as e:
                        async with lock:
                            api.request_logs.append(f"[{filename}] Exception: {str(e)}\n{'-'*40}\n")

            await asyncio.gather(*[_process_zip_file(fp) for fp in json_files_paths])

            debug_logs = api.request_logs
            ts = int(time.time())
            
            try:
                await msg.delete()
            except Exception: pass

            if discount_count > 0:
                discount_zip_path = os.path.join(temp_dir, "Discounted_Accounts")
                await asyncio.to_thread(shutil.make_archive, discount_zip_path, 'zip', discount_dir)
                
                with open(discount_zip_path + '.zip', 'rb') as zip_file:
                    await context.bot.send_document(chat_id=user_id, document=zip_file, filename="Discounted_Accounts.zip", caption=f"🎁 <b>فایل خروجی (فیلتر شده)</b>\nتعداد اکانت‌های دارای تخفیف: {discount_count}", parse_mode='HTML')
                
                links_out = io.BytesIO(links_text.encode('utf-8'))
                await context.bot.send_document(chat_id=user_id, document=links_out, filename=f"Discount_Report_{ts}.txt", caption="✅ گزارش لینک‌های دارای تخفیف")
            else:
                report_out = io.BytesIO("هیچ‌یک از اکانت‌های موجود دارای تخفیف نبودند.".encode('utf-8'))
                await context.bot.send_document(chat_id=user_id, document=report_out, filename=f"Discount_Report_{ts}.txt", caption="⚠️ گزارش تخفیف‌ها (تخفیفی یافت نشد)")
                
            if debug_logs:
                debug_out = io.BytesIO("".join(debug_logs).encode('utf-8'))
                await context.bot.send_document(chat_id=user_id, document=debug_out, filename=f"Okala_Logs_{ts}.txt", caption="📄 گزارش لاگ‌های اکالا (API)")

# ==========================================
# مینی‌سرور وب
# ==========================================
async def web_handler_get_account(request):
    link_id = request.match_info.get('link_id', '')
    data = await redis_client.get(f"acc_link:{link_id}")
    if data:
        return web.json_response(json.loads(data))
    return web.json_response({"error": "لینک نامعتبر است یا منقضی شده."}, status=404)

async def start_web_server():
    app = web.Application()
    app.router.add_get('/acc/{link_id}', web_handler_get_account)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080)) 
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()

# ==========================================
# منوها و دکمه‌ها
# ==========================================
def get_main_keyboard(is_admin_user, active_tag_name=None):
    keyboard = [[InlineKeyboardButton("🔑 ورود به حساب", callback_data="user_login")]]
    
    tag_btn_text = f"🏷 تغییر/حذف برچسب (فعال: {active_tag_name})" if active_tag_name else "🏷 تنظیم برچسب نشست (Tag)"
    keyboard.append([InlineKeyboardButton(tag_btn_text, callback_data="set_tag")])
    
    keyboard.append([
        InlineKeyboardButton("📂 برچسب‌های من", callback_data="my_tags"),
        InlineKeyboardButton("🔍 جستجوی لینک", callback_data="search_links")
    ])
    
    keyboard.append([InlineKeyboardButton("🎁 بررسی تخفیف لینک‌ها", callback_data="check_user_links")])
    
    keyboard.append([InlineKeyboardButton("📞 تماس با مدیر", callback_data="contact_admin")])
    
    if is_admin_user:
        keyboard.append([InlineKeyboardButton("⚙️ پنل مدیریت", callback_data="admin_panel")])
    return InlineKeyboardMarkup(keyboard)

def get_admin_keyboard():
    keyboard = [
        [InlineKeyboardButton("📊 آمار دیتابیس", callback_data="admin_stats"), InlineKeyboardButton("⏳ تنظیم انقضا", callback_data="admin_expire")],
        [InlineKeyboardButton("📋 گزارش لینک‌های کاربران", callback_data="admin_users_report")],
        [InlineKeyboardButton("🎁 بررسی تخفیف دیتابیس", callback_data="admin_check_discounts")],
        [InlineKeyboardButton("🔗 تبدیل زیپ به لینک", callback_data="admin_zip_to_link"), InlineKeyboardButton("🔍 بررسی تخفیف زیپ", callback_data="admin_zip_discount")],
        [InlineKeyboardButton("📥 استخراج شماره‌ها", callback_data="admin_export"), InlineKeyboardButton("🗑 پاکسازی", callback_data="admin_clear")],
        [InlineKeyboardButton("🔗 استخراج لینک‌ها", callback_data="admin_export_links"), InlineKeyboardButton("🔑 استخراج توکن‌ها", callback_data="admin_export_tokens")],
        [InlineKeyboardButton("🛠 تعمیر لینک‌های ناقص (سریع)", callback_data="admin_repair_links")],
        [InlineKeyboardButton("🌐 تنظیم پروکسی", callback_data="admin_set_proxy")],
        [InlineKeyboardButton("🚫 مدیریت دسترسی کاربران", callback_data="admin_manage_users")],
        [InlineKeyboardButton("⏸ روشن/خاموش کردن", callback_data="admin_toggle")],
        [InlineKeyboardButton("🔙 بازگشت به منوی اصلی", callback_data="main_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    admin_status = is_admin(user_id)
    active_tag = context.user_data.get('active_tag_name')
    
    text = (
        f"👋 <b>به سیستم لینک ساز خوش آمدید.</b>\n\n"
        f"🆔 آیدی عددی شما: <code>{user_id}</code>\n"
        f"👑 دسترسی ادمین: <b>{'بله ✅' if admin_status else 'خیر ❌'}</b>\n\n"
        f"لطفاً یک گزینه را انتخاب کنید:"
    )
    
    if update.message:
        await update.message.reply_text(text, reply_markup=get_main_keyboard(admin_status, active_tag), parse_mode='HTML')
    else:
        try:
            await update.callback_query.edit_message_text(text, reply_markup=get_main_keyboard(admin_status, active_tag), parse_mode='HTML')
        except Exception:
            pass

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    context.user_data['admin_state'] = None
    context.user_data['admin_zip_action'] = None
    await update.message.reply_text("⚙️ <b>پنل مدیریت سیستم:</b>", reply_markup=get_admin_keyboard(), parse_mode='HTML')

# ==========================================
# توابع مربوط به برچسب‌گذاری (Tagging) و جستجو
# ==========================================
async def ask_tag_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    kb = [[InlineKeyboardButton("❌ لغو عملیات", callback_data="cancel_action")]]
    if context.user_data.get('active_tag_name'):
        kb.insert(0, [InlineKeyboardButton("🗑 پاک کردن برچسب فعلی", callback_data="clear_active_tag")])
        
    text = (
        "🏷 <b>تنظیم برچسب (Batch Tagging)</b>\n\n"
        "لطفاً یک نام برای این دسته ارسال کنید (مثلاً: <code>مشتری 1</code> یا <code>سفارش صبح</code>).\n"
        "تمامی لینک‌هایی که از این پس ساخته شوند تحت این برچسب در بخش «برچسب‌های من» ذخیره خواهند شد."
    )
    await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
    return ASK_TAG

async def receive_tag_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    tag_name = update.message.text.strip()
    tag_id = uuid.uuid4().hex[:8]
    context.user_data['active_tag_id'] = tag_id
    context.user_data['active_tag_name'] = tag_name
    
    await update.message.reply_text(
        f"✅ برچسب <b>«{tag_name}»</b> با موفقیت تنظیم شد.\nاکنون می‌توانید شروع به ساختن لینک کنید.", 
        parse_mode='HTML', 
        reply_markup=get_main_keyboard(is_admin(update.effective_user.id), tag_name)
    )
    return ConversationHandler.END

async def clear_active_tag_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop('active_tag_id', None)
    context.user_data.pop('active_tag_name', None)
    await update.callback_query.answer("برچسب پاک شد 🗑")
    await show_main_menu(update, context)
    return ConversationHandler.END

async def ask_search_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو عملیات", callback_data="cancel_action")]])
    text = (
        "🔍 <b>جستجوی سریع لینک‌ها</b>\n\n"
        "لطفاً شماره موبایل کامل یا <b>۴ رقم آخر</b> آن‌ها را ارسال کنید.\n"
        "می‌توانید در هر خط یک شماره بفرستید تا ربات همه را با هم جستجو کند.\n\n"
        "<b>مثال:</b>\n"
        "<code>09123456789\n5678\n09351112222</code>"
    )
    await update.callback_query.edit_message_text(text, reply_markup=kb, parse_mode='HTML')
    return ASK_SEARCH

async def receive_search_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    queries = update.message.text.strip().split('\n')
    queries = [q.strip() for q in queries if q.strip()]
    
    msg = await update.message.reply_text("⏳ در حال جستجو در دیتابیس...")
    
    raw_logs = await redis_client.lrange("global_link_logs", 0, -1)
    phone_to_latest_link = {}
    
    for item in raw_logs:
        try:
            entry = json.loads(item)
            phone_to_latest_link[entry['phone']] = entry['link']
        except: pass
        
    report_text = "🔍 <b>نتایج جستجوی شما:</b>\n\n"
    found_count = 0
    
    for q in queries:
        found_for_q = False
        for phone, link in phone_to_latest_link.items():
            if q == phone or (len(q) >= 4 and phone.endswith(q)):
                report_text += f"✅ <code>{phone}</code>\n🔗 {link}\n\n"
                found_for_q = True
                found_count += 1
        if not found_for_q:
            report_text += f"❌ <code>{q}</code> ➔ یافت نشد\n\n"
            
    if len(report_text) > 4000:
        file_out = io.BytesIO(report_text.encode('utf-8'))
        await context.bot.send_document(chat_id=update.effective_user.id, document=file_out, filename=f"Search_Results_{int(time.time())}.txt", caption=f"✅ جستجو پایان یافت. پیدا شده: {found_count}")
        await msg.delete()
    else:
        await msg.edit_text(report_text, parse_mode='HTML', disable_web_page_preview=True)
        
    await show_main_menu(update, context)
    return ConversationHandler.END

# ==========================================
# بررسی تخفیف لینک‌های کاربر
# ==========================================
async def ask_user_links_for_discount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    
    if not await is_user_approved_for_discount(user_id):
        pending_key = f"pending_req:discount:{user_id}"
        
        if await redis_client.exists(pending_key):
            await update.callback_query.answer("⚠️ درخواست شما قبلاً برای ادمین ارسال شده است. لطفاً منتظر بمانید.", show_alert=True)
            return ConversationHandler.END
            
        await redis_client.setex(pending_key, 86400, "1")
        
        tg_user = update.effective_user
        admin_text = (
            "👤 <b>درخواست دسترسی به چکر تخفیف</b>\n\n"
            f"نام: {tg_user.full_name}\n"
            f"یوزرنیم: @{tg_user.username or 'ندارد'}\n"
            f"آیدی: <code>{user_id}</code>\n\n"
            "آیا با دادن دسترسی به این کاربر موافقت می‌کنید؟"
        )
        
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ تایید دسترسی", callback_data=f"approve_discount_{user_id}")],
            [InlineKeyboardButton("❌ رد درخواست", callback_data=f"deny_discount_{user_id}")]
        ])
        
        try:
            await context.bot.send_message(chat_id=MASTER_ADMIN_ID, text=admin_text, reply_markup=kb, parse_mode='HTML')
        except Exception as e:
            logging.error(f"Error sending request to Master Admin: {e}")
            
        await update.callback_query.answer("❌ شما به این بخش دسترسی ندارید. درخواست شما برای تایید به ادمین ارسال شد.", show_alert=True)
        return ConversationHandler.END
    
    await update.callback_query.answer()
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو عملیات", callback_data="cancel_action")]])
    text = (
        "🎁 <b>بررسی وضعیت تخفیف لینک‌ها</b>\n\n"
        "لطفاً لینک‌های تولید شده (یا شناسه‌های انتهای لینک) را ارسال کنید.\n"
        "می‌توانید چند لینک را زیر هم قرار داده و با یک پیام ارسال کنید تا ربات همه را همزمان بررسی کند."
    )
    await update.callback_query.edit_message_text(text, reply_markup=kb, parse_mode='HTML')
    return ASK_LINKS_FOR_DISCOUNT

async def process_user_links_discount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    
    if not await is_user_approved_for_discount(user_id):
        await update.message.reply_text("❌ دسترسی شما به این بخش لغو شده است.")
        await show_main_menu(update, context)
        return ConversationHandler.END
    
    text = update.message.text.strip()
    
    found_ids = []
    lines = text.split('\n')
    for line in lines:
        line = line.strip()
        if not line: continue
        match = re.search(r'/acc/([a-zA-Z0-9_-]+)', line)
        if match:
            found_ids.append((line, match.group(1)))
        else:
            found_ids.append((line, line))
            
    if not found_ids:
        await update.message.reply_text("❌ هیچ لینک معتبری یافت نشد. به منوی اصلی بازمی‌گردید.")
        await show_main_menu(update, context)
        return ConversationHandler.END

    msg = await update.message.reply_text(f"⏳ در حال بررسی <b>{len(found_ids)}</b> لینک... لطفاً منتظر بمانید.", parse_mode='HTML')
    
    await fetch_and_update_proxies_from_api()

    api = OkalaAPI()
    loop = asyncio.get_running_loop()
    sem = asyncio.Semaphore(18)

    async def _check_single_user_link(item):
        original_text, link_id = item
        async with sem:
            data = await redis_client.get(f"acc_link:{link_id}")
            if not data:
                return f"🔗 <code>{original_text}</code>\n❌ <i>لینک نامعتبر یا منقضی شده در سیستم</i>\n\n"
                
            data_json = json.loads(data)
            access_token = None
            refresh_token = None
            
            for cookie in data_json.get('cookies', []):
                if cookie.get('name') == 'tokenMS': access_token = cookie.get('value')
                if cookie.get('name') == 'refresh_token': refresh_token = cookie.get('value')
            
            if not access_token:
                for origin in data_json.get('origins', []):
                    for sub_item in origin.get('localStorage', []):
                        if sub_item.get('name') == 'tokenMS': access_token = sub_item.get('value')
                        if sub_item.get('name') == 'refresh_token': refresh_token = sub_item.get('value')
            
            if not access_token:
                return f"🔗 <code>{original_text}</code>\n❌ <i>توکن احراز هویت در این لینک یافت نشد</i>\n\n"
                
            user_uuid = get_user_id_from_token(access_token)
            if not user_uuid:
                return f"🔗 <code>{original_text}</code>\n❌ <i>آیدی کاربر (UUID) قابل شناسایی نیست</i>\n\n"
                
            proxy_dict = await get_random_proxy_from_db()
            
            def _do_check():
                status, res = api.check_discount_api(access_token, user_uuid, proxy_dict)
                if status == 401 and refresh_token:
                    new_acc, new_ref = api.refresh_token(refresh_token, proxy_dict)
                    if new_acc:
                        return api.check_discount_api(new_acc, user_uuid, proxy_dict)
                return status, res
                
            status, res = await loop.run_in_executor(executor, _do_check)
            
            if status == 200 and isinstance(res, dict):
                vouchers = res.get('data', [])
                if vouchers:
                    amounts = [v.get('discountAmount', 0) for v in vouchers if v.get('discountAmount')]
                    max_amount = max(amounts) // 10000 if amounts else 0
                    return f"🔗 <code>{original_text}</code>\n✅ <b>تخفیف دارد!</b> مبلغ: {max_amount} هزار تومان\n\n"
                else:
                    return f"🔗 <code>{original_text}</code>\n➖ <i>تخفیف ندارد</i>\n\n"
            elif status == 401:
                return f"🔗 <code>{original_text}</code>\n🔒 <i>توکن منقضی شده است و رفرش نشد</i>\n\n"
            else:
                return f"🔗 <code>{original_text}</code>\n⚠️ <i>خطا در ارتباط با اکالا ({status})</i>\n\n"

    results = await asyncio.gather(*[_check_single_user_link(it) for it in found_ids])
    report = "🎁 <b>گزارش بررسی تخفیف لینک‌های شما:</b>\n\n" + "".join(results)

    try:
        await msg.delete()
    except Exception: pass

    ts = int(time.time())
    
    report_out = io.BytesIO(report.encode('utf-8'))
    await context.bot.send_document(
        chat_id=update.effective_user.id, 
        document=report_out, 
        filename=f"Discount_Report_{ts}.txt", 
        caption="✅ گزارش وضعیت تخفیف لینک‌ها"
    )
    
    if api.request_logs:
        log_out = io.BytesIO("".join(api.request_logs).encode('utf-8'))
        await context.bot.send_document(
            chat_id=update.effective_user.id, 
            document=log_out, 
            filename=f"Okala_Logs_{ts}.txt", 
            caption="📄 گزارش لاگ‌های اکالا (API)"
        )
        
    await show_main_menu(update, context)
    return ConversationHandler.END

# ==========================================
# مدیریت دکمه‌های اصلی و ادمین
# ==========================================
async def core_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data
    
    if data.startswith("approve_discount_"):
        if user_id != MASTER_ADMIN_ID:
            await query.answer("❌ شما اجازه این کار را ندارید.", show_alert=True)
            return
        target_id = data.split("approve_discount_")[1]
        await approve_user_for_discount(target_id)
        await query.edit_message_text(f"✅ دسترسی کاربر <code>{target_id}</code> تایید شد.", parse_mode='HTML')
        try:
            await context.bot.send_message(chat_id=target_id, text="🎉 <b>درخواست شما تایید شد!</b>\nاکنون می‌توانید از دکمه بررسی تخفیف لینک‌ها استفاده کنید.", parse_mode='HTML')
        except:
            pass
        return
        
    if data.startswith("deny_discount_"):
        if user_id != MASTER_ADMIN_ID:
            await query.answer("❌ شما اجازه این کار را ندارید.", show_alert=True)
            return
        target_id = data.split("deny_discount_")[1]
        await remove_user_pending_req(target_id)
        await query.edit_message_text(f"❌ درخواست کاربر <code>{target_id}</code> رد شد.", parse_mode='HTML')
        try:
            await context.bot.send_message(chat_id=target_id, text="❌ <b>متاسفانه درخواست دسترسی شما توسط ادمین رد شد.</b>", parse_mode='HTML')
        except:
            pass
        return

    if data == "main_menu":
        await query.answer()
        context.user_data['admin_state'] = None
        await show_main_menu(update, context)
        return
        
    if data == "contact_admin":
        await query.answer()
        await query.edit_message_text(
            "📞 <b>ارتباط با مدیریت:</b>\n\n"
            "جهت هرگونه سوال، پیشنهاد یا گزارش مشکل به آیدی زیر پیام دهید:\n"
            "@navlink_1",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت به منوی اصلی", callback_data="main_menu")]]),
            parse_mode='HTML'
        )
        return
        
    if data == "finish_link_creation":
        await query.answer("در حال آماده‌سازی لینک‌های شما... ⏳")
        session_links = context.user_data.get('session_links', [])
        active_tag_name = context.user_data.get('active_tag_name')
        active_tag_id = context.user_data.get('active_tag_id')
        
        if not session_links:
            await query.edit_message_text("⚠️ هیچ لینکی در این نوبت ساخته نشده است.", reply_markup=get_main_keyboard(is_admin(user_id), active_tag_name))
            return
        
        report_text = f"🎉 <b>لینک‌های تولید شده شما (تعداد: {len(session_links)}):</b>\n"
        
        if active_tag_name and active_tag_id:
            report_text += f"🏷 <b>ذخیره شده در برچسب:</b> {active_tag_name}\n\n"
            tag_meta_key = f"user_tag_meta:{user_id}:{active_tag_id}"
            await redis_client.set(tag_meta_key, active_tag_name)
            await redis_client.sadd(f"user_tags_set:{user_id}", active_tag_id)
            
            tag_links_key = f"user_tag_links:{active_tag_id}"
            for item in session_links:
                await redis_client.rpush(tag_links_key, json.dumps(item, ensure_ascii=False))
        else:
            report_text += "\n"
            
        for idx, item in enumerate(session_links, 1):
            report_text += f"{idx}. 📱 <b>شماره:</b> <code>{item['phone']}</code>\n🔗 {item['link']}\n\n"
        
        context.user_data['session_links'] = []
        
        if len(report_text) > 4000:
            file_out = io.BytesIO(report_text.encode('utf-8'))
            await context.bot.send_document(chat_id=user_id, document=file_out, filename=f"My_Links_{int(time.time())}.txt", caption="✅ لینک‌های ساخته شده شما")
            await query.message.delete()
            await context.bot.send_message(chat_id=user_id, text="بازگشت به منوی اصلی:", reply_markup=get_main_keyboard(is_admin(user_id), active_tag_name))
        else:
            await query.edit_message_text(report_text, disable_web_page_preview=True, parse_mode='HTML')
            await context.bot.send_message(chat_id=user_id, text="بازگشت به منوی اصلی:", reply_markup=get_main_keyboard(is_admin(user_id), active_tag_name))
        return

    if data == "my_tags":
        await query.answer()
        tag_ids = await redis_client.smembers(f"user_tags_set:{user_id}")
        active_tag_name = context.user_data.get('active_tag_name')
        
        if not tag_ids:
            await query.edit_message_text("⚠️ شما هنوز هیچ برچسبی ایجاد نکرده‌اید.", reply_markup=get_main_keyboard(is_admin(user_id), active_tag_name))
            return
            
        kb = []
        for tid in list(tag_ids)[:40]: 
            t_name = await redis_client.get(f"user_tag_meta:{user_id}:{tid}")
            if t_name:
                kb.append([InlineKeyboardButton(f"🏷 {t_name}", callback_data=f"show_tag_{tid}")])
                
        kb.append([InlineKeyboardButton("🔙 بازگشت به منوی اصلی", callback_data="main_menu")])
        await query.edit_message_text("📂 <b>برچسب‌های ذخیره‌شده شما:</b>\nبرای مشاهده و دریافت لینک‌ها روی برچسب مورد نظر کلیک کنید:", reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
        return

    if data.startswith("show_tag_"):
        await query.answer()
        tid = data.split("show_tag_")[1]
        t_name = await redis_client.get(f"user_tag_meta:{user_id}:{tid}")
        raw_links = await redis_client.lrange(f"user_tag_links:{tid}", 0, -1)
        
        if not raw_links:
            await query.edit_message_text("⚠️ این برچسب خالی است.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="my_tags")]]))
            return
            
        report_text = f"📂 <b>لینک‌های برچسب «{t_name}» (تعداد: {len(raw_links)}):</b>\n\n"
        for idx, l in enumerate(raw_links, 1):
            try:
                ld = json.loads(l)
                report_text += f"{idx}. 📱 <code>{ld['phone']}</code>\n🔗 {ld['link']}\n\n"
            except: pass
            
        if len(report_text) > 4000:
            file_out = io.BytesIO(report_text.encode('utf-8'))
            await context.bot.send_document(chat_id=user_id, document=file_out, filename=f"Tag_{t_name}.txt", caption=f"📂 تمامی لینک‌های مربوط به برچسب: {t_name}")
            await context.bot.send_message(chat_id=user_id, text="بازگشت به لیست برچسب‌ها:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="my_tags")]]))
        else:
            await query.edit_message_text(report_text, disable_web_page_preview=True, parse_mode='HTML', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="my_tags")]]))
        return

    if not is_admin(user_id): return
    await query.answer()
    
    if data == "admin_panel":
        context.user_data['admin_zip_action'] = None
        await query.edit_message_text("⚙️ <b>پنل مدیریت سیستم:</b>", reply_markup=get_admin_keyboard(), parse_mode='HTML')
        
    elif data == "admin_manage_users":
        await query.edit_message_text(
            "🚫 <b>مدیریت دسترسی کاربران:</b>\n\n"
            "🔹 <b>/block @username</b> یا <b>/block userid</b> — مسدود کردن دسترسی تخفیف\n"
            "🔹 <b>/unblock @username</b> یا <b>/unblock userid</b> — آزاد کردن دسترسی تخفیف\n"
            "🔹 <b>/blocklist</b> — مشاهده لیست کاربرانی که تایید شده‌اند\n\n"
            "<b>مثال:</b>\n"
            "<code>/block @user123\n/block 7383838\n/unblock @user123</code>",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="admin_panel")]]),
            parse_mode='HTML'
        )
        
    elif data == "admin_set_proxy":
        context.user_data['admin_state'] = 'waiting_for_proxy'
        await query.edit_message_text(
            "🌐 <b>تنظیم پروکسی‌ها:</b>\n\n"
            "لطفاً لیست پروکسی‌های خود را (به صورت متن، لینک API، یا فایل `txt.`) ارسال کنید.\n\n"
            "⚠️ <b>فرمت‌های مجاز:</b>\n"
            "• `User:pass@ip:port`\n"
            "• `ip:port:user:pass`\n"
            "• لینک مستقیم فایل یا API پروکسی", 
            parse_mode='Markdown'
        )

    elif data == "admin_stats":
        acc_keys = await redis_client.keys("account:*")
        link_keys = await redis_client.keys("acc_link:*")
        
        proxy_count = 1000
        
        approved_users = await redis_client.smembers("approved_users:discount")
        approved_count = len(approved_users) if approved_users else 0
        
        maint = await redis_client.get("settings:maintenance")
        exp = await redis_client.get("settings:expire_time")
        exp = int(exp) if exp else 7200
        exp_str = f"{exp // 86400} روز" if exp >= 86400 else f"{exp // 3600} ساعت"
        status = "غیرفعال 🔴" if maint == "1" else "فعال 🟢"
        
        text = (
            "📊 <b>وضعیت سیستم:</b>\n\n"
            f"👤 <b>تعداد کل اکانت‌ها:</b> <code>{len(acc_keys)}</code>\n"
            f"🔗 <b>لینک‌های فعال:</b> <code>{len(link_keys)}</code>\n"
            f"🌐 <b>تعداد پروکسی‌ها:</b> <code>{proxy_count}</code>\n"
            f"✅ <b>کاربران تایید شده (تخفیف):</b> <code>{approved_count}</code>\n"
            f"⏳ <b>زمان انقضای لینک‌ها:</b> {exp_str}\n"
            f"🤖 <b>وضعیت ربات:</b> {status}"
        )
        await query.edit_message_text(text, reply_markup=get_admin_keyboard(), parse_mode='HTML')
        
    elif data == "admin_users_report":
        raw_logs = await redis_client.lrange("global_link_logs", 0, -1)
        if not raw_logs:
            await context.bot.send_message(chat_id=user_id, text="⚠️ هیچ گزارشی از ساخت لینک توسط کاربران ثبت نشده است.")
            return
        
        await context.bot.send_message(chat_id=user_id, text="⏳ در حال استخراج گزارش کاربران...")
        
        users_data = {}
        for item in raw_logs:
            try:
                entry = json.loads(item)
                uid = entry.get("tg_id")
                if uid not in users_data:
                    users_data[uid] = {
                        "name": entry.get("tg_name", "نامشخص"),
                        "username": entry.get("tg_user", ""),
                        "links": []
                    }
                users_data[uid]["links"].append(entry)
            except Exception: pass

        
        report_text = "📊 <b>گزارش جامع ساخت لینک کاربران:</b>\n\n"
        for uid, udata in users_data.items():
            uname_str = f" (@{udata['username']})" if udata['username'] else ""
            report_text += f"👤 کاربر: {udata['name']}{uname_str}\n🆔 آیدی: <code>{uid}</code>\n🔢 تعداد کل لینک‌ها: {len(udata['links'])}\n------------------------------------\n"
            
        file_out = io.BytesIO(report_text.encode('utf-8'))
        await context.bot.send_document(chat_id=user_id, document=file_out, filename=f"Users_Summary_{int(time.time())}.txt", caption="📊 گزارش خلاصه کارکرد کاربران")

    elif data == "admin_expire":
        kb = [
            [InlineKeyboardButton("۱ ساعت ⏱", callback_data="set_exp_3600"), InlineKeyboardButton("۲۴ ساعت 🕐", callback_data="set_exp_86400")],
            [InlineKeyboardButton("۱ هفته 📅", callback_data="set_exp_604800"), InlineKeyboardButton("۱ ماه 📆", callback_data="set_exp_2592000")],
            [InlineKeyboardButton("🔙 بازگشت", callback_data="admin_panel")]
        ]
        await query.edit_message_text("⏳ <b>زمان انقضای لینک‌ها را تعیین کنید:</b>", reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
        
    elif data.startswith("set_exp_"):
        new_time = int(data.split("_")[2])
        await redis_client.set("settings:expire_time", new_time)
        exp_str = f"{new_time // 86400} روز" if new_time >= 86400 else f"{new_time // 3600} ساعت"
        await query.edit_message_text(f"✅ انقضای لینک‌ها با موفقیت به <b>{exp_str}</b> تغییر یافت.", reply_markup=get_admin_keyboard(), parse_mode='HTML')

    elif data == "admin_check_discounts":
        acc_keys = await redis_client.keys("account:*")
        if not acc_keys:
            await context.bot.send_message(chat_id=user_id, text="⚠️ دیتابیس سیستم خالی است.")
            return
        await context.bot.send_message(chat_id=user_id, text="⏳ در حال پردازش دیتابیس با سیستم ضدربات (پروکسی). لطفاً منتظر بمانید...")
        asyncio.create_task(process_discounts_and_send_report(context.bot, user_id, acc_keys))

    elif data == "admin_zip_to_link":
        context.user_data['admin_zip_action'] = 'zip_to_link'
        await context.bot.send_message(chat_id=user_id, text="🔗 <b>عملیات استخراج لینک:</b>\nلطفاً فایل ZIP مربوطه را ارسال کنید.", parse_mode='HTML')
        
    elif data == "admin_zip_discount":
        context.user_data['admin_zip_action'] = 'zip_discount_check'
        await context.bot.send_message(chat_id=user_id, text="🔍 <b>عملیات بررسی تخفیف:</b>\nلطفاً فایل ZIP مربوطه را ارسال کنید.", parse_mode='HTML')

    elif data == "admin_export":
        acc_keys = await redis_client.keys("account:*")
        if not acc_keys:
            await context.bot.send_message(chat_id=user_id, text="⚠️ دیتابیس سیستم خالی است.")
            return
        export_text = "لیست شماره‌های ثبت شده در سیستم:\n\n"
        for key in acc_keys: export_text += f"{key.replace('account:', '')}\n"
        
        file_out = io.BytesIO(export_text.encode('utf-8'))
        try:
            await context.bot.send_document(chat_id=user_id, document=file_out, filename=f"Accounts_{int(time.time())}.txt", caption="📥 فایل دیتابیس (شماره‌ها) دریافت شد.")
        except Exception as e:
            logging.error(f"Error sending export: {e}")
            await context.bot.send_message(chat_id=user_id, text="❌ خطا در ارسال فایل استخراج.")

    elif data == "admin_export_links":
        link_keys = await redis_client.keys("acc_link:*")
        if not link_keys:
            await context.bot.send_message(chat_id=user_id, text="⚠️ هیچ لینکی در دیتابیس موجود نیست.")
            return
            
        await context.bot.send_message(chat_id=user_id, text="⏳ در حال استخراج لینک‌ها و شماره‌ها. لطفاً منتظر بمانید...")
        
        export_text = "لیست لینک‌های فعال به همراه شماره:\n\n"
        count = 0
        for l_key in link_keys:
            link_id = l_key.replace("acc_link:", "")
            final_url = f"{WEB_DOMAIN}/acc/{link_id}"
            link_data = await redis_client.get(l_key)
            phone = "نامشخص"
            try:
                data_json = json.loads(link_data)
                origins = data_json.get("origins", [])
                if origins:
                    for item in origins[0].get("localStorage", []):
                        if item.get("name") == "user":
                            user_obj = json.loads(urllib.parse.unquote(item.get("value")))
                            phone = user_obj.get("mobilePhone", "نامشخص")
                            break
            except Exception:
                pass
            export_text += f"📱 شماره: {phone}\n🔗 لینک: {final_url}\n\n"
            count += 1
            
        file_out = io.BytesIO(export_text.encode('utf-8'))
        try:
            await context.bot.send_document(chat_id=user_id, document=file_out, filename=f"Links_With_Phone_{int(time.time())}.txt", caption=f"✅ استخراج {count} لینک با موفقیت انجام شد.")
        except Exception as e:
            logging.error(f"Error sending links doc: {e}")
            await context.bot.send_message(chat_id=user_id, text="❌ خطا در ارسال فایل لینک‌ها.")

    elif data == "admin_export_tokens":
        acc_keys = await redis_client.keys("account:*")
        if not acc_keys:
            await context.bot.send_message(chat_id=user_id, text="⚠️ دیتابیس سیستم خالی است.")
            return
        await context.bot.send_message(chat_id=user_id, text="⏳ در حال استخراج توکن‌ها...")
        exported_data = {}
        for key in acc_keys:
            phone = key.replace('account:', '')
            tokens = await redis_client.hgetall(key)
            exported_data[phone] = {
                "access_token": tokens.get("access_token", ""),
                "refresh_token": tokens.get("refresh_token", "")
            }
        json_data = json.dumps(exported_data, indent=4, ensure_ascii=False)
        file_out = io.BytesIO(json_data.encode('utf-8'))
        try:
            await context.bot.send_document(chat_id=user_id, document=file_out, filename=f"Tokens_DB_{int(time.time())}.json", caption=f"🔑 فایل توکن‌های استخراج شده ({len(acc_keys)} شماره)")
        except Exception as e:
            logging.error(f"Error sending tokens doc: {e}")
            await context.bot.send_message(chat_id=user_id, text="❌ خطا در ارسال فایل توکن‌ها.")

    elif data == "admin_repair_links":
        link_keys = await redis_client.keys("acc_link:*")
        if not link_keys:
            await context.bot.send_message(chat_id=user_id, text="⚠️ هیچ لینکی در سیستم جهت تعمیر وجود ندارد.")
            return
            
        msg = await context.bot.send_message(chat_id=user_id, text="🛠 در حال بررسی و تعمیر لینک‌ها (عملیات دیتابیس داخلی)...")
        repaired_count = 0
        for l_key in link_keys:
            try:
                link_data = await redis_client.get(l_key)
                data_json = json.loads(link_data)
                phone = None
                origins = data_json.get("origins", [])
                if origins:
                    for item in origins[0].get("localStorage", []):
                        if item.get("name") == "user":
                            user_obj = json.loads(urllib.parse.unquote(item.get("value")))
                            phone = user_obj.get("mobilePhone")
                            break
                if phone:
                    acc_data = await redis_client.hgetall(f"account:{phone}")
                    r_tok = acc_data.get("refresh_token")
                    t_ms = acc_data.get("access_token")
                    if r_tok and t_ms:
                        if "cookies" not in data_json:
                            data_json["cookies"] = []
                        data_json["cookies"] = [c for c in data_json["cookies"] if c.get("name") not in ["tokenMS", "token", "refresh_token"]]
                        data_json["cookies"].append({"name": "tokenMS", "value": t_ms, "domain": ".okala.com", "path": "/", "secure": True, "sameSite": "None"})
                        data_json["cookies"].append({"name": "token", "value": t_ms, "domain": ".okala.com", "path": "/", "secure": True, "sameSite": "None"})
                        data_json["cookies"].append({"name": "refresh_token", "value": r_tok, "domain": ".okala.com", "path": "/", "secure": True, "sameSite": "None"})
                        ttl = await redis_client.ttl(l_key)
                        if ttl > 0:
                            await redis_client.setex(l_key, ttl, json.dumps(data_json, ensure_ascii=False))
                            repaired_count += 1
            except Exception as e:
                logging.error(f"Error repairing link {l_key}: {e}")
        await msg.edit_text(f"✅ عملیات تعمیر پایان یافت.\nتعداد <b>{repaired_count}</b> لینک به صورت خودکار ترمیم شدند.", parse_mode='HTML')

    elif data == "admin_clear":
        kb = [[InlineKeyboardButton("✅ تایید عملیات حذف", callback_data="admin_clear_confirm"), InlineKeyboardButton("❌ انصراف", callback_data="admin_panel")]]
        await query.edit_message_text("⚠️ <b>اخطار:</b> این عملیات تمامی اطلاعات ثبت شده را حذف خواهد کرد.\nآیا تایید می‌کنید؟", reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
        
    elif data == "admin_clear_confirm":
        acc_keys = await redis_client.keys("account:*")
        if acc_keys: await redis_client.delete(*acc_keys)
        await query.edit_message_text("🗑 عملیات پاکسازی با موفقیت انجام شد.", reply_markup=get_admin_keyboard())
        
    elif data == "admin_toggle":
        current = await redis_client.get("settings:maintenance")
        new_val = "0" if current == "1" else "1"
        await redis_client.set("settings:maintenance", new_val)
        status = "غیرفعال (تعمیرات) 🔴" if new_val == "1" else "فعال 🟢"
        await query.edit_message_text(f"⚙️ <b>تغییر وضعیت سیستم:</b>\nوضعیت کنونی: {status}", reply_markup=get_admin_keyboard(), parse_mode='HTML')

# ==========================================
# دستورات تغییر وضعیت تایید کاربران برای چکر
# ==========================================
async def block_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ شما اجازه استفاده از این دستور را ندارید.")
        return
    
    if not context.args:
        await update.message.reply_text(
            "🚫 <b>نحوه استفاده (لغو دسترسی):</b>\n"
            "<code>/block user_id</code>\n\n"
            "<b>مثال:</b>\n"
            "<code>/block 7383838</code>",
            parse_mode='HTML'
        )
        return
    
    target = context.args[0].strip()
    
    if target.startswith('@'):
        await update.message.reply_text("⚠️ لطفاً از آیدی عددی استفاده کنید.")
        return
    
    if target.isdigit():
        target_user_id = int(target)
    else:
        await update.message.reply_text("❌ فرمت نامعتبر است. لطفاً آیدی عددی را ارسال کنید.")
        return
    
    await redis_client.srem("approved_users:discount", str(target_user_id))
    
    await update.message.reply_text(
        f"✅ کاربر <code>{target_user_id}</code> از دسترسی به بررسی تخفیف محروم شد.",
        parse_mode='HTML'
    )

async def unblock_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ شما اجازه استفاده از این دستور را ندارید.")
        return
    
    if not context.args:
        await update.message.reply_text(
            "🔓 <b>نحوه استفاده (اعطای دسترسی):</b>\n"
            "<code>/unblock user_id</code>\n\n"
            "<b>مثال:</b>\n"
            "<code>/unblock 7383838</code>",
            parse_mode='HTML'
        )
        return
    
    target = context.args[0].strip()
    
    if target.startswith('@'):
        await update.message.reply_text("⚠️ لطفاً از آیدی عددی استفاده کنید.")
        return
    
    if target.isdigit():
        target_user_id = int(target)
    else:
        await update.message.reply_text("❌ فرمت نامعتبر است. لطفاً آیدی عددی را ارسال کنید.")
        return
    
    await approve_user_for_discount(target_user_id)
    
    await update.message.reply_text(
        f"✅ کاربر <code>{target_user_id}</code> دسترسی به چکر تخفیف را دریافت کرد.",
        parse_mode='HTML'
    )

async def blocklist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ شما اجازه استفاده از این دستور را ندارید.")
        return
    
    approved_users = await redis_client.smembers("approved_users:discount")
    
    if not approved_users:
        await update.message.reply_text("✅ هنوز هیچ کاربری برای بخش چکر تخفیف تایید نشده است.")
        return
    
    report_text = "✅ <b>لیست کاربرانی که به چکر تخفیف دسترسی دارند:</b>\n\n"
    for uid in sorted(approved_users):
        report_text += f"• <code>{uid}</code>\n"
    
    report_text += f"\n<b>تعداد کل:</b> {len(approved_users)}"
    
    await update.message.reply_text(report_text, parse_mode='HTML')

# ==========================================
# سیستم هندل کردن ورودی متنی / فایلی / API پروکسی
# ==========================================
async def handle_admin_text_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id): return
    
    state = context.user_data.get('admin_state')
    
    if state == 'waiting_for_proxy':
        msg = await update.message.reply_text("⏳ در حال پردازش و دریافت پروکسی‌ها...")
        
        try:
            if update.message.text and update.message.text.strip().startswith("http"):
                api_link = update.message.text.strip()
                await redis_client.set("settings:proxy_api_url", api_link)
                count = await fetch_and_update_proxies_from_api(api_link)
                context.user_data['admin_state'] = None
                if count > 0:
                    await msg.edit_text(f"✅ لینک API ذخیره شد و تعداد <b>{count}</b> پروکسی با موفقیت دریافت گردید.", reply_markup=get_admin_keyboard(), parse_mode='HTML')
                else:
                    await msg.edit_text("⚠️ لینک API ذخیره شد اما خروجی پروکسی دریافت نشد.", reply_markup=get_admin_keyboard())
                return

            text_content = ""
            if update.message.document:
                file_name = update.message.document.file_name.lower()
                if not file_name.endswith('.txt'):
                    await msg.edit_text("❌ فرمت فایل پروکسی باید `.txt` باشد.")
                    return
                file = await update.message.document.get_file()
                with tempfile.NamedTemporaryFile(delete=False) as temp_file:
                    await file.download_to_drive(temp_file.name)
                    with open(temp_file.name, 'r', encoding='utf-8') as f:
                        text_content = f.read()
            else:
                text_content = update.message.text
                
            proxies = []
            for line in text_content.split('\n'):
                p = parse_proxy_line(line)
                if p:
                    proxies.append(p)
                    
            if proxies:
                await redis_client.set("settings:proxies", json.dumps(proxies))
                context.user_data['admin_state'] = None
                await msg.edit_text(f"✅ تعداد <b>{len(proxies)}</b> پروکسی با موفقیت ذخیره شد.", reply_markup=get_admin_keyboard(), parse_mode='HTML')
            else:
                await msg.edit_text("⚠️ متنی حاوی پروکسی یافت نشد. لطفاً مجدداً امتحان کنید.")
                
        except Exception as e:
            logging.error(f"Error reading proxies: {e}")
            await msg.edit_text("❌ خطا در پردازش فایل یا متن پروکسی.")

# ==========================================
# توابع لاگین کاربر 
# ==========================================
def get_user_headers(context: ContextTypes.DEFAULT_TYPE):
    if 'device_id' not in context.user_data:
        context.user_data['device_id'] = str(uuid.uuid4())
        context.user_data['session_id'] = str(uuid.uuid4())
    headers = {
        'accept': 'application/json, text/plain, */*',
        'source': 'okala',
        'ui-version': '2.0',
        'origin': 'https://www.okala.com',
        'User-Agent': random.choice(USER_AGENTS)
    }
    headers['X-User-Unique-Id'] = context.user_data['device_id']
    headers['session-id'] = context.user_data['session_id']
    return headers

async def async_request(method, url, **kwargs):
    loop = asyncio.get_running_loop()
    if method.upper() == 'POST': return await loop.run_in_executor(executor, lambda: requests.post(url, **kwargs))
    return await loop.run_in_executor(executor, lambda: requests.get(url, **kwargs))

async def check_maintenance(update: Update) -> bool:
    maint = await redis_client.get("settings:maintenance")
    user_id = update.effective_user.id if update.effective_user else 0
    if maint == "1" and not is_admin(user_id):
        text = "⛔️ سیستم در حال حاضر موقتاً غیرفعال است."
        if update.message: await update.message.reply_text(text)
        else: await update.callback_query.message.reply_text(text)
        return True
    return False

async def start_login_process(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await check_maintenance(update): return ConversationHandler.END
    await update.callback_query.answer()
    
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ کنسل عملیات", callback_data="cancel_action")]])
    await update.callback_query.edit_message_text("📱 <b>لطفاً شماره موبایل خود را وارد کنید:</b>", reply_markup=kb, parse_mode='HTML')
    return PHONE

async def cancel_process_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer("عملیات لغو شد ❌")
    await show_main_menu(update, context) 
    return ConversationHandler.END

async def request_otp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await check_maintenance(update): return ConversationHandler.END
    phone = update.message.text.strip()
    
    existing_link = await redis_client.get(f"phone_active_link:{phone}")
    if existing_link:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 بازگشت به منوی اصلی", callback_data="main_menu")]
        ])
        await update.message.reply_text(
            f"⚠️ <b>خطا: شماره تکراری!</b>\n\n"
            f"برای شماره <code>{phone}</code> از قبل یک لینک فعال در سیستم وجود دارد:\n"
            f"🔗 {existing_link}\n\n"
            f"تا زمانی که لینک قبلی منقضی نشود، نمی‌توانید لینک جدیدی برای این شماره بسازید.",
            reply_markup=kb,
            parse_mode='HTML'
        )
        return ConversationHandler.END
    
    context.user_data['phone'] = phone
    
    url = "https://apigateway.okala.com/api/voyager/C/CustomerAccount/OTPRegister"
    payload = {"mobile": phone, "deviceTypeCode": 7, "confirmTerms": True, "notRobot": False, "otpType": 0, "ValidationCodeCreateReason": 5, "OtpApp": 0, "IsAppOnly": False}
    response = await async_request('POST', url, json=payload, headers=get_user_headers(context), timeout=15)
    
    if response.status_code == 200:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 ارسال مجدد کد ورود", callback_data="resend_otp")],
            [InlineKeyboardButton("❌ کنسل عملیات", callback_data="cancel_action")]
        ])
        await update.message.reply_text("✉️ <b>کد تایید ارسال شد.</b>\nلطفاً آن را وارد کنید:", reply_markup=kb, parse_mode='HTML')
        return OTP
    else:
        await update.message.reply_text(f"❌ خطا در ارتباط با سیستم: <code>{response.status_code}</code>", parse_mode='HTML')
        return ConversationHandler.END

async def resend_otp_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    phone = context.user_data.get('phone')
    await query.answer("در حال ارسال مجدد کد... ⏳")
    
    url = "https://apigateway.okala.com/api/voyager/C/CustomerAccount/OTPRegister"
    payload = {"mobile": phone, "deviceTypeCode": 7, "confirmTerms": True, "notRobot": False, "otpType": 0, "ValidationCodeCreateReason": 5, "OtpApp": 0, "IsAppOnly": False}
    response = await async_request('POST', url, json=payload, headers=get_user_headers(context), timeout=15)
    
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 ارسال مجدد کد ورود", callback_data="resend_otp")],
        [InlineKeyboardButton("❌ کنسل عملیات", callback_data="cancel_action")]
    ])
    
    if response.status_code == 200:
        await query.edit_message_text(f"✉️ <b>کد تایید مجدداً به {phone} ارسال شد.</b>\nلطفاً کد جدید را وارد کنید:", reply_markup=kb, parse_mode='HTML')
    else:
        await query.edit_message_text(f"❌ خطا در ارسال مجدد: <code>{response.status_code}</code>", reply_markup=kb, parse_mode='HTML')
    return OTP 

async def verify_otp_and_check_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    otp_code = update.message.text.strip()
    phone = context.user_data.get('phone')
    msg = await update.message.reply_text("⏳ در حال پردازش درخواست...")
    
    token_url = "https://apigateway.okala.com/api/v1/accounts/tokens"
    payload = {"mobile_number": phone, "otp_code": otp_code, "grant_type": "customer_grant_type", "client_id": "customer_client_id", "client_secret": "u_M{'57j!%LI21#", "client_name": "customer_client_name", "device_type_code": 7, "scope": "offline_access", "loginDuration": 4815}
    headers = get_user_headers(context)
    headers["Content-Type"] = "application/x-www-form-urlencoded"
    
    response = await async_request('POST', token_url, data=payload, headers=headers)
    
    if response.status_code == 200:
        auth_data = response.json()
        context.user_data['auth_data'] = auth_data 
        if auth_data.get("access_token"):
            await redis_client.hset(f"account:{phone}", mapping={"access_token": auth_data.get("access_token"), "refresh_token": auth_data.get("refresh_token")})
            
        if not auth_data.get("UserInfo", {}).get("HasName", False):
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ کنسل عملیات", callback_data="cancel_action")]])
            await msg.edit_text("⚠️ <b>اطلاعات حساب ناقص است.</b>\nلطفاً نام و نام خانوادگی خود را وارد کنید:", reply_markup=kb, parse_mode='HTML')
            return ASK_NAME
        else:
            return await generate_and_send_link(update, context, msg)
    else:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 ارسال مجدد کد ورود", callback_data="resend_otp")],
            [InlineKeyboardButton("❌ کنسل عملیات", callback_data="cancel_action")]
        ])
        await msg.edit_text("❌ کد وارد شده اشتباه یا منقضی است.\nلطفاً مجدداً تلاش کنید.", reply_markup=kb)
        return OTP 

async def save_name_and_continue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    full_name = update.message.text.strip()
    if not full_name: return ASK_NAME
    parts = full_name.split(maxsplit=1)
    msg = await update.message.reply_text("⏳ در حال ثبت اطلاعات...")
    
    url = "https://apigateway.okala.com/api/voyager/C/CustomerAccount/UpdateCustomer" 
    headers = get_user_headers(context)
    headers["Authorization"] = f"Bearer {context.user_data['auth_data'].get('access_token')}"
    payload = {"birthDate": "", "birthDateEpoch": 700086600, "customerType": 0, "firstName": parts[0], "genderCode": 1, "genderTitle": "مذکر", "lastName": parts[1] if len(parts)>1 else "", "gender": "male"}
    
    await async_request('POST', url, json=payload, headers=headers)
    return await generate_and_send_link(update, context, msg)

async def generate_and_send_link(update: Update, context: ContextTypes.DEFAULT_TYPE, status_msg) -> int:
    auth_data = context.user_data.get('auth_data')
    phone = context.user_data.get('phone', 'نامشخص')
    injection_json = format_for_injector(auth_data)
    link_id = str(uuid.uuid4())[:12]
    
    expire_time = await redis_client.get("settings:expire_time")
    expire_time = int(expire_time) if expire_time else 7200
    await redis_client.setex(f"acc_link:{link_id}", expire_time, json.dumps(injection_json, ensure_ascii=False))
    
    final_url = f"{WEB_DOMAIN}/acc/{link_id}"
    
    await redis_client.setex(f"phone_active_link:{phone}", expire_time, final_url)
    
    if 'session_links' not in context.user_data:
        context.user_data['session_links'] = []
    context.user_data['session_links'].append({"phone": phone, "link": final_url})
    
    tg_user = update.effective_user
    log_entry = {
        "tg_id": tg_user.id,
        "tg_name": tg_user.full_name or "نامشخص",
        "tg_user": tg_user.username or "",
        "phone": phone,
        "link": final_url,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S")
    }
    await redis_client.rpush("global_link_logs", json.dumps(log_entry, ensure_ascii=False))
    
    count = len(context.user_data['session_links'])
    
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ ساخت لینک برای یک خط دیگر", callback_data="user_login")],
        [InlineKeyboardButton("🏁 پایان لینک ساختن", callback_data="finish_link_creation")],
        [InlineKeyboardButton("🔙 بازگشت به منوی اصلی", callback_data="main_menu")]
    ])
    
    text = (
        f"✅ <b>ورود به حساب شماره {phone} با موفقیت انجام شد.</b>\n\n"
        f"📥 لینک تولید شد و آماده تحویل است.\n"
        f"📊 تعداد لینک‌های آماده ارسال در این نوبت: <b>{count}</b>\n\n"
        "می‌توانید شماره دیگری اضافه کنید یا دکمه <b>«🏁 پایان لینک ساختن»</b> را بزنید."
    )
    await status_msg.edit_text(text, reply_markup=kb, parse_mode='HTML')
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("❌ عملیات متوقف شد.")
    await show_main_menu(update, context)
    return ConversationHandler.END

# ==========================================
# راه‌اندازی اصلی
# ==========================================
async def main():
    BOT_TOKEN = os.environ.get("BOT_TOKEN")
    if not BOT_TOKEN:
        logging.error("BOT_TOKEN environment variable is missing.")
        return

    await start_web_server()

    application = Application.builder().token(BOT_TOKEN).build()
    
    application.add_handler(CommandHandler('start', show_main_menu))
    application.add_handler(CommandHandler('admin', admin_command))
    application.add_handler(CommandHandler('block', block_command))
    application.add_handler(CommandHandler('unblock', unblock_command))
    application.add_handler(CommandHandler('blocklist', blocklist_command))
    
    application.add_handler(MessageHandler(filters.Document.FileExtension("zip"), handle_zip_upload))
    
    conv_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(start_login_process, pattern="^user_login$"),
            CallbackQueryHandler(ask_tag_name, pattern="^set_tag$"),
            CallbackQueryHandler(ask_search_query, pattern="^search_links$"),
            CallbackQueryHandler(ask_user_links_for_discount, pattern="^check_user_links$")
        ],
        states={
            PHONE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, request_otp),
            ],
            OTP: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, verify_otp_and_check_name),
                CallbackQueryHandler(resend_otp_callback, pattern="^resend_otp$"),
            ],
            ASK_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_name_and_continue),
            ],
            ASK_TAG: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_tag_name),
                CallbackQueryHandler(clear_active_tag_callback, pattern="^clear_active_tag$")
            ],
            ASK_SEARCH: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_search_query)
            ],
            ASK_LINKS_FOR_DISCOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, process_user_links_discount)
            ]
        },
        fallbacks=[
            CommandHandler('cancel', cancel),
            CallbackQueryHandler(cancel_process_callback, pattern="^cancel_action$")
        ]
    )
    application.add_handler(conv_handler)
    
    application.add_handler(CallbackQueryHandler(core_callback, pattern="^admin_|^set_exp_|^main_menu$|^admin_panel$|^finish_link_creation$|^my_tags$|^show_tag_|^contact_admin$|^approve_discount_|^deny_discount_"))
    
    application.add_handler(MessageHandler(filters.TEXT | filters.Document.FileExtension("txt"), handle_admin_text_document))

    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    
    logging.info("System initialized successfully.")
    stop_signal = asyncio.Event()
    await stop_signal.wait()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
