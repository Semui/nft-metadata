import os
import json
import time
import requests
import pandas as pd
from collections import defaultdict
from web3 import Web3
from datetime import datetime

# ------------------ Config ------------------
with open('config.json') as f:
    cfg = json.load(f)

ETHERSCAN_KEY = cfg['eth_api_key']
ETH_NODE_URL  = cfg['eth_node_url']          # e.g., https://eth-mainnet.g.alchemy.com/v2/YOUR_KEY
ADDRESSES     = [a.lower() for a in cfg['addresses']]

BASE_URL = "https://api.etherscan.io/v2/api"
CHAIN_ID = 1  # Ethereum mainnet for Etherscan V2

# ------------------ Web3 --------------------
w3 = Web3(Web3.HTTPProvider(ETH_NODE_URL, request_kwargs={"timeout": 20}))

def assert_web3_ready():
    if not w3.is_connected():
        raise RuntimeError("Web3 not connected. Check ETH_NODE_URL and network/firewall.")
    cid = w3.eth.chain_id
    if cid != CHAIN_ID:
        raise RuntimeError(f"RPC chain_id={cid} != {CHAIN_ID} (Ethereum mainnet). Point ETH_NODE_URL to mainnet.")
assert_web3_ready()

# ------------------ ABIs --------------------
ERC721_META_ABI_STR = [
    {"constant": True, "inputs": [], "name": "symbol", "outputs": [{"name": "", "type": "string"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "name",   "outputs": [{"name": "", "type": "string"}], "type": "function"},
    {"constant": True, "inputs": [{"name": "tokenId", "type": "uint256"}], "name": "tokenURI",
     "outputs": [{"name": "", "type": "string"}], "type": "function"}
]
# Some contracts use bytes32 for symbol/name
ERC721_META_ABI_BYTES32 = [
    {"constant": True, "inputs": [], "name": "symbol", "outputs": [{"name": "", "type": "bytes32"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "name",   "outputs": [{"name": "", "type": "bytes32"}], "type": "function"}
]

# web3.py v6 uses snake_case:
def checksum(addr):
    return Web3.to_checksum_address(addr)

def _decode_b32(val: bytes):
    if isinstance(val, (bytes, bytearray)):
        return val.rstrip(b"\x00").decode("utf-8", errors="ignore")
    return None

def get_symbol_and_name(contract_addr):
    """Try string ABI, then bytes32 fallback. Return ('','') on failure."""
    try:
        c = w3.eth.contract(address=checksum(contract_addr), abi=ERC721_META_ABI_STR)
        sym = c.functions.symbol().call()
        nm  = c.functions.name().call()
        return sym or "", nm or ""
    except Exception as e1:
        try:
            c2 = w3.eth.contract(address=checksum(contract_addr), abi=ERC721_META_ABI_BYTES32)
            sym_b = _decode_b32(c2.functions.symbol().call()) or ""
            nm_b  = _decode_b32(c2.functions.name().call()) or ""
            return sym_b, nm_b
        except Exception as e2:
            print(f"[warn] symbol/name failed for {contract_addr}: {e1} / {e2}")
            return "", ""

def get_token_uri(contract_addr, token_id):
    """Call tokenURI; return None on failure (with a warning)."""
    try:
        c = w3.eth.contract(address=checksum(contract_addr), abi=ERC721_META_ABI_STR)
        return c.functions.tokenURI(int(token_id)).call()
    except Exception as e:
        print(f"[warn] tokenURI failed for {contract_addr} #{token_id}: {e}")
        return None

# ------------------ Etherscan helpers -----------------
def etherscan_v2_tokennfttx(address, page=1, offset=1000):
    params = {
        "chainid": CHAIN_ID,
        "module": "account",
        "action": "tokennfttx",
        "address": address,
        "startblock": 0,
        "endblock": 99999999,
        "sort": "asc",
        "page": page,
        "offset": offset,
        "apikey": ETHERSCAN_KEY
    }
    r = requests.get(BASE_URL, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    if data.get("status") != "1":
        if (data.get("message") or "").lower() != "no transactions found":
            print(f"[warn] tokennfttx status={data.get('status')} msg={data.get('message')}")
        return []
    res = data.get("result", [])
    return res if isinstance(res, list) else []

def fetch_all_erc721_transfers(address):
    all_rows, page, per_page = [], 1, 1000
    while True:
        batch = etherscan_v2_tokennfttx(address, page=page, offset=per_page)
        if not batch:
            break
        all_rows.extend(batch)
        if len(batch) < per_page:
            break
        page += 1
        time.sleep(0.1)  # be polite
    return all_rows

def build_erc721_holdings_and_event_symbols(events, wallet_lower):
    """
    Returns:
      holdings: dict(contract -> set(tokenId))
      event_symbols: dict(contract -> {'symbol': str, 'name': str}) from Etherscan events
    """
    holdings = defaultdict(set)
    event_symbols = defaultdict(dict)

    for e in events:
        contract = e['contractAddress'].lower()
        token_id = e['tokenID']
        frm = e['from'].lower()
        to  = e['to'].lower()

        # Track holdings
        if to == wallet_lower:
            holdings[contract].add(token_id)
        if frm == wallet_lower and token_id in holdings[contract]:
            holdings[contract].discard(token_id)

        # Capture Etherscan-provided fallbacks
        sym = (e.get('tokenSymbol') or "").strip()
        nm  = (e.get('tokenName')   or "").strip()
        if sym and 'symbol' not in event_symbols[contract]:
            event_symbols[contract]['symbol'] = sym
        if nm and 'name' not in event_symbols[contract]:
            event_symbols[contract]['name'] = nm

    return holdings, event_symbols

# ------------------ Metadata resolve -----------------
def ipfs_to_http(uri, gateway="https://ipfs.io/ipfs/"):
    if not uri:
        return None
    if uri.startswith("ipfs://"):
        path = uri.removeprefix("ipfs://")
        if path.startswith("ipfs/"):
            path = path[len("ipfs/"):]
        return gateway + path
    return uri

def maybe_fill_template_id(uri, token_id):
    """Replace common {id} templates if present (some 721s use it too)."""
    if not uri:
        return uri
    if "{id}" in uri:
        hexid = format(int(token_id), "x").zfill(64)  # 64-char lowercase hex
        return uri.replace("{id}", hexid)
    return uri

_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "nft-metadata-fetcher/1.0"})

def fetch_metadata(uri):
    """
    Return (metadata_json_dict, resolved_metadata_url) or (None, url) on failure
    """
    if not uri:
        return None, None

    if uri.startswith("data:application/json;base64,"):
        import base64
        b64 = uri.split(",", 1)[1]
        try:
            meta = json.loads(base64.b64decode(b64))
            return meta, uri
        except Exception as e:
            print(f"[warn] failed to decode base64 metadata: {e}")
            return None, uri

    url = ipfs_to_http(uri)
    for attempt in range(2):
        try:
            r = _SESSION.get(url, timeout=25)
            r.raise_for_status()
            try:
                return r.json(), url
            except Exception as je:
                print(f"[warn] metadata not JSON at {url}: {je}")
                return None, url
        except Exception as e:
            if attempt == 0:
                time.sleep(0.3)
            else:
                print(f"[warn] metadata fetch failed {url}: {e}")
                return None, url

def extract_image_url(meta):
    if not isinstance(meta, dict):
        return None
    for k in ("image", "image_url", "image_data"):
        v = meta.get(k)
        if v:
            return ipfs_to_http(v)
    return None

# ------------------ Main flow ----------------
rows = []

for wallet in ADDRESSES:
    events = fetch_all_erc721_transfers(wallet)
    holdings, event_symbols = build_erc721_holdings_and_event_symbols(events, wallet)

    sym_cache = {}
    name_cache = {}

    for contract, token_ids in holdings.items():
        if not token_ids:
            continue

        # Etherscan fallbacks
        fallback_sym = event_symbols.get(contract, {}).get('symbol', "")
        fallback_nm  = event_symbols.get(contract, {}).get('name', "")

        # on-chain (with fallback)
        if contract not in sym_cache:
            onchain_sym, onchain_nm = get_symbol_and_name(contract)
            sym_cache[contract]  = onchain_sym or fallback_sym
            name_cache[contract] = onchain_nm  or fallback_nm
            if not onchain_sym and fallback_sym:
                print(f"[info] using Etherscan symbol fallback for {contract}: {fallback_sym}")
            if not onchain_nm and fallback_nm:
                print(f"[info] using Etherscan name fallback for {contract}: {fallback_nm}")

        for token_id in sorted(token_ids, key=lambda x: int(x)):
            token_uri = get_token_uri(contract, token_id)
            token_uri = maybe_fill_template_id(token_uri, token_id) if token_uri else None

            meta, resolved_meta_url = fetch_metadata(token_uri) if token_uri else (None, None)
            image_url = extract_image_url(meta)

            rows.append({
                "wallet": wallet,
                "contract": contract,
                "token_id": token_id,
                "token_symbol": sym_cache[contract] or "",
                "token_name": name_cache[contract] or "",
                "token_uri": token_uri or "",
                "metadata_url": resolved_meta_url or "",
                "image_url": image_url or ""
            })
            time.sleep(0.05)  # gentle pacing

# ------------------ Output -------------------
df = pd.DataFrame(rows)
current_date = datetime.now().strftime("%m%d%Y")
out_dir = os.path.join("Data", f"erc721_holdings_{current_date}")
os.makedirs(out_dir, exist_ok=True)
out_path = os.path.join(out_dir, f"erc721_holdings_{current_date}.csv")
df.to_csv(out_path, index=False)
print(f"Saved: {out_path}")
