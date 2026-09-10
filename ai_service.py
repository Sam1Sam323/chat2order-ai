import json
import logging
import os
import re
from urllib import error, request

logger = logging.getLogger(__name__)

COLORS = [
    "ดำ", "ขาว", "แดง", "น้ำเงิน", "ฟ้า", "เขียว", "เหลือง", "ชมพู", "เทา", "ม่วง", "ส้ม",
    "đen", "trắng", "đỏ", "xanh dương", "xanh", "xanh lá", "vàng", "hồng", "xám", "tím", "cam",
]
SIZES = ["XS", "S", "M", "L", "XL", "XXL", "Free Size"]


def _find_color(line: str):
    for color in sorted(COLORS, key=len, reverse=True):
        if re.search(rf"(?:สี\s*)?{re.escape(color)}", line, re.I):
            return color
    return None


def _find_size(line: str):
    for size in sorted(SIZES, key=len, reverse=True):
        if len(size) > 2:
            if re.search(rf"(?:ไซส์|ไซซ์|size|cỡ|size\s*)?{re.escape(size)}", line, re.I):
                return size
        else:
            if re.search(rf"(?<!\w){re.escape(size)}(?!\w)", line, re.I):
                return size
    return None


def _find_quantity(line: str):
    match = re.search(r"(?:จำนวน\s*|qty\s*|số lượng\s*)?(\d+)\s*(?:ตัว|ชิ้น|อัน|แก้ว|ชุด|กล่อง|แพ็ก|cái|chiếc|ly|bộ|hộp|pcs?|piece|pieces|set)?", line, re.I)
    if match:
        return int(match.group(1))
    return 1


def _infer_fallback_product(line: str):
    cleaned = line
    cleaned = re.sub(r"^(เอา|ซื้อ|สั่ง|ต้องการ|ขอ|ครับ|ค่ะ|หน่อย|mua|đặt|cho tôi|tôi cần)\s*", "", cleaned, flags=re.I)
    cleaned = re.sub(r"(?:จำนวน|qty|số lượng)\s*", " ", cleaned, flags=re.I)
    for color_name in sorted(COLORS, key=len, reverse=True):
        cleaned = re.sub(rf"(?:สี\s*)?{re.escape(color_name)}", " ", cleaned, flags=re.I)
    for size_name in sorted(SIZES, key=len, reverse=True):
        cleaned = re.sub(rf"(?<!\w){re.escape(size_name)}(?!\w)", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"(?:จำนวน|số lượng\s*)?\d+\s*(?:ตัว|ชิ้น|อัน|แก้ว|ชุด|กล่อง|แพ็ก|cái|chiếc|ly|bộ|hộp|pcs?|piece|pieces|set)\b", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"[^\w\sก-๙]", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or None


def extract_items(message: str, product_names=None):
    product_names = product_names or []
    product_names = sorted({name.strip() for name in product_names if name and name.strip()}, key=lambda p: (-len(p), p.lower()))
    lines = re.split(r"(?:\n|\s+และ\s+|\s+แล้ว\s+|\s+กับ\s+|\s+và\s+|\s+với\s+|,)", message, flags=re.I)
    items = []

    for line in lines:
        line = line.strip()
        if not line:
            continue

        qty = _find_quantity(line)
        color = _find_color(line)
        size = _find_size(line)

        product = None
        normalized_line = line.lower()
        for candidate in product_names:
            candidate_lower = candidate.lower()
            if candidate_lower in normalized_line:
                product = candidate
                break

        if not product:
            cleaned = line
            cleaned = re.sub(r"^(เอา|ซื้อ|สั่ง|ต้องการ|ขอ|ครับ|ค่ะ|หน่อย|mua|đặt|cho tôi|tôi cần)\s*", "", cleaned, flags=re.I)
            cleaned = re.sub(r"(?:จำนวน|qty|số lượng)\s*", " ", cleaned, flags=re.I)
            cleaned = re.sub(r"(?:สี|ไซส์|ไซซ์|size|màu|cỡ)\s*", " ", cleaned, flags=re.I)
            for color_name in sorted(COLORS, key=len, reverse=True):
                cleaned = re.sub(rf"(?:สี\s*)?{re.escape(color_name)}", " ", cleaned, flags=re.I)
            for size_name in sorted(SIZES, key=len, reverse=True):
                cleaned = re.sub(rf"(?<!\w){re.escape(size_name)}(?!\w)", " ", cleaned, flags=re.I)
            cleaned = re.sub(r"(?:จำนวน|số lượng\s*)?\d+\s*(?:ตัว|ชิ้น|อัน|แก้ว|ชุด|กล่อง|แพ็ก|cái|chiếc|ly|bộ|hộp|pcs?|piece|pieces|set)\b", " ", cleaned, flags=re.I)
            cleaned = re.sub(r"[^\w\sก-๙]", " ", cleaned)
            cleaned = re.sub(r"\s+", " ", cleaned).strip()
            fallback_product = _infer_fallback_product(line)
            if fallback_product:
                if not cleaned or cleaned == fallback_product:
                    product = cleaned or fallback_product
                elif fallback_product in cleaned:
                    product = fallback_product
                else:
                    product = cleaned or fallback_product
            else:
                product = cleaned or "ไม่ระบุสินค้า"

        items.append({
            "product": product,
            "color": color or "-",
            "size": size or "-",
            "quantity": qty
        })

    return items


def _valid_items(value):
    if not isinstance(value, list):
        return None
    items = []
    for item in value:
        if not isinstance(item, dict) or not item.get("product"):
            return None
        try:
            quantity = int(item.get("quantity", 1))
        except (TypeError, ValueError):
            return None
        if quantity < 1:
            return None
        items.append(
            {
                "product": str(item["product"]).strip(),
                "color": str(item.get("color") or "-").strip(),
                "size": str(item.get("size") or "-").strip(),
                "quantity": quantity,
            }
        )
    return items


def extract_items_with_ai(message: str, product_names=None):
    """Use an OpenAI-compatible chat API when configured, otherwise use rules."""
    fallback = extract_items(message, product_names)
    api_key = os.getenv("CHAT2ORDER_AI_API_KEY", "").strip()
    if not api_key:
        return fallback, "rule-based"

    base_url = os.getenv(
        "CHAT2ORDER_AI_BASE_URL", "https://api.openai.com/v1"
    ).rstrip("/")
    model = os.getenv("CHAT2ORDER_AI_MODEL", "gpt-4o-mini")
    catalog = ", ".join(product_names or []) or "(ไม่มีรายการสินค้าในแคตตาล็อก)"
    system_prompt = (
        "คุณเป็นระบบแยกรายการสั่งซื้อจากข้อความลูกค้า "
        "ตอบเป็น JSON array เท่านั้น โดยแต่ละรายการต้องมี product, color, size, quantity "
        "ใช้ '-' เมื่อไม่มีข้อมูล และ quantity ต้องเป็นจำนวนเต็มบวก "
        f"สินค้าที่ร้านมี: {catalog}"
    )
    payload = json.dumps(
        {
            "model": model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": message},
            ],
            "response_format": {"type": "json_object"},
        },
        ensure_ascii=False,
    ).encode("utf-8")
    req = request.Request(
        f"{base_url}/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=30) as response:
            body = json.loads(response.read().decode("utf-8"))
        content = body["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        if isinstance(parsed, dict):
            parsed = parsed.get("items")
        validated = _valid_items(parsed)
        if validated is None:
            raise ValueError("AI returned an invalid item schema")
        return validated, "ai"
    except (error.URLError, TimeoutError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        logger.exception("AI extraction failed; using rule-based extraction")
        return fallback, "rule-based-fallback"
