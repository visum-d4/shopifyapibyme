from fastapi import FastAPI
from pydantic import BaseModel
from typing import Optional
import re
import aiohttp
from urllib.parse import urlparse

app = FastAPI(title="Shopify Checker API", version="1.0.0")

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

@app.get("/")
async def root():
    return {"status": "ok", "service": "Shopify Checker API"}

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
        return {"site": site, "card": card, "status": "ERROR", "message": price}

    return {
        "site": site,
        "card": card,
        "status": "PENDING",
        "price": price,
        "variant_id": info["variant_id"],
        "link": info["link"]
    }