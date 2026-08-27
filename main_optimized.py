import asyncio
from concurrent.futures import ThreadPoolExecutor, as_completed
import time

# ThreadPoolExecutor بزرگ‌تر برای parallelization
executor = ThreadPoolExecutor(max_workers=20)  # از 5 به 20

# ==========================================
# بهینه‌سازی: parallel discount checking
# ==========================================
async def process_discounts_and_send_report_optimized(bot, chat_id, acc_keys):
    loop = asyncio.get_running_loop()
    api = OkalaAPI()
    ts = int(time.time())

    proxy_check = await get_random_proxy_from_db()
    if not proxy_check:
        await bot.send_message(
            chat_id=chat_id,
            text="⚠️ <b>هیچ پروکسی‌ای در سیستم تنظیم نشده است!</b>",
            parse_mode='HTML'
        )

    # بارگیری تمام لاگ‌ها یکبار
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
        text=f"🔍 شروع بررسی <b>{total}</b> اکانت با پروکسی...",
        parse_mode='HTML'
    )

    detail_logs = []
    discount_results = []
    done = 0
    lock = asyncio.Lock()

    def _check_sync(acc_token, ref_token, uid, p_dict, phone, attempt=0):
        """تک درخواست بدون تأخیر random"""
        try:
            status, res = api.check_discount_api(acc_token, uid, proxy_dict=p_dict)

            if status == 401 and ref_token and attempt < 1:
                new_acc, new_ref = api.refresh_token(ref_token, proxy_dict=p_dict)
                if new_acc:
                    status, res = api.check_discount_api(new_acc, uid, proxy_dict=p_dict)
                    return status, res, new_acc, new_ref
            
            return status, res, None, None
        except Exception as e:
            return 0, str(e), None, None

    async def process_single_account(key):
        """پردازش یک اکانت به صورت async"""
        nonlocal done
        
        try:
            phone = key.replace("account:", "")
            token_data = await redis_client.hgetall(key)
            access_token = token_data.get("access_token")
            refresh_token = token_data.get("refresh_token")

            if not access_token:
                log_line = f"⚠️ {phone} — توکن موجود نیست\n"
                async with lock:
                    detail_logs.append(log_line)
                    done += 1
                return

            user_uuid = get_user_id_from_token(access_token)
            if not user_uuid:
                log_line = f"⚠️ {phone} — UUID قابل استخراج نیست\n"
                async with lock:
                    detail_logs.append(log_line)
                    done += 1
                return

            proxy_dict = await get_random_proxy_from_db()
            
            # بدون time.sleep()
            status, res, new_acc, new_ref = await loop.run_in_executor(
                executor, _check_sync, access_token, refresh_token, user_uuid, proxy_dict, phone
            )

            log_line = f"[{time.strftime('%H:%M:%S')}] 📱 {phone}"
            
            if new_acc:
                await redis_client.hset(key, mapping={
                    "access_token": new_acc, 
                    "refresh_token": new_ref or ""
                })
                log_line += " ♻️ رفرش موفق"

            if status == 200 and isinstance(res, dict):
                vouchers = res.get('data', [])
                if vouchers:
                    amounts = [v.get('discountAmount', 0) for v in vouchers if v.get('discountAmount')]
                    max_amount = max(amounts) // 10000 if amounts else 0
                    log_line += f" | 🎁 {len(vouchers)} کد | {max_amount} هزار"
                    
                    async with lock:
                        discount_results.append({
                            "phone": phone,
                            "count": len(vouchers),
                            "max_amount": max_amount,
                            "link": phone_to_latest_link.get(phone, "")
                        })
                else:
                    log_line += " | ➖ بدون تخفیف"
            elif status == 401:
                log_line += " | 🔒 توکن منقضی"
            else:
                log_line += f" | ❌ خطا {status}"

            log_line += "\n"
            async with lock:
                detail_logs.append(log_line)
                done += 1

        except Exception as e:
            async with lock:
                detail_logs.append(f"❌ {key}: {e}\n")
                done += 1

    # اجرای parallel تمام اکانت‌ها
    tasks = [process_single_account(key) for key in acc_keys]
    
    # بروزرسانی progress هر 10 اکانت
    for i in range(0, total, 10):
        await asyncio.gather(*tasks[i:min(i+10, total)])
        try:
            await progress_msg.edit_text(
                f"✅ {done}/{total} | 🎁 تخفیف‌ها: {len(discount_results)}",
                parse_mode='HTML'
            )
        except:
            pass

    # بقیه tasks
    if total % 10 != 0:
        await asyncio.gather(*tasks[total - (total % 10):])

    # ارسال گزارش‌ها
    try:
        await progress_msg.delete()
    except:
        pass

    report_text = f"🎁 <b>گزارش ({len(discount_results)}/{total}):</b>\n\n"
    for r in discount_results:
        report_text += f"📱 {r['phone']} | {r['count']} کد | {r['max_amount']} هزار\n"

    report_out = io.BytesIO(report_text.encode('utf-8'))
    await bot.send_document(
        chat_id=chat_id,
        document=report_out,
        filename=f"Discounts_{ts}.txt",
        caption=f"✅ {len(discount_results)} اکانت دارای تخفیف"
    )


# ==========================================
# بهینه‌سازی: ZIP discount check به صورت parallel
# ==========================================
async def handle_zip_upload_optimized(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    file_name = update.message.document.file_name.lower()
    if not file_name.endswith('.zip'):
        await update.message.reply_text("❌ فایل ZIP الزامی است.")
        return

    action = context.user_data.get('admin_zip_action', 'zip_to_link')
    msg = await update.message.reply_text("⏳ دریافت و استخراج...")

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
            await msg.edit_text("⚠️ هیچ JSON یافت نشد.")
            return

        loop = asyncio.get_running_loop()
        api = OkalaAPI()

        if action == 'zip_to_link':
            # پردازش موازی فایل‌ها
            async def process_file_to_link(file_path):
                try:
                    filename = os.path.basename(file_path)
                    phone = filename.replace('.json', '')

                    existing_link = await redis_client.get(f"phone_active_link:{phone}")
                    if existing_link:
                        return phone, None, "تکراری"

                    with open(file_path, 'r', encoding='utf-8') as f:
                        data = json.loads(f.read())
                        access_token = None
                        refresh_token = None

                        for cookie in data.get('cookies', []):
                            if cookie.get('name') == 'tokenMS':
                                access_token = cookie.get('value')
                            elif cookie.get('name') == 'refresh_token':
                                refresh_token = cookie.get('value')

                        if not access_token:
                            for origin in data.get('origins', []):
                                for item in origin.get('localStorage', []):
                                    if item.get('name') == 'tokenMS':
                                        access_token = item.get('value')
                                    elif item.get('name') == 'refresh_token':
                                        refresh_token = item.get('value')

                        if access_token and not await redis_client.exists(f"account:{phone}"):
                            await redis_client.hset(f"account:{phone}", 
                                mapping={"access_token": access_token, "refresh_token": refresh_token or ""})

                        link_id = str(uuid.uuid4())[:12]
                        await redis_client.setex(f"acc_link:{link_id}", expire_time, json.dumps(data, ensure_ascii=False))
                        final_url = f"{WEB_DOMAIN}/acc/{link_id}"
                        await redis_client.setex(f"phone_active_link:{phone}", expire_time, final_url)

                        return phone, final_url, "✅"
                except Exception as e:
                    return None, None, f"❌ {str(e)}"

            # اجرای parallel
            results = await asyncio.gather(*[
                process_file_to_link(fp) for fp in json_files_paths
            ])

            links_text = "<b>نتایج:</b>\n\n"
            count = 0
            for phone, link, status in results:
                if status == "✅":
                    links_text += f"✅ {phone}\n{link}\n\n"
                    count += 1
                else:
                    links_text += f"{status} {phone}\n\n"

            file_out = io.BytesIO(links_text.encode('utf-8'))
            await context.bot.send_document(chat_id=user_id, document=file_out,
                filename=f"Links_{int(time.time())}.txt",
                caption=f"✅ {count} لینک ساخته شد")
            await msg.delete()

        elif action == 'zip_discount_check':
            await msg.edit_text("🔍 بررسی موازی تخفیف‌ها...")

            async def check_discount_for_file(file_path):
                try:
                    filename = os.path.basename(file_path)
                    phone = filename.replace('.json', '')

                    with open(file_path, 'r', encoding='utf-8') as f:
                        data = json.loads(f.read())
                        access_token = None
                        refresh_token = None

                        for cookie in data.get('cookies', []):
                            if cookie.get('name') == 'tokenMS':
                                access_token = cookie.get('value')
                            elif cookie.get('name') == 'refresh_token':
                                refresh_token = cookie.get('value')

                        if not access_token:
                            for origin in data.get('origins', []):
                                for item in origin.get('localStorage', []):
                                    if item.get('name') == 'tokenMS':
                                        access_token = item.get('value')
                                    elif item.get('name') == 'refresh_token':
                                        refresh_token = item.get('value')

                        if not access_token:
                            return phone, None, "❌ توکن یافت نشد"

                        user_uuid = get_user_id_from_token(access_token)
                        if not user_uuid:
                            return phone, None, "❌ UUID نامعتبر"

                        proxy_dict = await get_random_proxy_from_db()

                        def _check():
                            status, res = api.check_discount_api(access_token, user_uuid, proxy_dict)
                            if status == 401 and refresh_token:
                                new_acc, new_ref = api.refresh_token(refresh_token, proxy_dict)
                                if new_acc:
                                    return api.check_discount_api(new_acc, user_uuid, proxy_dict)
                            return status, res

                        status, res = await loop.run_in_executor(executor, _check)

                        if status == 200 and isinstance(res, dict):
                            vouchers = res.get('data', [])
                            if vouchers:
                                return phone, file_path, "✅"
                            else:
                                return phone, None, "➖"
                        else:
                            return phone, None, f"❌ {status}"

                except Exception as e:
                    return phone, None, f"❌ {str(e)}"

            # اجرای parallel
            results = await asyncio.gather(*[
                check_discount_for_file(fp) for fp in json_files_paths
            ])

            discount_files = [r for r in results if r[1] is not None]
            report_text = f"🎁 <b>نتایج ({len(discount_files)}):</b>\n\n"

            for phone, _, status in results:
                report_text += f"{status} {phone}\n"

            report_out = io.BytesIO(report_text.encode('utf-8'))
            await context.bot.send_document(chat_id=user_id, document=report_out,
                filename=f"Discount_Report_{int(time.time())}.txt",
                caption=f"🎁 {len(discount_files)} اکانت دارای تخفیف")

            await msg.delete()


# ==========================================
# بهینه‌سازی: OkalaAPI - بدون تأخیر‌های random
# ==========================================
class OkalaAPI:
    def __init__(self):
        self.request_logs = []

    def check_discount_api(self, token, uid, proxy_dict=None):
        url = f"https://apigateway.okala.com/api/discount/v1/discounts/customer/{uid}"
        headers = {
            'Authorization': f'Bearer {token}',
            'Accept': 'application/json, text/plain, */*',
            'source': 'okala',
            'User-Agent': random.choice(USER_AGENTS)
        }
        
        for attempt in range(2):  # تقلیل تلاش‌ها
            try:
                res = requests.get(url, headers=headers, proxies=proxy_dict, timeout=30)  # timeout کاهش
                
                if res.status_code == 200:
                    try:
                        return 200, res.json()
                    except:
                        return 200, {}
                elif res.status_code == 401:
                    return 401, {}
                else:
                    return res.status_code, res.text
                    
            except Exception as e:
                if attempt == 1:
                    return 0, str(e)
                # بدون time.sleep()
        
        return 0, "Network Error"

    def refresh_token(self, refresh_token, proxy_dict=None):
        url = "https://apigateway.okala.com/api/v1/accounts/tokens"
        payload = {
            "grant_type": "refresh_token",
            "client_id": "customer_client_id",
            "client_secret": "u_M{'57j!%LI21#",
            "refresh_token": refresh_token
        }
        headers = {
            "content-type": "application/x-www-form-urlencoded",
            "User-Agent": random.choice(USER_AGENTS)
        }
        
        for attempt in range(2):  # تقلیل تلاش‌ها
            try:
                res = requests.post(url, data=payload, headers=headers, proxies=proxy_dict, timeout=30)
                
                if res.status_code == 200:
                    data = res.json()
                    return data.get('access_token'), data.get('refresh_token')
                    
            except Exception as e:
                if attempt == 1:
                    return None, None
                # بدون time.sleep()
        
        return None, None


# ==========================================
# بهینه‌سازی: batch Redis operations
# ==========================================
async def batch_save_accounts(accounts_dict):
    """ذخیره چندین اکانت به صورت batch"""
    pipe = await redis_client.pipeline()
    for phone, data in accounts_dict.items():
        await pipe.hset(f"account:{phone}", mapping=data)
    await pipe.execute()

async def batch_get_accounts(phone_list):
    """بارگیری چندین اکانت به صورت batch"""
    pipe = await redis_client.pipeline()
    for phone in phone_list:
        await pipe.hgetall(f"account:{phone}")
    return await pipe.execute()
