import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timedelta, timezone
from supabase import create_client, Client
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

# --- ตั้งค่า Supabase และ Token บอทกะดึก ---
SUPABASE_URL = "https://gxqztvcwamchihnqplin.supabase.co"
SUPABASE_KEY = "sb_publishable_lnQnwygZZvi6orL46p9okA_gO0irVDQ"
TOKEN = "8944966971:AAF2MAuzAEIlkkr-16wc7iTz4SxYtYWMxSU"

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

GROUP_CHAT_ID = None
BREAK_TYPES = {"ห้องน้ำ": 20, "ดูดบุหรี่": 10, "กินข้าว": 40, "ซื้อของ": 40}
RETURN_COMMANDS = ["กลับ", "มาค่ะ", "มาครับ", "เข้า"]

TH_TIMEZONE = timezone(timedelta(hours=7))

def run_dummy_server():
    port = int(os.environ.get("PORT", 10000))
    class SimpleHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Bot is alive!")
        def log_message(self, format, *args):
            pass
            
    server = HTTPServer(("0.0.0.0", port), SimpleHandler)
    server.serve_forever()

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
            "shift": "กะดึก (18:00 - 05:00 น.)", 
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
    current_date = get_work_date()

    if text == "สรุป":
        summary_text = f"📊 **สรุปยอดกะดึก ({current_date})**\n----------------------------------------\n"
        emp_res = supabase.table("employee_data").select("*").eq("work_date", current_date).execute()
        active_res = supabase.table("active_breaks").select("emp_id, break_type").execute()
        active_dict = {r["emp_id"]: r["break_type"] for r in active_res.data}
        
        if not emp_res.data:
            summary_text += "❌ ยังไม่มีข้อมูลการเบรค"
        else:
            for d in emp_res.data:
                status = f"⏳ กำลังเบรค ({active_dict[d['emp_id']]})" if d['emp_id'] in active_dict else "🟢 ทำงานปกติ"
                summary_text += (
                    f"👤 รหัส: `{d['emp_id']}` | {status}\n"
                    f"• ใช้ไป: {d['quota_used']}/90 นาที | ข้าว: {d['meal_used']}/2\n"
                    f"----------------------------------------\n"
                )
        await update.message.reply_text(summary_text, parse_mode="Markdown")
        return

    parts = text.split()
    if len(parts) < 2: 
        return
    emp_id, action = parts[0], parts[1]
    data = get_or_create_employee(emp_id, current_date)
    quota_left = data["quota_total"] - data["quota_used"]

    if action == "สรุป":
        active_res = supabase.table("active_breaks").select("break_type").eq("emp_id", emp_id).execute()
        status = f"⏳ กำลังเบรค ({active_res.data[0]['break_type']})" if active_res.data else "🟢 ทำงานปกติ"
        await update.message.reply_text(
            f"📊 รหัส `{emp_id}`\nสถานะ: {status}\n⏳ โควตาเหลือ: {quota_left} นาที\n🍽️ กินข้าว: {data['meal_used']}/2 ครั้ง", 
            parse_mode="Markdown"
        )
        return

    # แจ้งกลับเข้าทำงาน (แสดงข้อมูลครบถ้วน)
    if action in RETURN_COMMANDS:
        active_res = supabase.table("active_breaks").select("*").eq("emp_id", emp_id).execute()
        if not active_res.data:
            await update.message.reply_text(f"รหัส {emp_id} ยังไม่ได้เริ่มเบรค")
            return
        info = active_res.data[0]
        start_time = datetime.fromisoformat(info["start_time"])
        now_time = datetime.now(TH_TIMEZONE)
        
        elapsed = max(1, int((now_time - start_time).total_seconds() // 60))
        new_used = data["quota_used"] + elapsed
        
        supabase.table("employee_data").update({"quota_used": new_used}).eq("emp_id", emp_id).eq("work_date", current_date).execute()
        supabase.table("break_history").insert({
            "emp_id": emp_id, 
            "work_date": current_date, 
            "break_type": info["break_type"], 
            "used_mins": elapsed, 
            "time_range": f"{start_time.strftime('%H:%M')} - {now_time.strftime('%H:%M')}"
        }).execute()
        supabase.table("active_breaks").delete().eq("emp_id", emp_id).execute()
        
        return_msg = (
            f"🏁 รหัส {emp_id} กลับเข้าทำงานแล้ว\n"
            f"• เวลาเข้า: {now_time.strftime('%H:%M:%S')} น.\n"
            f"• ใช้เวลาครั้งนี้: {elapsed} นาที (กำหนด {info['allowed_mins']} นาที)\n"
            f"📊 สรุปยอดวันนี้ (รอบวันที่ {current_date}):\n"
            f"- ใช้ไปรวม: {new_used} นาที\n"
            f"- โควตาคงเหลือ: {data['quota_total'] - new_used} นาที"
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
        if action in ["กินข้าว", "ซื้อของ"]:
            if meal_used >= 2:
                await update.message.reply_text(f"❌ ใช้สิทธิ์ข้าวครบ 2 ครั้งแล้ว")
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
            f"🔔 ควรกลับเข้าทำงานก่อนเวลา: {due_time.strftime('%H:%M:%S')} น.\n"
            f"📊 {data['shift']}\n"
            f"• โควตาเวลารวมคงเหลือ: {quota_left} นาที"
        )
        await update.message.reply_text(reply_msg)

if __name__ == '__main__':
    server_thread = threading.Thread(target=run_dummy_server, daemon=True)
    server_thread.start()

    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Night Shift Bot is running...")
    app.run_polling()
