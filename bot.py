from telethon import TelegramClient, events
import requests

API_ID = 38386998
API_HASH = '37190623410210007351c1f9586ce19a'
BOT_TOKEN = '8931960128:AAEWe5Lk_oqnRWqVfdDgqJK02jlBKEb8XsQ'

API_URL = "https://shopifyapibyme.onrender.com"
ADMIN_IDS = [8705531041]

bot = TelegramClient('shopifyx', API_ID, API_HASH).start(bot_token=BOT_TOKEN)

@bot.on(events.NewMessage(pattern='/start'))
async def start(event):
    text = """🛒 SHOPIFY X

/site <url> — সাইট টেস্ট
/check <site> <cc|mm|yy|cvv> — কার্ড চেক
/help — কমান্ড"""
    await event.reply(text)

@bot.on(events.NewMessage(pattern='/site'))
async def site_check(event):
    parts = event.raw_text.split()
    if len(parts) != 2:
        return await event.reply("Usage: /site <url>")

    site = parts[1]
    msg = await event.reply("🔍 Testing...")

    try:
        r = requests.post(f"{API_URL}/site", json={"site": site})
        data = r.json()
        await msg.edit(f"🌐 {data.get('site')}\n📊 Status: {data.get('status')}\n💰 Price: {data.get('price', '-')}")
    except Exception as e:
        await msg.edit(f"❌ {str(e)[:50]}")

@bot.on(events.NewMessage(pattern='/check'))
async def check(event):
    parts = event.raw_text.split()
    if len(parts) != 3:
        return await event.reply("Usage: /check <site> <cc|mm|yy|cvv>")

    site = parts[1]
    card_parts = parts[2].split("|")
    if len(card_parts) != 4:
        return await event.reply("❌ Card format: cc|mm|yy|cvv")

    cc, mm, yy, cvv = card_parts
    msg = await event.reply("🛒 Checking...")

    try:
        r = requests.post(f"{API_URL}/check", json={
            "site": site,
            "cc": cc,
            "mm": mm,
            "yy": yy,
            "cvv": cvv
        })
        data = r.json()
        text = f"""💳 {cc[:6]}xxxx{cc[-4:]}
🌐 {data.get('site')}
📊 Status: {data.get('status')}
💰 Price: {data.get('price', '-')}
📝 {data.get('message', '-')}"""
        await msg.edit(text)
    except Exception as e:
        await msg.edit(f"❌ {str(e)[:50]}")

@bot.on(events.NewMessage(pattern='/help'))
async def help_cmd(event):
    await event.reply("/site <url>\n/check <site> <cc|mm|yy|cvv>")

print("✅ SHOPIFY X BOT STARTED")
bot.run_until_disconnected()