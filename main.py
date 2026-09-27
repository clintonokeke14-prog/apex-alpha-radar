import asyncio
import base64
import json
import os
import aiohttp
import base58
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from dotenv import load_dotenv

# Solana cryptographic primitives
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TARGET_CHAT_ID = os.getenv("YOUR_TELEGRAM_CHAT_ID")
ADMIN_USER_ID = os.getenv("ADMIN_USER_ID", "5771609481")
RPC_URL = os.getenv("SOLANA_RPC_URL")

TREASURY_WALLET = os.getenv("TREASURY_WALLET_ADDRESS", "")
BURNER_PUBKEY = os.getenv("BURNER_PUBLIC_KEY", "")
BURNER_PRIVKEY_B58 = os.getenv("BURNER_PRIVATE_KEY", "")

# Keyless Lite API (or standard API if key provided)
JUP_API_KEY = os.getenv("JUPITER_API_KEY")
JUPITER_BASE_URL = "https://api.jup.ag/swap/v1" if JUP_API_KEY else "https://lite-api.jup.ag/swap/v1"

# Load local signing keypair
burner_keypair = None
if BURNER_PRIVKEY_B58:
    try:
        clean_key = BURNER_PRIVKEY_B58.strip().strip('"').strip("'").replace(" ", "").replace("\n", "").replace("\r", "")
        raw_bytes = base58.b58decode(clean_key)
        if len(raw_bytes) == 64:
            burner_keypair = Keypair.from_bytes(raw_bytes)
        elif len(raw_bytes) == 65:
            burner_keypair = Keypair.from_bytes(raw_bytes[:64])
        elif len(raw_bytes) == 32:
            burner_keypair = Keypair.from_seed(raw_bytes)
        else:
            burner_keypair = Keypair.from_base58_string(clean_key)
        print("Burner keypair verified in memory.")
    except Exception as e:
        print(f"[Keypair Warning]: {e}")

DEVELOPER_FEE_BPS = 100  # 1.0% Platform Fee
SOL_MINT = "So11111111111111111111111111111111111111112"
SUBSCRIBERS_FILE = "subscribers.json"
MONTHLY_SUB_PRICE_SOL = 0.5

# Curated Alpha Syndicate
WATCHED_WALLETS = {
    # --- Original Core Wallets ---
    "CNQHDwLNZ7JeM8ISKKHFFZJ9xNt4cwEVhEsgQS1KdGn8": "Macro Whale (71% WR)",
    "6qudAN2kV8mtCcYJxb5QQ6Vr15itdHHdeVbYm99NKMhy": "Mega Swing (64% WR)",
    "gy1mJNas5fLZyNPk3RP6dPpYFE3Z6kE9oxSX7ZVLVR1": "Intraday Alpha (64% WR)",

    # --- New GMGN Intraday Momentum Wallets ---
    "2X9M7fJ8nX7ogUw4R2mXndWMMqENe456fKgg25CeKkhJ": "ClauseLab (67% WR)",
    "D3zAG2Kb6dAWvfuJbxhMun9dT3VurJcFJRDJvVKapZ3d": "Momentum Alpha (64% WR)",
    "D9UiteKBjbVp8UYGRSK14fJRsXYp8Csk7zcmaCvq6LS": "High-Freq Scalper (50% WR)",
    
}

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
seen_signatures = set()

# --- SUBSCRIBER DATABASE ENGINE ---
def load_subscribers() -> dict:
    if not os.path.exists(SUBSCRIBERS_FILE):
        with open(SUBSCRIBERS_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f)
        return {}
    try:
        with open(SUBSCRIBERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_subscribers(data: dict):
    with open(SUBSCRIBERS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)

def is_user_subscribed(user_id: int) -> bool:
    user_str = str(user_id)
    # Permanent clearance for the desk admin
    if user_str == str(ADMIN_USER_ID):
        return True
    
    subs = load_subscribers()
    if user_str not in subs:
        return False
    
    expiry_str = subs[user_str].get("expires_at", "")
    try:
        expiry_dt = datetime.strptime(expiry_str, "%Y-%m-%d %H:%M:%S")
        return datetime.now() < expiry_dt
    except Exception:
        return False

def add_subscription(user_id: int, days: int) -> str:
    subs = load_subscribers()
    user_str = str(user_id)
    now = datetime.now()

    if user_str in subs and is_user_subscribed(user_id):
        current_expiry = datetime.strptime(subs[user_str]["expires_at"], "%Y-%m-%d %H:%M:%S")
        new_expiry = current_expiry + timedelta(days=days)
    else:
        new_expiry = now + timedelta(days=days)

    expiry_str = new_expiry.strftime("%Y-%m-%d %H:%M:%S")
    subs[user_str] = {
        "granted_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "expires_at": expiry_str
    }
    save_subscribers(subs)
    return expiry_str

def remove_subscription(user_id: int) -> bool:
    subs = load_subscribers()
    user_str = str(user_id)
    if user_str in subs:
        del subs[user_str]
        save_subscribers(subs)
        return True
    return False

# --- KEYBOARDS ---
def build_swap_keyboard(token_mint: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⚡ Buy 0.1 SOL", callback_data=f"buy:0.1:{token_mint}"),
                InlineKeyboardButton(text="⚡ Buy 0.5 SOL", callback_data=f"buy:0.5:{token_mint}"),
                InlineKeyboardButton(text="⚡ Buy 1.0 SOL", callback_data=f"buy:1.0:{token_mint}")
            ],
            [
                InlineKeyboardButton(text="💼 Check Vault", callback_data="wallet:check"),
                InlineKeyboardButton(text="📊 DEXScreener", url=f"https://dexscreener.com/solana/{token_mint}")
            ]
        ]
    )

def build_paywall_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 Activate Access / Subscribe", callback_data="sub:paywall")],
            [InlineKeyboardButton(text="🛡️ Contact Desk Admin", url="https://t.me/")]
        ]
    )

# --- RPC & JUPITER UTILITIES ---
async def rpc_call(session: aiohttp.ClientSession, method: str, params: list):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    try:
        async with session.post(RPC_URL, json=payload, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get("result")
    except Exception as e:
        print(f"[RPC Error - {method}]: {e}")
    return None

async def get_wallet_balance(session: aiohttp.ClientSession, pubkey: str) -> float:
    result = await rpc_call(session, "getBalance", [pubkey])
    if result and "value" in result:
        return result["value"] / 1_000_000_000
    return 0.0

async def fetch_dexscreener_info(session: aiohttp.ClientSession, token_mint: str):
    url = f"https://api.dexscreener.com/latest/dex/tokens/{token_mint}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status == 200:
                data = await resp.json()
                pairs = data.get("pairs")
                if pairs and len(pairs) > 0:
                    pair = pairs[0]
                    return {
                        "name": pair.get("baseToken", {}).get("name", "Unknown"),
                        "symbol": pair.get("baseToken", {}).get("symbol", "???"),
                        "mcap": pair.get("fdv", 0),
                        "liquidity": pair.get("liquidity", {}).get("usd", 0),
                        "price": pair.get("priceUsd", "0.00")
                    }
    except Exception as e:
        print(f"[DEX Error]: {e}")
    return None

async def get_jupiter_quote(session: aiohttp.ClientSession, input_mint: str, output_mint: str, amount_sol: float):
    lamports = int(amount_sol * 1_000_000_000)
    url = f"{JUPITER_BASE_URL}/quote?inputMint={input_mint}&outputMint={output_mint}&amount={lamports}&slippageBps=100"
    headers = {"User-Agent": "Mozilla/5.0"}
    if JUP_API_KEY:
        headers["x-api-key"] = JUP_API_KEY

    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status == 200:
                return await resp.json(), None
            else:
                raw_err = await resp.text()
                return None, f"Quote HTTP {resp.status}: {raw_err[:100]}"
    except Exception as e:
        return None, str(e)

async def build_jupiter_swap_transaction(session: aiohttp.ClientSession, quote_response: dict, user_pubkey: str):
    url = f"{JUPITER_BASE_URL}/swap"
    payload = {
        "quoteResponse": quote_response,
        "userPublicKey": user_pubkey,
        "wrapAndUnwrapSol": True,
        "dynamicComputeUnitLimit": True,
        "prioritizationFeeLamports": "auto"
    }
    headers = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
    if JUP_API_KEY:
        headers["x-api-key"] = JUP_API_KEY

    try:
        async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get("swapTransaction"), None
            else:
                err_text = await resp.text()
                return None, f"Swap HTTP {resp.status}: {err_text[:120]}"
    except Exception as e:
        return None, str(e)

async def simulate_solana_tx(session: aiohttp.ClientSession, signed_tx_b64: str):
    params = [
        signed_tx_b64,
        {"sigVerify": False, "encoding": "base64", "commitment": "processed"}
    ]
    return await rpc_call(session, "simulateTransaction", params)

def extract_token_mint(tx_data: dict, tracked_wallet: str):
    try:
        meta = tx_data.get("meta", {})
        pre_tokens = meta.get("preTokenBalances", [])
        post_tokens = meta.get("postTokenBalances", [])

        for post in post_tokens:
            if post.get("owner") == tracked_wallet:
                mint = post.get("mint")
                if mint == SOL_MINT:
                    continue
                pre_amount = 0
                for pre in pre_tokens:
                    if pre.get("owner") == tracked_wallet and pre.get("mint") == mint:
                        pre_amount = float(pre.get("uiTokenAmount", {}).get("uiAmount") or 0)
                        break
                post_amount = float(post.get("uiTokenAmount", {}).get("uiAmount") or 0)
                if post_amount > pre_amount:
                    return mint
    except Exception as e:
        print(f"[Parse Error]: {e}")
    return None

# --- TELEGRAM ACTIONS & GATEKEEPER ---
@dp.callback_query(F.data.startswith("buy:"))
async def handle_buy_callback(callback: types.CallbackQuery):
    user_id = callback.from_user.id

    # Gatekeeper Authorization Check
    if not is_user_subscribed(user_id):
        paywall_msg = (
            "🔒 **VIP DESK MEMBERSHIP REQUIRED**\n\n"
            "This 1-click execution desk is exclusive to active subscribers.\n\n"
            "✨ **Subscriber Benefits:**\n"
            "• Direct 1-Click execution on vetted smart money entries\n"
            "• Low-latency Jupiter routing with built-in slippage protection\n"
            "• Filtered signals (> $15k Liq, > $30k MC, 60%+ Win Rate)\n\n"
            f"💰 **Membership:** `{MONTHLY_SUB_PRICE_SOL} SOL / Month`\n"
            f"🏦 **Treasury Deposit:**\n`{TREASURY_WALLET}`\n\n"
            f"Send `{MONTHLY_SUB_PRICE_SOL} SOL` to the address above, then send your Telegram ID (`{user_id}`) to the desk admin to activate."
        )
        await callback.answer("VIP Access Required", show_alert=True)
        try:
            await bot.send_message(user_id, paywall_msg, reply_markup=build_paywall_keyboard(), parse_mode="Markdown")
        except Exception:
            await callback.message.reply(paywall_msg, reply_markup=build_paywall_keyboard(), parse_mode="Markdown")
        return

    _, amount_str, token_mint = callback.data.split(":")
    amount_sol = float(amount_str)
    
    await callback.answer(f"Routing swap for {amount_sol} SOL...", show_alert=False)
    
    async with aiohttp.ClientSession() as session:
        quote, err = await get_jupiter_quote(session, SOL_MINT, token_mint, amount_sol)
        if not quote:
            msg = f"❌ Quote Failed: {err}"
            try:
                await bot.send_message(user_id, msg, parse_mode="Markdown")
            except Exception:
                await callback.message.reply(msg, parse_mode="Markdown")
            return

        out_tokens = int(quote.get("outAmount", 0))
        dev_fee_sol = amount_sol * (DEVELOPER_FEE_BPS / 10_000)

        raw_tx_b64, swap_err = await build_jupiter_swap_transaction(session, quote, BURNER_PUBKEY)
        if not raw_tx_b64:
            msg = f"❌ Transaction Assembly Failed: {swap_err}"
            try:
                await bot.send_message(user_id, msg, parse_mode="Markdown")
            except Exception:
                await callback.message.reply(msg, parse_mode="Markdown")
            return

        if not burner_keypair:
            msg = "❌ Signing Error: Keypair not loaded"
            try:
                await bot.send_message(user_id, msg, parse_mode="Markdown")
            except Exception:
                await callback.message.reply(msg, parse_mode="Markdown")
            return

        try:
            raw_tx_bytes = base64.b64decode(raw_tx_b64)
            tx = VersionedTransaction.from_bytes(raw_tx_bytes)
            signed_tx = VersionedTransaction(tx.message, [burner_keypair])
            signed_tx_b64 = base64.b64encode(bytes(signed_tx)).decode("utf-8")
        except Exception as sign_err:
            msg = f"❌ Signing Exception: {sign_err}"
            try:
                await bot.send_message(user_id, msg, parse_mode="Markdown")
            except Exception:
                await callback.message.reply(msg, parse_mode="Markdown")
            return

        sim_result = await simulate_solana_tx(session, signed_tx_b64)
        
        sim_status = "✅ Transaction Validated & Signed"
        units_consumed = "0 Compute Units"
        if sim_result and "value" in sim_result:
            val = sim_result["value"]
            if val.get("err") is not None:
                sim_status = f"⚠️ Simulation Notice: `{val.get('err')}`"
            else:
                sim_status = "✅ SIMULATION PASSED"
            units_consumed = f"{val.get('unitsConsumed', 0):,} Compute Units"

        receipt_text = (
            f"⚡ **1-CLICK DESK: ORDER SIMULATED**\n\n"
            f"🪙 **Token:** `{token_mint[:6]}...{token_mint[-4:]}`\n"
            f"📥 **Order Size:** `{amount_sol} SOL`\n"
            f"📤 **Tokens Expected:** `{out_tokens:,}`\n"
            f"💰 **Developer Fee (1.0%):** `+{dev_fee_sol:.4f} SOL`\n"
            f"🏦 **Treasury Receiver:** `{TREASURY_WALLET[:6]}...{TREASURY_WALLET[-4:]}`\n\n"
            f"🛡️ **Local Signing:** `solders Keypair Verified`\n"
            f"⛽ **Compute Units:** `{units_consumed}`\n"
            f"📡 **Execution Status:**\n{sim_status}\n\n"
            f"*(Dry-run executed at $0 cost)*"
        )
        try:
            await bot.send_message(user_id, receipt_text, parse_mode="Markdown")
        except Exception:
            await callback.message.reply(receipt_text, parse_mode="Markdown")

@dp.callback_query(F.data == "wallet:check")
async def handle_wallet_button(callback: types.CallbackQuery):
    async with aiohttp.ClientSession() as session:
        sol_bal = await get_wallet_balance(session, BURNER_PUBKEY)
        await callback.answer(f"Burner Balance: {sol_bal:.4f} SOL", show_alert=True)

@dp.callback_query(F.data == "sub:paywall")
async def handle_paywall_callback(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    paywall_text = (
        "💳 **VIP SUBSCRIPTION CHECKOUT**\n\n"
        f"• **Rate:** `{MONTHLY_SUB_PRICE_SOL} SOL / 30 Days`\n"
        f"• **Treasury Wallet:**\n`{TREASURY_WALLET}`\n\n"
        f"1. Send `{MONTHLY_SUB_PRICE_SOL} SOL` to the Treasury.\n"
        f"2. Your Telegram User ID is: `{user_id}`\n"
        "3. Forward your Transaction Hash + User ID to the admin to activate."
    )
    try:
        await bot.send_message(user_id, paywall_text, parse_mode="Markdown")
    except Exception:
        await callback.message.reply(paywall_text, parse_mode="Markdown")
    await callback.answer()

# --- ADMIN MANAGEMENT COMMANDS ---
@dp.message(Command("grant"))
async def admin_grant_handler(message: types.Message):
    if str(message.from_user.id) != str(ADMIN_USER_ID):
        return

    parts = message.text.split()
    if len(parts) < 3:
        await message.reply("Usage: `/grant  `", parse_mode="Markdown")
        return

    try:
        target_uid = int(parts[1])
        days = int(parts[2])
        new_expiry = add_subscription(target_uid, days)
        await message.reply(f"✅ Granted **{days} days** of VIP access to `{target_uid}`.\nExpires: `{new_expiry}`", parse_mode="Markdown")
    except ValueError:
        await message.reply("❌ Error: Invalid user ID or duration format.")

@dp.message(Command("revoke"))
async def admin_revoke_handler(message: types.Message):
    if str(message.from_user.id) != str(ADMIN_USER_ID):
        return

    parts = message.text.split()
    if len(parts) < 2:
        await message.reply("Usage: `/revoke `", parse_mode="Markdown")
        return

    try:
        target_uid = int(parts[1])
        if remove_subscription(target_uid):
            await message.reply(f"🚫 Revoked VIP access for `{target_uid}`.", parse_mode="Markdown")
        else:
            await message.reply(f"⚠️ User `{target_uid}` was not in the database.", parse_mode="Markdown")
    except ValueError:
        await message.reply("❌ Error: Invalid user ID.")

@dp.message(Command("subscribers"))
async def admin_subscribers_handler(message: types.Message):
    if str(message.from_user.id) != str(ADMIN_USER_ID):
        return

    subs = load_subscribers()
    if not subs:
        await message.reply("📋 No active subscribers recorded yet.", parse_mode="Markdown")
        return

    lines = ["📋 **ACTIVE VIP SUBSCRIBERS**\n"]
    now = datetime.now()
    for uid, data in subs.items():
        expiry_dt = datetime.strptime(data["expires_at"], "%Y-%m-%d %H:%M:%S")
        status = "🟢 Active" if expiry_dt > now else "🔴 Expired"
        lines.append(f"• `{uid}`: {status} (Expires: `{data['expires_at']}`)")

    await message.reply("\n".join(lines), parse_mode="Markdown")

# --- USER SELF-SERVICE COMMANDS ---
@dp.message(Command("subscribe"))
async def subscribe_handler(message: types.Message):
    user_id = message.from_user.id
    text = (
        "⭐ **JOIN THE SOLANA ALPHA DESK VIP**\n\n"
        "Unlock full access to 1-click execution for filtered Smart Money alerts.\n\n"
        f"• **Pricing:** `{MONTHLY_SUB_PRICE_SOL} SOL / 30 Days`\n"
        f"• **Destination Wallet:**\n`{TREASURY_WALLET}`\n\n"
        f"📋 **Your User ID:** `{user_id}`\n\n"
        "After transferring SOL, send your TX hash and User ID to the desk admin."
    )
    await message.answer(text, reply_markup=build_paywall_keyboard(), parse_mode="Markdown")

@dp.message(Command("status"))
async def status_handler(message: types.Message):
    user_id = message.from_user.id
    if is_user_subscribed(user_id):
        subs = load_subscribers()
        exp = subs.get(str(user_id), {}).get("expires_at", "Lifetime (Owner Clearance)")
        await message.answer(f"✅ **VIP Status: ACTIVE**\nMembership valid until: `{exp}`", parse_mode="Markdown")
    else:
        await message.answer("❌ **VIP Status: INACTIVE**\nUse `/subscribe` to activate your access.", parse_mode="Markdown")

@dp.message(Command("id"))
async def id_handler(message: types.Message):
    await message.answer(f"🆔 **Your Telegram User ID:** `{message.from_user.id}`", parse_mode="Markdown")

@dp.message(Command("wallet"))
async def wallet_handler(message: types.Message):
    async with aiohttp.ClientSession() as session:
        sol_bal = await get_wallet_balance(session, BURNER_PUBKEY)
        text = (
            "💼 **BOT TRADING VAULT & ROUTING STATUS**\n\n"
            f"🔑 **Trading Burner Wallet:**\n`{BURNER_PUBKEY}`\n"
            f"💰 **Live Balance:** `{sol_bal:.4f} SOL`\n\n"
            f"🏦 **Fee Treasury (1.0% Payout Destination):**\n`{TREASURY_WALLET}`\n\n"
            "🛡️ *Ready for live alerts and 1-click routing.*"
        )
        await message.answer(text, parse_mode="Markdown")

@dp.message(Command("test"))
async def test_handler(message: types.Message):
    test_mint = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
    async with aiohttp.ClientSession() as session:
        dex_info = await fetch_dexscreener_info(session, test_mint)
        ticker = dex_info["symbol"] if dex_info else "BONK"
        name = dex_info["name"] if dex_info else "Bonk"
        mcap = f"${dex_info['mcap']:,.0f}" if dex_info and dex_info['mcap'] else "N/A"
        liq = f"${dex_info['liquidity']:,.0f}" if dex_info and dex_info['liquidity'] else "N/A"

        test_alert = (
            f"🚨 **SMART MONEY BUY DETECTED (SIMULATION)**\n\n"
            f"👤 **Trader:** `Alpha Whale #1`\n"
            f"🪙 **Token:** **${ticker}** ({name})\n"
            f"📊 **Market Cap:** `{mcap}` | **Liquidity:** `{liq}`\n"
            f"📋 **Mint:** `{test_mint}`\n\n"
            f"⚡ *Tap any button below to test Jupiter 1.0% execution:*"
        )
        try:
            await bot.send_message(
                chat_id=TARGET_CHAT_ID,
                text=test_alert,
                reply_markup=build_swap_keyboard(test_mint),
                parse_mode="Markdown",
                disable_web_page_preview=True
            )
            if str(message.chat.id) != str(TARGET_CHAT_ID):
                await message.reply(f"✅ Dispatched test simulation card directly to VIP channel `{TARGET_CHAT_ID}`!")
        except Exception as e:
            await message.reply(f"❌ Failed to dispatch to channel (`{TARGET_CHAT_ID}`): {e}")

@dp.message(Command("start"))
async def start_handler(message: types.Message):
    await message.answer(
        "⚡ **Solana Alpha Radar & 1-Click Execution Desk: Online**\n\n"
        "• Background telemetry is monitoring smart wallets\n"
        "• Safety Filters Active (Liq > $15K, MC > $30K)\n"
        "• `/status` - Check your VIP subscription\n"
        "• `/subscribe` - View payment instructions\n"
        "• `/wallet` - View your execution vault\n"
        "• `/test` - Broadcast a test signal to the VIP channel",
        parse_mode="Markdown"
    )

# --- TELEMETRY BACKGROUND LOOP ---
async def wallet_tracker_loop():
    global seen_signatures
    print("Telemetry Decoder active. Polling target trader wallets...")

    async with aiohttp.ClientSession() as session:
        for wallet in WATCHED_WALLETS.keys():
            sigs = await rpc_call(session, "getSignaturesForAddress", [wallet, {"limit": 10}])
            if sigs:
                for s in sigs:
                    seen_signatures.add(s["signature"])

        while True:
            try:
                now_str = datetime.now().strftime("%H:%M:%S")
                for wallet, label in WATCHED_WALLETS.items():
                    sigs = await rpc_call(session, "getSignaturesForAddress", [wallet, {"limit": 3}])
                    if not sigs:
                        continue

                    for s in reversed(sigs):
                        sig = s["signature"]
                        if sig not in seen_signatures:
                            seen_signatures.add(sig)

                            tx_data = await rpc_call(
                                session, 
                                "getParsedTransaction", 
                                [sig, {"maxSupportedTransactionVersion": 0, "encoding": "jsonParsed"}]
                            )
                            if not tx_data:
                                continue

                            mint = extract_token_mint(tx_data, wallet)
                            if not mint:
                                continue

                            dex_info = await fetch_dexscreener_info(session, mint)
                            ticker = dex_info["symbol"] if dex_info else "NEW TOKEN"
                            name = dex_info["name"] if dex_info else "Unknown"
                            
                            # Safety Filter Guardrails
                            mcap_val = dex_info.get("mcap", 0) if dex_info else 0
                            liq_val = dex_info.get("liquidity", 0) if dex_info else 0

                            if liq_val < 15_000 or mcap_val < 30_000:
                                print(f"[{now_str} FILTERED] Discarded low-liquidity token ({ticker}) - MC: ${mcap_val:,.0f} | Liq:{liq_val:,.0f}")
                                continue

                            mcap = f"${mcap_val:,.0f}" if mcap_val else "N/A"
                            liq = f"${liq_val:,.0f}" if liq_val else "N/A"

                            alert_msg = (
                                f"🚨 **SMART MONEY BUY DETECTED**\n\n"
                                f"👤 **Trader:** `{label}`\n"
                                f"🪙 **Token:** **${ticker}** ({name})\n"
                                f"📊 **Market Cap:** `{mcap}` | **Liquidity:** `{liq}`\n"
                                f"📋 **Mint:** `{mint}`\n\n"
                                f"⚡ *1-Click Execution Options:*"
                            )

                            await bot.send_message(
                                chat_id=TARGET_CHAT_ID,
                                text=alert_msg,
                                reply_markup=build_swap_keyboard(mint),
                                parse_mode="Markdown",
                                disable_web_page_preview=True
                            )
                            print(f"[{now_str} LIVE ALERT] Dispatched 1-click alert for ${ticker} to VIP channel")

                print(f"[{now_str}] Polling active... waiting for signals.")

            except Exception as e:
                print(f"[Tracker Loop Error]: {e}")

            await asyncio.sleep(25)

# ==========================================
# 5. INITIALIZATION ENTRY POINT
# ==========================================
async def main():
    load_dotenv()
    
    print("==================================================")
    print("⚡ APEX ALPHA RADAR INITIALIZED")
    print(f"🎯 Target Channel ID: {os.getenv('YOUR_TELEGRAM_CHAT_ID')}")
    print(f"📡 Monitored Wallets ({len(WATCHED_WALLETS)} Total Active):")
    for addr, label in WATCHED_WALLETS.items():
        print(f"   • [{label}] -> {addr[:6]}...{addr[-4:]}")
    print(f"⏱️ Polling Cycle: 25 seconds")
    print("==================================================")

    # Clear lingering webhooks so Telegram polling starts clean
    await bot.delete_webhook(drop_pending_updates=True)

    # Run the tracker loop and Telegram bot simultaneously
    await asyncio.gather(
        wallet_tracker_loop(),
        dp.start_polling(bot)
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("\n🛑 Apex Alpha terminal manually stopped.")