import json
import base64
import hashlib
import hmac
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from io import BytesIO

import httpx
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from openpyxl import Workbook
from openpyxl.styles import Font
from starlette.middleware.sessions import SessionMiddleware

from ai_service import extract_items_with_ai
from database import get_conn, hash_password, init_db, verify_password


app = FastAPI(title="Chat2Order AI")
logger = logging.getLogger(__name__)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


@app.on_event("startup")
def startup():
    init_db()


PUBLIC_PATHS = {"/login", "/register", "/static", "/line/webhook", "/health"}


def is_authenticated(request: Request):
    return request.session.get("user_id") is not None


def current_user(request: Request):
    user_id = request.session.get("user_id")
    if user_id is None:
        return None
    conn = get_conn()
    user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    conn.close()
    return user


def current_store_id(request: Request) -> int:
    user = current_user(request)
    if user is None:
        raise ValueError("authenticated user has no store")
    return user["store_id"]


def _line_signature_is_valid(body: bytes, signature: str | None) -> bool:
    secret = os.getenv("CHAT2ORDER_LINE_CHANNEL_SECRET", "").strip()
    if not secret or not signature:
        return False
    expected = base64.b64encode(
        hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    ).decode("ascii")
    return hmac.compare_digest(expected, signature.strip())


def _line_store_for_user(conn, line_user_id: str):
    mapping = conn.execute(
        "SELECT store_id FROM line_user_stores WHERE line_user_id=?",
        (line_user_id,),
    ).fetchone()
    if mapping is not None:
        return mapping["store_id"]

    configured_store = os.getenv("CHAT2ORDER_LINE_STORE_ID", "").strip()
    if configured_store:
        try:
            store_id = int(configured_store)
        except ValueError:
            logger.warning("CHAT2ORDER_LINE_STORE_ID must be an integer")
        else:
            exists = conn.execute(
                "SELECT 1 FROM stores WHERE id=?", (store_id,)
            ).fetchone()
            if exists is not None:
                conn.execute(
                    """
                    INSERT INTO line_user_stores (line_user_id, store_id)
                    VALUES (?, ?)
                    ON CONFLICT(line_user_id) DO UPDATE SET
                        store_id=excluded.store_id,
                        updated_at=CURRENT_TIMESTAMP
                    """,
                    (line_user_id, store_id),
                )
                return store_id

    stores = conn.execute("SELECT id FROM stores ORDER BY id").fetchall()
    if len(stores) == 1:
        store_id = stores[0]["id"]
        conn.execute(
            "INSERT INTO line_user_stores (line_user_id, store_id) VALUES (?, ?)",
            (line_user_id, store_id),
        )
        return store_id
    return None


def _format_line_items(items: list) -> str:
    if not items:
        return "ยังแยกรายการสินค้าไม่ได้"
    parts = []
    for item in items:
        if not isinstance(item, dict):
            continue
        product = str(item.get("product") or "สินค้า")
        quantity = item.get("quantity", 1)
        options = " ".join(
            str(item.get(key))
            for key in ("color", "size")
            if item.get(key)
        )
        parts.append(f"{product}{f' ({options})' if options else ''} x{quantity}")
    return ", ".join(parts) or "ยังแยกรายการสินค้าไม่ได้"


async def _send_line_reply(reply_token: str, text: str) -> bool:
    channel_token = os.getenv("CHAT2ORDER_LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    if not channel_token:
        logger.warning("LINE channel access token is not configured")
        return False
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            "https://api.line.me/v2/bot/message/reply",
            headers={
                "Authorization": f"Bearer {channel_token}",
                "Content-Type": "application/json",
            },
            json={"replyToken": reply_token, "messages": [{"type": "text", "text": text}]},
        )
        response.raise_for_status()
    return True


def render(request: Request, template_name: str, context: dict, status_code: int = 200):
    template_context = {"request": request, **context}
    return templates.TemplateResponse(
        template_name, template_context, status_code=status_code
    )


@app.get("/health")
def health():
    conn = get_conn()
    conn.execute("SELECT 1").fetchone()
    conn.close()
    return {"status": "ok"}


@app.middleware("http")
async def require_login(request: Request, call_next):
    if (
        request.url.path not in PUBLIC_PATHS
        and not any(request.url.path.startswith(path + "/") for path in PUBLIC_PATHS)
        and not is_authenticated(request)
    ):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "กรุณาเข้าสู่ระบบ"}, status_code=401)
        return RedirectResponse("/login", status_code=303)
    return await call_next(request)


app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("CHAT2ORDER_SESSION_SECRET", "change-this-secret-key"),
    max_age=60 * 60 * 8,
)


@app.post("/line/webhook")
async def line_webhook(request: Request):
    body = await request.body()
    if not _line_signature_is_valid(
        body, request.headers.get("x-line-signature")
    ):
        raise HTTPException(status_code=401, detail="Invalid LINE signature")

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc

    events = payload.get("events", [])
    if not isinstance(events, list):
        raise HTTPException(status_code=400, detail="Invalid events payload")

    for event in events:
        if not isinstance(event, dict):
            continue
        message_event = event.get("message")
        source = event.get("source") or {}
        line_user_id = source.get("userId")
        message_text = (
            message_event.get("text")
            if isinstance(message_event, dict)
            and message_event.get("type") == "text"
            else None
        )
        reply_token = event.get("replyToken")
        if not line_user_id or not message_text:
            continue

        conn = get_conn()
        try:
            event_id = event.get("webhookEventId")
            if event_id and conn.execute(
                "SELECT 1 FROM line_messages WHERE event_id=?", (event_id,)
            ).fetchone():
                continue

            store_id = _line_store_for_user(conn, line_user_id)
            reply_text = (
                "ได้รับข้อความแล้ว แต่ยังไม่ได้เชื่อมผู้ใช้ LINE กับร้านค้า "
                "กรุณาตั้งค่า CHAT2ORDER_LINE_STORE_ID"
                if store_id is None
                else ""
            )
            order_id = None
            if store_id is not None:
                products = [
                    row["name"]
                    for row in conn.execute(
                        "SELECT name FROM products WHERE store_id=? ORDER BY id",
                        (store_id,),
                    ).fetchall()
                ]
                items, source_name = extract_items_with_ai(message_text, products)
                reply_text = (
                    "ได้รับออเดอร์แล้ว\n"
                    f"รายการ: {_format_line_items(items)}\n"
                    f"แหล่งวิเคราะห์: {source_name}\n"
                    "ทีมงานจะตรวจสอบและติดต่อกลับครับ"
                )
                order_cur = conn.execute(
                    """
                    INSERT INTO orders (customer_message, items_json, status, store_id)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        message_text,
                        json.dumps(items, ensure_ascii=False),
                        "รับจาก LINE - รอยืนยัน",
                        store_id,
                    ),
                )
                order_id = order_cur.lastrowid

            conn.execute(
                """
                INSERT INTO line_messages
                    (event_id, line_user_id, store_id, message_text, reply_text, order_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    line_user_id,
                    store_id,
                    message_text,
                    reply_text,
                    order_id,
                ),
            )
            conn.commit()
        finally:
            conn.close()

        if reply_token:
            await _send_line_reply(reply_token, reply_text)

    return {"success": True}


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if is_authenticated(request):
        return RedirectResponse("/", status_code=303)
    return render(request, "login.html", {"error": None})


@app.post("/login", response_class=HTMLResponse)
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    conn = get_conn()
    user = conn.execute(
        "SELECT * FROM users WHERE username=?", (username.strip(),)
    ).fetchone()
    conn.close()
    if user is not None and verify_password(password, user["password_hash"]):
        request.session.clear()
        request.session["user_id"] = user["id"]
        return RedirectResponse("/", status_code=303)
    return render(
        request,
        "login.html",
        {"error": "ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง"},
        status_code=401,
    )


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    if is_authenticated(request):
        return RedirectResponse("/", status_code=303)
    return render(request, "register.html", {"error": None})


@app.post("/register", response_class=HTMLResponse)
def register(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    store_name: str = Form("ร้านค้าของฉัน"),
):
    username = username.strip()
    store_name = store_name.strip() or "ร้านค้าของฉัน"
    if not username or not password:
        return render(
            request,
            "register.html",
            {"error": "กรุณากรอกชื่อผู้ใช้และรหัสผ่าน"},
            status_code=400,
        )
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("INSERT INTO stores (name) VALUES (?)", (store_name,))
        store_id = cur.lastrowid
        cur.execute(
            "INSERT INTO users (username, password_hash, store_id) VALUES (?, ?, ?)",
            (username, hash_password(password), store_id),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        conn.close()
        return render(
            request,
            "register.html",
            {"error": "ชื่อผู้ใช้นี้มีอยู่แล้ว"},
            status_code=400,
        )
    conn.close()
    return RedirectResponse("/login", status_code=303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


def fetch_products(store_id: int):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM products WHERE store_id=? ORDER BY id DESC", (store_id,)
    ).fetchall()
    conn.close()
    return rows


def _order_total(conn, order, store_id: int) -> float:
    try:
        items = json.loads(order["items_json"] or "[]")
    except (TypeError, json.JSONDecodeError):
        return 0.0
    total = 0.0
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            quantity = max(0, int(item.get("quantity", 1)))
        except (TypeError, ValueError):
            quantity = 0
        product = None
        product_id = item.get("product_id")
        if product_id is not None:
            product = conn.execute(
                "SELECT price FROM products WHERE id=? AND store_id=?",
                (product_id, store_id),
            ).fetchone()
        if product is None:
            product = conn.execute(
                "SELECT price FROM products WHERE store_id=? AND name=?",
                (store_id, item.get("product", "")),
            ).fetchone()
        if product is not None:
            total += float(product["price"] or 0) * quantity
    return total


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    store_id = current_store_id(request)
    conn = get_conn()
    total_orders = conn.execute(
        "SELECT COUNT(*) FROM orders WHERE store_id=?", (store_id,)
    ).fetchone()[0]
    total_products = conn.execute(
        "SELECT COUNT(*) FROM products WHERE store_id=?", (store_id,)
    ).fetchone()[0]
    store_orders = conn.execute(
        "SELECT * FROM orders WHERE store_id=? ORDER BY id DESC", (store_id,)
    ).fetchall()
    store = conn.execute("SELECT * FROM stores WHERE id=?", (store_id,)).fetchone()
    total_revenue = sum(_order_total(conn, order, store_id) for order in store_orders)
    conn.close()
    return render(
        request,
        "index.html",
        {
            "total_orders": total_orders,
            "total_products": total_products,
            "total_revenue": total_revenue,
            "recent": store_orders[:5],
            "store": store,
        },
    )


@app.get("/billing", response_class=HTMLResponse)
def billing_page(request: Request):
    store_id = current_store_id(request)
    conn = get_conn()
    store = conn.execute("SELECT * FROM stores WHERE id=?", (store_id,)).fetchone()
    conn.close()
    return render(request, "billing.html", {"store": store, "message": None})


@app.post("/billing/plan", response_class=HTMLResponse)
def change_plan(request: Request, plan: str = Form(...)):
    if plan not in {"free", "pro"}:
        raise HTTPException(status_code=400, detail="Invalid subscription plan")
    store_id = current_store_id(request)
    if plan == "free":
        status = "active"
        expires_at = None
    else:
        status = "pending_payment"
        expires_at = (
            datetime.now(timezone.utc) + timedelta(days=30)
        ).isoformat(timespec="seconds")
    conn = get_conn()
    conn.execute(
        """
        UPDATE stores
        SET subscription_plan=?, subscription_status=?, subscription_expires_at=?
        WHERE id=?
        """,
        (plan, status, expires_at, store_id),
    )
    conn.commit()
    store = conn.execute("SELECT * FROM stores WHERE id=?", (store_id,)).fetchone()
    conn.close()
    return render(
        request,
        "billing.html",
        {
            "store": store,
            "message": (
                "เลือกแพ็กเกจ Pro แล้ว แต่ยังไม่เปิดใช้งานจนกว่าจะเชื่อมระบบชำระเงินจริง"
                if plan == "pro"
                else "เปลี่ยนกลับเป็นแพ็กเกจ Free แล้ว"
            ),
        },
    )


@app.get("/line", response_class=HTMLResponse)
def line_page(request: Request):
    store_id = current_store_id(request)
    conn = get_conn()
    messages = conn.execute(
        """
        SELECT * FROM line_messages
        WHERE store_id=?
        ORDER BY id DESC
        LIMIT 30
        """,
        (store_id,),
    ).fetchall()
    connected_users = conn.execute(
        "SELECT COUNT(*) FROM line_user_stores WHERE store_id=?",
        (store_id,),
    ).fetchone()[0]
    conn.close()
    return render(
        request,
        "line.html",
        {
            "messages": messages,
            "connected_users": connected_users,
            "channel_configured": bool(
                os.getenv("CHAT2ORDER_LINE_CHANNEL_SECRET", "").strip()
                and os.getenv("CHAT2ORDER_LINE_CHANNEL_ACCESS_TOKEN", "").strip()
            ),
            "store_id": store_id,
        },
    )


@app.get("/analyze", response_class=HTMLResponse)
def analyze_page(request: Request):
    return render(request, "analyze.html", {})


@app.post("/api/analyze")
async def analyze(request: Request, data: dict):
    message = data.get("message", "")
    products = [row["name"] for row in fetch_products(current_store_id(request))]
    items, source = extract_items_with_ai(message, products)
    return JSONResponse({"items": items, "source": source})


@app.post("/api/order")
async def create_order(request: Request, data: dict):
    store_id = current_store_id(request)
    message = data.get("message", "")
    items = data.get("items", [])
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO orders (customer_message, items_json, status, store_id)
        VALUES (?, ?, ?, ?)
        """,
        (message, json.dumps(items, ensure_ascii=False), "Chờ xác nhận", store_id),
    )
    conn.commit()
    order_id = cur.lastrowid
    conn.close()
    return {"success": True, "order_id": order_id}


@app.get("/orders", response_class=HTMLResponse)
def orders_page(request: Request):
    store_id = current_store_id(request)
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM orders WHERE store_id=? ORDER BY id DESC", (store_id,)
    ).fetchall()
    orders = []
    for row in rows:
        order = dict(row)
        try:
            order["items"] = json.loads(order["items_json"] or "[]")
        except json.JSONDecodeError:
            order["items"] = []
        order["total_amount"] = _order_total(conn, row, store_id)
        orders.append(order)
    conn.close()
    return render(request, "orders.html", {"orders": orders})


@app.get("/orders/export")
def export_orders(request: Request):
    store_id = current_store_id(request)
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM orders WHERE store_id=? ORDER BY id DESC", (store_id,)
    ).fetchall()
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Orders"
    headers = [
        "Order ID",
        "Created At",
        "Status",
        "Customer Message",
        "Product",
        "Color",
        "Size",
        "Quantity",
        "Unit Price",
        "Total",
    ]
    sheet.append(headers)
    for cell in sheet[1]:
        cell.font = Font(bold=True)

    for row in rows:
        try:
            items = json.loads(row["items_json"] or "[]")
        except json.JSONDecodeError:
            items = []
        if not items:
            items = [{}]
        for item in items:
            product_name = str(item.get("product", ""))
            product = conn.execute(
                "SELECT price FROM products WHERE store_id=? AND name=?",
                (store_id, product_name),
            ).fetchone()
            unit_price = float(product["price"] or 0) if product else 0.0
            try:
                quantity = max(0, int(item.get("quantity", 1)))
            except (TypeError, ValueError):
                quantity = 0
            sheet.append(
                [
                    row["id"],
                    row["created_at"],
                    row["status"],
                    row["customer_message"],
                    product_name,
                    item.get("color", "-"),
                    item.get("size", "-"),
                    quantity,
                    unit_price,
                    unit_price * quantity,
                ]
            )
    conn.close()

    for column in sheet.columns:
        width = min(max(len(str(cell.value or "")) for cell in column) + 2, 50)
        sheet.column_dimensions[column[0].column_letter].width = width
    output = BytesIO()
    workbook.save(output)
    output.seek(0)
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=orders.xlsx"},
    )


@app.post("/orders/{order_id}/delete")
def delete_order(request: Request, order_id: int):
    conn = get_conn()
    conn.execute(
        "DELETE FROM orders WHERE id=? AND store_id=?",
        (order_id, current_store_id(request)),
    )
    conn.commit()
    conn.close()
    return RedirectResponse("/orders", status_code=303)


@app.get("/products", response_class=HTMLResponse)
def products_page(request: Request):
    return render(
        request,
        "products.html",
        {"products": fetch_products(current_store_id(request))},
    )


@app.post("/products/add")
def add_product(request: Request, name: str = Form(...), price: float = Form(0)):
    conn = get_conn()
    conn.execute(
        "INSERT INTO products (name, price, store_id) VALUES (?, ?, ?)",
        (name.strip(), price, current_store_id(request)),
    )
    conn.commit()
    conn.close()
    return RedirectResponse("/products", status_code=303)


@app.post("/products/{product_id}/delete")
def delete_product(request: Request, product_id: int):
    conn = get_conn()
    conn.execute(
        "DELETE FROM products WHERE id=? AND store_id=?",
        (product_id, current_store_id(request)),
    )
    conn.commit()
    conn.close()
    return RedirectResponse("/products", status_code=303)
