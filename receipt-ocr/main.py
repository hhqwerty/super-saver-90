import io
import json
import logging
import re
from contextlib import asynccontextmanager

import httpx
import pytesseract
import uvicorn
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from PIL import Image, ImageEnhance, ImageFilter

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("receipt-ocr")

RKLLAMA_URL = "http://rkllama:8080/api/chat"
MODEL = "Qwen2.5-3B"

SYSTEM_PROMPT = """\
You are a financial receipt and bank transfer data extraction engine.
You will receive raw OCR text which may be a shop receipt OR a bank transfer screenshot.
Your task: return ONLY a valid JSON object — no markdown, no explanation, no extra text.

Required JSON format:
{
  "merchant": "<string or null>",
  "total_amount": <number, no commas or symbols, or null>,
  "currency": "<ISO 4217 code e.g. VND, or null>",
  "date": "<YYYY-MM-DD or null>",
  "time": "<HH:mm or null>",
  "description": "<transaction memo or brief item list or null>",
  "category": "<see rules below>",
  "transaction_type": "<expense or income>"
}

Account owners: HOANG NHAT HOANG, LE THI THANH DUNG

transaction_type rules (apply in this exact order, stop at first match):
1. "Tới tài khoản" contains "HOANG NHAT HOANG" or "LE THI THANH DUNG" → income
2. "Tới tài khoản" exists and is NOT an account owner → expense
3. "Tới tài khoản" absent (only sender shown) → income
4. No bank transfer fields present (shop/store receipt) → expense

merchant rules:
- income: merchant = name under "Từ tài khoản" (sender), or null if not shown
- expense (bank transfer): merchant = name under "Tới tài khoản" (receiver)
- expense (shop receipt): merchant = store/vendor name

total_amount rules:
- Always a positive number (ignore leading minus/plus sign)
- Strip commas and currency symbols (e.g. "-VND 118,000" → 118000)

category rules:
- expense: one of: Groceries Dining Transport Utilities Health Entertainment Shopping Transfer Other
- income: one of: Salary Investment Debt_Repaid Bonus Other_Income

Other rules:
- Date: DD/MM/YYYY → YYYY-MM-DD (e.g. "09:27 21/04/2026" → date="2026-04-21" time="09:27")
- time: HH:mm only (e.g. "09:27")
- currency: VND for Vietnamese Dong
- If a field cannot be determined, use null
- Output ONLY the JSON object, nothing else"""


def preprocess_image(raw: bytes) -> Image.Image:
    """Enhance receipt image for better OCR accuracy."""
    img = Image.open(io.BytesIO(raw)).convert("L")  # grayscale

    # Upscale small images — tesseract works best at ~300 DPI
    w, h = img.size
    if w < 1000:
        scale = 1000 / w
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

    # Sharpen and boost contrast
    img = img.filter(ImageFilter.SHARPEN)
    img = ImageEnhance.Contrast(img).enhance(2.0)

    return img


def run_ocr(img: Image.Image) -> str:
    """Run tesseract on preprocessed image."""
    config = "--psm 6 -l eng+vie"  # block of text, English + Vietnamese
    text = pytesseract.image_to_string(img, config=config)
    return text.strip()


def extract_date_time_from_ocr(ocr_text: str) -> tuple[str | None, str | None]:
    """Fallback: extract date and time from raw OCR text when LLM returns null."""
    # Pattern: HH:MM DD/MM/YYYY  e.g. "18:59 20/04/2026"
    m = re.search(r"(\d{2}:\d{2})\s+(\d{2})\/(\d{2})\/(\d{4})", ocr_text)
    if m:
        time_val = m.group(1)
        date_val = f"{m.group(4)}-{m.group(3)}-{m.group(2)}"
        return date_val, time_val
    # Pattern: DD/MM/YYYY only
    m = re.search(r"(\d{2})\/(\d{2})\/(\d{4})", ocr_text)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}", None
    # Pattern: YYYY-MM-DD
    m = re.search(r"(\d{4}-\d{2}-\d{2})", ocr_text)
    if m:
        return m.group(1), None
    return None, None


def extract_json(text: str) -> dict:
    """Extract JSON from LLM output, handling markdown fences and comment chars."""
    text = text.strip()
    # Remove ```json ... ``` or ``` ... ```
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    # Strip leading # from each line (Qwen sometimes outputs "# key": value)
    lines = [re.sub(r"^\s*#\s?", "", l) for l in text.splitlines()]
    text = "\n".join(lines)
    # Find the first {...} block
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        raise ValueError(f"No JSON object found in: {text!r}")
    raw = match.group()
    # Fix invalid JSON escape sequences (e.g. \B, \M from OCR text embedded in strings)
    # Replace any backslash not followed by a valid JSON escape char with \\
    raw = re.sub(r'\\(?!["\\/bfnrtu0-9])', r'\\\\', raw)
    return json.loads(raw)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("receipt-ocr service starting")
    yield
    log.info("receipt-ocr service stopping")


app = FastAPI(title="Receipt OCR Service", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/process-receipt")
async def process_receipt(file: UploadFile = File(...)):
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty file")

    # Step 1: OCR
    try:
        img = preprocess_image(raw)
        ocr_text = run_ocr(img)
    except Exception as exc:
        log.error("OCR failed: %s", exc)
        raise HTTPException(status_code=422, detail=f"OCR failed: {exc}")

    log.info("OCR extracted %d chars", len(ocr_text))
    if not ocr_text:
        raise HTTPException(status_code=422, detail="OCR extracted no text from image")

    # Step 2: Send to rkllama (NPU)
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Receipt text:\n\n{ocr_text}"},
        ],
        "stream": False,
        "options": {"temperature": 0, "num_predict": 512},
    }

    try:
        async with httpx.AsyncClient(timeout=90) as client:
            resp = await client.post(RKLLAMA_URL, json=payload)
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        log.error("rkllama request failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"rkllama unreachable: {exc}")

    raw_content = resp.json().get("message", {}).get("content", "")
    log.info("LLM raw output: %s", raw_content[:200])

    # Step 3: Parse JSON
    try:
        data = extract_json(raw_content)
    except (ValueError, json.JSONDecodeError) as exc:
        log.error("JSON parse failed: %s | raw: %s", exc, raw_content)
        return JSONResponse(
            status_code=422,
            content={"error": "LLM did not return valid JSON", "raw": raw_content, "ocr_text": ocr_text},
        )

    # Coerce types
    if data.get("total_amount") is not None:
        try:
            data["total_amount"] = float(str(data["total_amount"]).replace(",", ""))
        except (ValueError, TypeError):
            data["total_amount"] = None

    # Fallback: extract date/time from OCR if LLM returned null
    if not data.get("date") or not data.get("time"):
        fallback_date, fallback_time = extract_date_time_from_ocr(ocr_text)
        if not data.get("date") and fallback_date:
            data["date"] = fallback_date
            log.info("Date extracted from OCR fallback: %s", fallback_date)
        if not data.get("time") and fallback_time:
            data["time"] = fallback_time
            log.info("Time extracted from OCR fallback: %s", fallback_time)

    return {"data": data, "ocr_text": ocr_text}


@app.get("/edit", response_class=HTMLResponse)
async def edit_form(request: Request):
    p = request.query_params
    receipt_id = p.get("id", "")
    merchant   = p.get("merchant", "")
    amount     = p.get("amount", "")
    currency   = p.get("currency", "VND")
    date       = p.get("date", "")
    time_val   = p.get("time", "")
    html = f"""<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Chỉnh sửa giao dịch</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, sans-serif; background: var(--tg-theme-bg-color, #fff);
         color: var(--tg-theme-text-color, #000); padding: 16px; }}
  h2 {{ font-size: 18px; margin-bottom: 16px; }}
  label {{ display: block; font-size: 13px; color: var(--tg-theme-hint-color, #888); margin-bottom: 4px; }}
  input {{ width: 100%; padding: 10px 12px; font-size: 15px; border: 1px solid var(--tg-theme-hint-color, #ccc);
           border-radius: 8px; background: var(--tg-theme-secondary-bg-color, #f5f5f5);
           color: var(--tg-theme-text-color, #000); margin-bottom: 14px; }}
  input:focus {{ outline: none; border-color: var(--tg-theme-button-color, #2196F3); }}
  button {{ width: 100%; padding: 13px; font-size: 16px; font-weight: 600;
            background: var(--tg-theme-button-color, #2196F3);
            color: var(--tg-theme-button-text-color, #fff);
            border: none; border-radius: 10px; cursor: pointer; }}
</style>
</head>
<body>
<h2>✏️ Chỉnh sửa giao dịch</h2>
<form id="f">
  <input type="hidden" id="receipt_id" value="{receipt_id}">
  <label>Merchant / Tên cửa hàng</label>
  <input type="text" id="merchant" value="{merchant}" placeholder="VD: HOANG NHAT HOANG">
  <label>Số tiền</label>
  <input type="number" id="amount" value="{amount}" placeholder="VD: 22000000">
  <label>Tiền tệ</label>
  <input type="text" id="currency" value="{currency}" maxlength="3" placeholder="VND">
  <label>Ngày (DD/MM/YYYY)</label>
  <input type="text" id="date" value="{date}" placeholder="VD: 20/04/2026">
  <label>Giờ (HH:mm)</label>
  <input type="text" id="time" value="{time_val}" placeholder="VD: 19:09">
  <button type="submit">💾 Lưu</button>
</form>
<script>
  const tg = window.Telegram.WebApp;
  tg.ready();
  tg.expand();
  document.getElementById('f').addEventListener('submit', function(e) {{
    e.preventDefault();
    const data = {{
      receipt_id: document.getElementById('receipt_id').value,
      merchant:   document.getElementById('merchant').value.trim(),
      amount:     document.getElementById('amount').value,
      currency:   document.getElementById('currency').value.trim().toUpperCase(),
      date:       document.getElementById('date').value.trim(),
      time:       document.getElementById('time').value.trim(),
    }};
    tg.sendData(JSON.stringify(data));
  }});
</script>
</body>
</html>"""
    return HTMLResponse(content=html)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, workers=1)
