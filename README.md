# Chat2Order AI

โปรเจกต์เว็บสำหรับแปลงข้อความแชตลูกค้าเป็นรายการสั่งซื้ออัตโนมัติ

## เริ่มใช้งาน
1. ติดตั้ง Python 3.10+
2. เปิดโฟลเดอร์ใน VS Code
3. เปิด Terminal แล้วรัน:
   pip install -r requirements.txt
4. รัน:
   uvicorn main:app --reload
5. เปิด:
   http://127.0.0.1:8000

## Deploy ขึ้น Server
1. คัดลอก `.env.example` เป็น `.env` และเปลี่ยนค่า secret/password ทั้งหมดก่อน production
2. ติดตั้ง dependencies:
   `pip install -r requirements.txt`
3. รัน production server:
   `uvicorn main:app --host 0.0.0.0 --port 8000`
4. ตั้ง reverse proxy หรือ platform ให้บริการ HTTPS แล้วตรวจสอบ:
   `https://โดเมนของคุณ/health`
5. ตั้ง LINE webhook เป็น:
   `https://โดเมนของคุณ/line/webhook`

ห้าม commit ไฟล์ `.env`, database จริง หรือค่า Channel secret/access token ลง source control

### Deploy บน Render
1. สร้าง repository จากโฟลเดอร์นี้ แล้ว push source code ขึ้น GitHub
2. ใน Render เลือก **New > Blueprint** และเลือก repository นี้
3. Render จะอ่านไฟล์ `render.yaml` และสร้าง Web Service พร้อม Persistent Disk
4. เปิด Environment ของ service แล้วกรอกค่า `CHAT2ORDER_USERNAME` และ `CHAT2ORDER_PASSWORD`
5. ถ้าใช้ LINE OA ให้กรอก `CHAT2ORDER_LINE_CHANNEL_SECRET`, `CHAT2ORDER_LINE_CHANNEL_ACCESS_TOKEN` และ `CHAT2ORDER_LINE_STORE_ID`
6. ถ้าใช้ AI จริง ให้กรอก `CHAT2ORDER_AI_API_KEY`
7. Deploy แล้วตรวจสอบ `https://ชื่อบริการ.onrender.com/health`
8. ตั้ง LINE webhook เป็น `https://ชื่อบริการ.onrender.com/line/webhook`

Persistent Disk จำเป็นเพราะระบบใช้ SQLite; ห้ามลบ disk หากต้องการเก็บออเดอร์เดิม

## เข้าสู่ระบบ
- ชื่อผู้ใช้เริ่มต้น: `admin`
- รหัสผ่านเริ่มต้น: `admin123`
- เปลี่ยนค่าได้ด้วยตัวแปรสภาพแวดล้อม `CHAT2ORDER_USERNAME`, `CHAT2ORDER_PASSWORD`
- ตั้งค่า `CHAT2ORDER_SESSION_SECRET` เป็นค่าลับที่ยาวและไม่ซ้ำกันก่อนใช้งานจริง
- ผู้ใช้ใหม่สมัครได้จากหน้า `สมัครสมาชิก` โดยรหัสผ่านจะถูกเก็บเป็น PBKDF2 hash
- ผู้ใช้แต่ละคนมีร้านค้าของตัวเอง สินค้าและออเดอร์จะแยกตามร้าน

## ฟีเจอร์
- วิเคราะห์ข้อความแชต
- แยกสินค้า สี ขนาด จำนวน
- บันทึกออเดอร์ SQLite
- เพิ่มสินค้า
- Dashboard
- ดูและลบออเดอร์

## เชื่อมต่อ AI จริง
- ตั้งค่า `CHAT2ORDER_AI_API_KEY` เป็น API key ของผู้ให้บริการที่รองรับ OpenAI Chat Completions
- ค่าเริ่มต้นใช้ `gpt-4o-mini`
- เปลี่ยนโมเดลด้วย `CHAT2ORDER_AI_MODEL`
- หากใช้ผู้ให้บริการ OpenAI-compatible อื่น ให้ตั้ง `CHAT2ORDER_AI_BASE_URL`
- ถ้า API key ไม่มีหรือ AI ตอบผิดรูปแบบ ระบบจะใช้ Rule-based fallback และแสดงแหล่งที่มาผลลัพธ์

## LINE OA webhook
- ตั้งค่า `CHAT2ORDER_LINE_CHANNEL_SECRET` เป็น Channel secret จาก LINE Developers
- ตั้งค่า `CHAT2ORDER_LINE_CHANNEL_ACCESS_TOKEN` เป็น Channel access token
- ตั้งค่า `CHAT2ORDER_LINE_STORE_ID` เป็นเลข `stores.id` ของร้านที่จะรับออเดอร์
- ตั้งค่า webhook URL ใน LINE Developers เป็น `https://โดเมนของคุณ/line/webhook`
- LINE จะส่งข้อความเข้าระบบหลังจากตรวจสอบ `X-Line-Signature` แล้ว
- ข้อความจะถูกบันทึกในตาราง `line_messages` และข้อความจะถูกวิเคราะห์เป็นออเดอร์ในร้านที่กำหนด
- ถ้าไม่ตั้งค่า access token ระบบยังบันทึกข้อมูลได้ แต่จะไม่สามารถตอบกลับ LINE ได้
- Webhook ต้องเป็น public HTTPS URL จึงจะใช้กับ LINE OA ได้
