import os
import threading
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timedelta, timezone
from supabase import create_client, Client
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

SUPABASE_URL = "https://gxqztvcwamchihnqplin.supabase.co"
SUPABASE_KEY = "sb_publishable_lnQnwygZZvi6orL46p9okA_gO0irVDQ"
TOKEN = "8944966971:AAF2MAuzAEIlkkr-16wc7iTz4SxYtYWMxSU"

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

GROUP_CHAT_ID = None
BREAK_TYPES = {"ห้องน้ำ": 20, "ดูดบุหรี่": 10, "กินข้าว": 40, "ซื้อของ": 40, "ซื้อof": 40}
RETURN_COMMANDS = ["กลับ", "มาค่ะ", "มาครับ", "เข้า"]

TH_TIMEZONE = timezone(timedelta(hours=7))

def run_dummy_server():
    port = int(os.environ.get("PORT", 10000))
    class SimpleHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Bot is alive!")
            
        def do_HEAD(self):
            self.send_response(200)
            self.end_headers()

        def log_message(self, format, *args):
            pass
            
    server = HTTPServer(("0.0.0.0", port), SimpleHandler)
    server.serve_forever()

def get_personal_summary_text(emp_id, current_date):
    date_obj = datetime.strptime(current_date, "%Y-%m-%d")
    date_str = date_obj.strftime("%d/%m/%y")

    data_res = supabase.table("employee_data").select("*").eq("emp_id", emp_id).eq("work_date", current_date).execute()
    if not data_res.data:
        return f"❌ ยังไม่มีข้อมูลการเบรคของรหัส {emp_id} ในวันที่ {date_str}"
    
    d = data_res.data[0]
    quota_total = d["quota_total"]
    quota_used = d["quota_used"]
    quota_left = quota_total - quota_used
    meal_used = d["meal_used"]
    
    hist_res = supabase.table("break_history").select("*").eq("emp_id", emp_id).eq("work_date", current_date).execute()
    
    break_summary = {
        "กินข้าว/ซื้อของ": {"count": meal_used, "mins": 0},
        "ห้องน้ำ": {"count": 0, "mins": 0},
        "ดูดบุหรี่": {"count": 0, "mins": 0}
    }
    
    violations_map = {}
    
    if hist_res.data:
        for h in hist_res.data:
            b_type = h["break_type"]
            b_mins = h["used_mins"]
            if b_type == "ซื้อof":
                b_type = "ซื้อของ"
                
            if b_type == "ห้องน้ำ":
                group_key = "ห้องน้ำ"
                allowed_limit = BREAK_TYPES["ห้องน้ำ"]
            elif b_type == "ดูดบุหรี่":
                group_key = "ดูดบุหรี่"
                allowed_limit = BREAK_TYPES["ดูดบุหรี่"]
            else:
                group_key = "กินข้าว/ซื้อของ"
                allowed_limit = BREAK_TYPES["กินข้าว"]

            if group_key != "กินข้าว/ซื้อของ":
                break_summary[group_key]["count"] += 1
            break_summary[group_key]["mins"] += b_mins

            if b_mins > allowed_limit:
                over = b_mins - allowed_limit
                if group_key not in violations_map:
                    violations_map[group_key] = {"over_mins": 0, "count": 0}
                violations_map[group_key]["over_mins"] += over
                violations_map[group_key]["count"] += 1

    history_lines = [
        f"🍽 กินข้าว/ซื้อของ: {break_summary['กินข้าว/ซื้อของ']['count']} / 2 ครั้ง / {break_summary['กินข้าว/ซื้อของ']['mins']} นาที",
        f"🚻 ห้องน้ำ: {break_summary['ห้องน้ำ']['count']} ครั้ง / {break_summary['ห้องน้ำ']['mins']} นาที",
        f"🚬 ดูดบุหรี่: {break_summary['ดูดบุหรี่']['count']} ครั้ง / {break_summary['ดูดบุหรี่']['mins']} นาที"
    ]
    history_str = "\n".join(history_lines)

    status_parts = []
    if quota_used > quota_total:
        status_parts.append(f"• ใช้โควตารวมเกินกำหนดไป {quota_used - quota_total} นาที")
    
    for group_key, v in violations_map.items():
        status_parts.append(f"• {group_key} เกินกำหนดรวม {v['over_mins']} นาที (เกิน {v['count']} ครั้ง)")

    if not status_parts and quota_used <= quota_total:
        status_text = "ปกติ / เป็นไปตามระเบียบของบริษัท"
    else:
        status_text = "ผิดปกติ /\n" + "\n".join(status_parts)

    report = (
        f"📝 **ใบสรุปประวัติการใช้สิทธิ์หักเบรค ({date_str})**\n"
        f"----------------------------------------\n"
        f"🔹 รหัสพนักงาน: `{emp_id}` 🔹 ประจำกะ: รอบกะดึก (B)\n"
        f"----------------------------------------\n"
        f"⏱️ **สรุปเวลา:**\n"
        f"• โควตาตั้งต้น: {quota_total} นาที\n"
        f"• ใช้ไปทั้งหมด: {quota_used} นาที\n"
        f"• โควตาคงเหลือสุทธิ: {quota_left} นาที\n\n"
        f"📁 **ประวัติการทำรายการ:**\n{history_str}\n"
        f"----------------------------------------\n"
        f"สถานะ: {status_text}"
    )
    return report

def run_background_tasks():
    global GROUP_CHAT_ID
    sent_today = None
    notified_overtime = set()

    while True:
        try:
            now = datetime.now(TH_TIMEZONE)
            current_date = (now - timedelta(days=1)).strftime("%Y-%m-%d") if now.hour < 5 else now.strftime("%Y-%m-%d")
            
            if GROUP_CHAT_ID:
                active_res = supabase.table("active_breaks").select("*").execute()
                if active_res.data:
                    for info in active_res.data:
                        emp_id = info["emp_id"]
                        start_time = datetime.fromisoformat(info["start_time"])
                        allowed_mins = info["allowed_mins"]
                        break_type = info["break_type"]
                        
                        elapsed_seconds = (now - start_time).total_seconds()
                        elapsed_mins = elapsed_seconds / 60
                        
                        if elapsed_mins > allowed_mins and emp_id not in notified_overtime:
                            over_mins = int(elapsed_mins - allowed_mins)
                            alert_msg = (
                                f"🚨 **แจ้งเตือน! พนักงานเบรคเกินเวลา** 🚨\n"
                                f"----------------------------------------\n"
                                f"🔹 รหัสพนักงาน: `{emp_id}`\n"
                                f"• ประเภทเบรค: {break_type} (กำหนด {allowed_mins} นาที)\n"
                                f"• เกินเวลามาแล้ว: ประมาณ {over_mins} นาที\n"
                                f"⚠️ กรุณากลับเข้าทำงานหรือรายงานตัวด่วน!"
                            )
                            url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
                            requests.post(url, json={"chat_id": GROUP_CHAT_ID, "text": alert_msg, "parse_mode": "Markdown"})
                            notified_overtime.add(emp_id)

                active_emp_ids = {i["emp_id"] for i in (active_res.data or [])}
                for emp_id in list(notified_overtime):
                    if emp_id not in active_emp_ids:
                        notified_overtime.remove(emp_id)

            if now.hour == 5 and now.minute == 0:
                if sent_today != current_date and GROUP_CHAT_ID:
                    emp_res = supabase.table("employee_data").select("*").eq("work_date", current_date).execute()
                    
                    if not emp_res.data:
                        summary_text = f"📊 **สรุปยอดกะดึกประจำวัน ({current_date})**\n----------------------------------------\n❌ ยังไม่มีข้อมูลการเบรคในวันนี้"
                    else:
                        summary_text = f"📊 **สรุปยอดกะดึกประจำวัน ({current_date}) [สรุปอัตโนมัติสิ้นสุดกะ]**\n----------------------------------------\n"
                        for d in emp_res.data:
                            summary_text += get_personal_summary_text(d['emp_id'], current_date) + "\n\n========================================\n"
                    
                    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
                    requests.post(url, json={"chat_id": GROUP_CHAT_ID, "text": summary_text, "parse_mode": "Markdown"})
                    sent_today = current_date
            
            threading.Event().wait(15)
        except Exception as e:
            print("Error in background tasks thread:", e)
            threading.Event().wait(15)

def get_work_date():
    now = datetime.now(TH_TIMEZONE)
    if now.hour < 5:
        return (now - timedelta(days=1)).strftime("%Y-%m-%d")
    return now.strftime("%Y-%m-%d")

def get_or_create_employee(emp_id, work_date):
    res = supabase.table("employee_data").select("*").eq("emp_id", emp_id).eq("work_date", work_date).execute()
    if not res.data:
        new_data = {
            "emp_id": emp_id, 
            "work_date": work_date, 
            "shift": "กะดึก (ก(B))", 
            "quota_total": 90, 
            "quota_used": 0, 
            "meal_used": 0, 
            "meal_total": 2
        }
        supabase.table("employee_data").insert(new_data).execute()
        return new_data
    return res.data[0]

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global GROUP_CHAT_ID
    if not update.message or not update.message.text: 
        return
    GROUP_CHAT_ID = update.message.chat_id
    text = update.message.text.strip()
    
    try:
        current_date = get_work_date()

        if text == "สรุป":
            emp_res = supabase.table("employee_data").select("*").eq("work_date", current_date).execute()
            if not emp_res.data:
                await update.message.reply_text(f"📊 **สรุปยอดกะดึก ({current_date})**\n----------------------------------------\n❌ ยังไม่มีข้อมูลการเบรค", parse_mode="Markdown")
                return
            
            for d in emp_res.data:
                report = get_personal_summary_text(d['emp_id'], current_date)
                await update.message.reply_text(report, parse_mode="Markdown")
            return

        parts = text.split()
        if len(parts) < 2: 
            return
        emp_id, action = parts[0], parts[1]
        data = get_or_create_employee(emp_id, current_date)

        if action == "เช็ค":
            quota_left = data["quota_total"] - data["quota_used"]
            check_msg = (
                f"📊 รายงานสถานะเบรค\n"
                f"👤 รหัสพนักงาน: {emp_id}\n"
                f"⏰ ช่วงเวลา: กะดึก (B)\n"
                f"⏳ โควตาเวลาคงเหลือ: {quota_left} / {data['quota_total']} นาที\n"
                f"🍽️ สิทธิ์กินข้าว/ซื้อของ: {data['meal_used']} / 2 ครั้ง"
            )
            await update.message.reply_text(check_msg)
            return

        if action == "สรุป":
            report = get_personal_summary_text(emp_id, current_date)
            await update.message.reply_text(report, parse_mode="Markdown")
            return

        if action in RETURN_COMMANDS:
            active_res = supabase.table("active_breaks").select("*").eq("emp_id", emp_id).execute()
            if not active_res.data:
                await update.message.reply_text(f"รหัส {emp_id} ยังไม่ได้เริ่มเบรค")
                return
            info = active_res.data[0]
            start_time = datetime.fromisoformat(info["start_time"])
            now_time = datetime.now(TH_TIMEZONE)
            
            total_seconds = int((now_time - start_time).total_seconds())
            mins = total_seconds // 60
            secs = total_seconds % 60
            elapsed_text = f"{mins} นาที {secs} วินาที" if mins > 0 else f"{secs} วินาที"
            elapsed_mins_for_quota = max(1, mins if secs == 0 else mins + 1)
            
            new_used = data["quota_used"] + elapsed_mins_for_quota
            
            supabase.table("employee_data").update({"quota_used": new_used}).eq("emp_id", emp_id).eq("work_date", current_date).execute()
            supabase.table("break_history").insert({
                "emp_id": emp_id, 
                "work_date": current_date, 
                "break_type": info["break_type"], 
                "used_mins": elapsed_mins_for_quota, 
                "time_range": f"{start_time.strftime('%H:%M')} - {now_time.strftime('%H:%M')}"
            }).execute()
            supabase.table("active_breaks").delete().eq("emp_id", emp_id).execute()
            
            allowed = info["allowed_mins"]
            overtime_line = ""
            if elapsed_mins_for_quota > allowed:
                over_mins = elapsed_mins_for_quota - allowed
                overtime_line = f"⚠️ เกินเวลาไป {over_mins} นาที\n"
            
            return_msg = (
                f"🏁 รหัส {emp_id} กลับเข้าทำงานแล้ว ({info['break_type']})\n"
                f"• เวลาเข้า: {now_time.strftime('%H:%M:%S')} น.\n"
                f"• ใช้เวลาครั้งนี้: {elapsed_text} (กำหนด {allowed} นาที)\n"
                f"{overtime_line}"
                f"📊 โควตาคงเหลือ (กะดึก (B)): {data['quota_total'] - new_used} นาที"
            )
            await update.message.reply_text(return_msg)
            return

        if action in BREAK_TYPES:
            active_res = supabase.table("active_breaks").select("*").eq("emp_id", emp_id).execute()
            if active_res.data:
                await update.message.reply_text(f"รหัส {emp_id} กำลังเบรคอยู่")
                return
            dur = BREAK_TYPES[action]
            meal_used = data["meal_used"]
            if action in ["กินข้าว", "ซื้อของ", "ซื้อof"]:
                if meal_used >= 2:
                    await update.message.reply_text(f"❌ ใช้สิทธิ์ข้าวครบ 2 ครั้งแล้วสำหรับกะดึก")
                    return
                meal_used += 1
                supabase.table("employee_data").update({"meal_used": meal_used}).eq("emp_id", emp_id).eq("work_date", current_date).execute()
            
            now_time = datetime.now(TH_TIMEZONE)
            due_time = now_time + timedelta(minutes=dur)
            
            supabase.table("active_breaks").upsert({
                "emp_id": emp_id, 
                "start_time": now_time.isoformat(), 
                "allowed_mins": dur, 
                "break_type": action
            }).execute()
            
            reply_msg = (
                f"⏳ รหัส {emp_id} เริ่มเบรค {action}\n"
                f"• เวลาที่ได้: {dur} นาที\n"
                f"• เวลาเริ่ม: {now_time.strftime('%H:%M:%S')} น.\n"
                f"• ควรกลับเข้าทำงานก่อนเวลา: {due_time.strftime('%H:%M:%S')} น.\n"
                f"📊 กะดึก (18:00 - 05:00 น.)\n"
                f"• โควตาเวลารวมคงเหลือ: {data['quota_total'] - data['quota_used']} นาที"
            )
            await update.message.reply_text(reply_msg)
            
    except Exception as e:
        print("Error handling message:", e)
        await update.message.reply_text(f"⚠️ เกิดข้อผิดพลาดในระบบ: {e}")

if __name__ == '__main__':
    server_thread = threading.Thread(target=run_dummy_server, daemon=True)
    server_thread.start()

    bg_thread = threading.Thread(target=run_background_tasks, daemon=True)
    bg_thread.start()

    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Night Shift Bot is running...")
    app.run_polling()
