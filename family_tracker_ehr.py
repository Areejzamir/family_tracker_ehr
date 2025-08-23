import json
import os
import time
import smtplib
import requests
import streamlit as st
from datetime import datetime, timedelta, date
from email.mime.text import MIMEText
from email.utils import formataddr
from web3 import Web3
from eth_account import Account

# -----------------------------
# Page Config
# -----------------------------
st.set_page_config(page_title="Family EHR (Sepolia + IPFS)", page_icon="🏥", layout="centered")

# -----------------------------
# Session State Initialization
# -----------------------------
if "role" not in st.session_state:
    st.session_state.role = None
if "wallet_address" not in st.session_state:
    st.session_state.wallet_address = None
if "account" not in st.session_state:
    st.session_state.account = None
if "ui_tab" not in st.session_state:
    st.session_state.ui_tab = None  # holds current tab label

# -----------------------------
# Load secrets
# -----------------------------
INFURA_ETH_URL = st.secrets["INFURA_ETH_URL"]
PRIVATE_KEY = st.secrets.get("PRIVATE_KEY", "")
CONTRACT_ADDRESS = st.secrets["CONTRACT_ADDRESS"]
PINATA_API_KEY = st.secrets["PINATA_API_KEY"]
PINATA_API_SECRET = st.secrets["PINATA_API_SECRET"]

# (Optional) SMTP settings for email alerts
SMTP_HOST = st.secrets.get("SMTP_HOST", "")
SMTP_PORT = int(st.secrets.get("SMTP_PORT", 587))
SMTP_USER = st.secrets.get("SMTP_USER", "")
SMTP_PASS = st.secrets.get("SMTP_PASS", "")
SMTP_USE_TLS = bool(st.secrets.get("SMTP_USE_TLS", True))
SMTP_FROM_NAME = st.secrets.get("SMTP_FROM_NAME", "Family EHR")
SMTP_FROM_EMAIL = st.secrets.get("SMTP_FROM_EMAIL", SMTP_USER or "no-reply@example.com")

# -----------------------------
# Load ABI
# -----------------------------
with open("abi.json") as f:
    ABI = json.load(f)

# -----------------------------
# Web3 Setup
# -----------------------------
w3 = Web3(Web3.HTTPProvider(INFURA_ETH_URL))
if not w3.is_connected():
    st.error("❌ Could not connect to Sepolia via Infura.")
    st.stop()

contract = w3.eth.contract(address=Web3.to_checksum_address(CONTRACT_ADDRESS), abi=ABI)
CHAIN_ID = 11155111
GATEWAYS = [
    "https://dweb.link/ipfs/",
    "https://cloudflare-ipfs.com/ipfs/",
    "https://gateway.pinata.cloud/ipfs/"
]

# -----------------------------
# Local storage helpers
# -----------------------------
DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)

def _profiles_path(wallet_addr: str) -> str:
    safe = wallet_addr.lower()
    return os.path.join(DATA_DIR, f"family_profiles_{safe}.json")

def _notifications_path(wallet_addr: str) -> str:
    safe = wallet_addr.lower()
    return os.path.join(DATA_DIR, f"notifications_{safe}.json")

def _settings_path(wallet_addr: str) -> str:
    safe = wallet_addr.lower()
    return os.path.join(DATA_DIR, f"user_settings_{safe}.json")

def load_json_file(path: str, default):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return default

def save_json_file(path: str, obj):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2)
    except Exception as e:
        st.warning(f"Could not save file {path}: {e}")

def load_family_profiles(wallet_addr: str) -> dict:
    return load_json_file(_profiles_path(wallet_addr), {})

def save_family_profiles(wallet_addr: str, profiles: dict):
    save_json_file(_profiles_path(wallet_addr), profiles)

def load_notifications(wallet_addr: str) -> list:
    return load_json_file(_notifications_path(wallet_addr), [])

def save_notifications(wallet_addr: str, items: list):
    save_json_file(_notifications_path(wallet_addr), items)

def load_user_settings(wallet_addr: str) -> dict:
    # { "email": "", "email_alerts": true, "reminder_email": true }
    defaults = {"email": "", "email_alerts": False, "reminder_email": False}
    data = load_json_file(_settings_path(wallet_addr), defaults)
    # ensure all keys
    for k, v in defaults.items():
        data.setdefault(k, v)
    return data

def save_user_settings(wallet_addr: str, settings: dict):
    save_json_file(_settings_path(wallet_addr), settings)

# -----------------------------
# IPFS helpers
# -----------------------------
def upload_json_to_ipfs(json_obj: dict) -> str:
    url = "https://api.pinata.cloud/pinning/pinJSONToIPFS"
    headers = {"pinata_api_key": PINATA_API_KEY, "pinata_secret_api_key": PINATA_API_SECRET}
    r = requests.post(url, headers=headers, json=json_obj, timeout=60)
    r.raise_for_status()
    return r.json()["IpfsHash"]

def upload_file_to_ipfs(file_bytes: bytes, filename: str) -> str:
    url = "https://api.pinata.cloud/pinning/pinFileToIPFS"
    headers = {"pinata_api_key": PINATA_API_KEY, "pinata_secret_api_key": PINATA_API_SECRET}
    files = {"file": (filename, file_bytes)}
    r = requests.post(url, headers=headers, files=files, timeout=60)
    r.raise_for_status()
    return r.json()["IpfsHash"]

def fetch_json_from_ipfs(cid: str) -> dict:
    for gateway in GATEWAYS:
        try:
            r = requests.get(f"{gateway}{cid}", timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception:
            continue
    st.error(f"❌ Failed to fetch JSON from IPFS for CID: {cid}")
    return {}

def fetch_pdf_bytes_by_cid(pdf_cid: str) -> bytes | None:
    for gateway in GATEWAYS:
        try:
            r = requests.get(f"{gateway}{pdf_cid}", timeout=20)
            if r.status_code == 200 and r.content:
                return r.content
        except Exception:
            continue
    return None

def link_tx(tx_hash: str) -> str:
    return f"https://sepolia.etherscan.io/tx/{tx_hash}"

def link_cid(cid: str) -> str:
    return f"{GATEWAYS[0]}{cid}"

# -----------------------------
# Email helper
# -----------------------------
def send_email_alert(to_email: str, subject: str, body: str) -> bool:
    """Send plain-text email via SMTP settings in secrets.toml. Returns True if queued."""
    if not to_email:
        return False
    if not (SMTP_HOST and SMTP_USER and SMTP_PASS and SMTP_FROM_EMAIL):
        # Missing SMTP config: silently skip (no error that would break flow)
        return False
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = formataddr((SMTP_FROM_NAME, SMTP_FROM_EMAIL))
        msg["To"] = to_email

        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
        if SMTP_USE_TLS:
            server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_FROM_EMAIL, [to_email], msg.as_string())
        server.quit()
        return True
    except Exception:
        # Do not raise in UI; keep app flow smooth
        return False

# -----------------------------
# Tx helper
# -----------------------------
def send_tx(tx_func):
    account = st.session_state.account
    nonce = w3.eth.get_transaction_count(account.address)
    base = w3.eth.gas_price
    max_priority = w3.to_wei("1", "gwei")
    max_fee = base + max_priority * 2
    tx = tx_func.build_transaction({
        "from": account.address,
        "nonce": nonce,
        "chainId": CHAIN_ID,
        "maxPriorityFeePerGas": max_priority,
        "maxFeePerGas": max_fee
    })
    try:
        gas_estimate = w3.eth.estimate_gas(tx)
        tx["gas"] = int(gas_estimate * 1.2)
    except Exception:
        tx["gas"] = 200000
    signed = w3.eth.account.sign_transaction(
        tx,
        bytes.fromhex(PRIVATE_KEY[2:] if PRIVATE_KEY.startswith("0x") else PRIVATE_KEY)
    )
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    return tx_hash.hex()

# -----------------------------
# Notifications: doctor views & reminders
# -----------------------------
def add_doctor_view_notification(patient_wallet: str, doctor_wallet: str, context: dict | None = None):
    """Append a doctor-view event to patient's local notifications, and pin the event to IPFS."""
    items = load_notifications(patient_wallet)
    entry = {
        "type": "doctor_view",
        "patient": patient_wallet,
        "doctor": doctor_wallet,
        "timestamp": int(time.time()),
        "read": False,
        "context": context or {}
    }
    try:
        cid = upload_json_to_ipfs(entry)  # for audit
        entry["ipfs_cid"] = cid
    except Exception:
        entry["ipfs_cid"] = None
    items.append(entry)
    save_notifications(patient_wallet, items)

def mark_all_notifications_read(wallet: str):
    items = load_notifications(wallet)
    dirty = False
    for it in items:
        if not it.get("read", False):
            it["read"] = True
            dirty = True
    if dirty:
        save_notifications(wallet, items)

def unread_count(wallet: str) -> int:
    return len([n for n in load_notifications(wallet) if not n.get("read", False)])

def build_followup_reminders_for_patient(patient_wallet: str, lookahead_days: int = 30) -> list:
    """Reminders from record JSON (key: follow_up_ts)."""
    reminders = []
    try:
        evt = contract.events.RecordAdded()
        events = evt.get_logs(
            from_block=0, to_block="latest",
            argument_filters={"patient": Web3.to_checksum_address(patient_wallet)}
        )
    except Exception:
        events = []

    now_ts = int(time.time())
    horizon_ts = now_ts + lookahead_days * 24 * 3600

    for ev in events:
        cid = ev.args.ipfsHash
        data = fetch_json_from_ipfs(cid)
        fup = data.get("follow_up_ts")
        if not fup:
            continue
        try:
            fup = int(fup)
        except Exception:
            continue

        fam_meta = data.get("family_member", {})
        fam_label = "Unassigned / Not specified"
        if fam_meta:
            nm = fam_meta.get("name", "Unnamed")
            rel = fam_meta.get("relation", "Unknown")
            fam_label = f"{nm} ({rel})"

        status = "upcoming" if now_ts <= fup <= horizon_ts else ("overdue" if fup < now_ts else "future")
        if status in ("upcoming", "overdue"):
            reminders.append({
                "type": "follow_up",
                "patient": patient_wallet,
                "record_cid": cid,
                "status": status,
                "follow_up_ts": fup,
                "family": fam_label,
                "note": data.get("condition", "")[:140]
            })
    reminders.sort(key=lambda r: (0 if r["status"] == "overdue" else 1, r["follow_up_ts"]))
    return reminders

# -----------------------------
# Login Page
# -----------------------------
if st.session_state.role is None:
    st.title("🏥 Family Tracker EHR")
    st.markdown("Select your role and enter your wallet address:")

    role_input = st.radio("I am a:", ["Patient", "Doctor"])
    wallet_input = st.text_input("Enter your Wallet Address (0x...)")

    if st.button("Login"):
        if wallet_input.startswith("0x") and len(wallet_input) == 42:
            st.session_state.role = role_input
            st.session_state.wallet_address = wallet_input
            if role_input == "Patient":
                st.session_state.account = Account.from_key(
                    PRIVATE_KEY[2:] if PRIVATE_KEY.startswith("0x") else PRIVATE_KEY
                )
            else:
                st.session_state.account = None
            st.session_state.ui_tab = None
            st.experimental_rerun()
        else:
            st.error("❌ Invalid wallet address.")
    st.stop()

# -----------------------------
# Sidebar: Logout + Notification Bell (Patient)
# -----------------------------
with st.sidebar:
    cols = st.columns([1, 1.2])
    with cols[0]:
        if st.button("Logout"):
            st.session_state.role = None
            st.session_state.wallet_address = None
            st.session_state.account = None
            st.session_state.ui_tab = None
            st.experimental_rerun()
    if st.session_state.role == "Patient":
        unread = unread_count(st.session_state.wallet_address)
        label = f"🔔 ({unread})" if unread > 0 else "🔔 (0)"
        with cols[1]:
            if st.button(label, help="View Notifications"):
                st.session_state.ui_tab = "Notifications"
                st.experimental_rerun()

# -----------------------------
# Sidebar Tabs
# -----------------------------
if st.session_state.role == "Patient":
    tabs = [
        "Add Record",
        "View Record",
        "Grant Access",
        "Revoke Access",
        "View Granted Access",
        "Family Members",
        "Family Summary",
        "Notifications",     # NEW
        "Settings"           # NEW (email settings)
    ]
elif st.session_state.role == "Doctor":
    tabs = ["View Patient Records"]

# compute index from session_state.ui_tab
def _tab_index(tabs_list, current_label):
    if current_label and current_label in tabs_list:
        return tabs_list.index(current_label)
    return 0

tab = st.sidebar.radio(f"🔧 {st.session_state.role} Dashboard", tabs, index=_tab_index(tabs, st.session_state.ui_tab), key="sidebar_tabs")
# keep session_state.ui_tab synced
st.session_state.ui_tab = tab

# -----------------------------
# Utilities: family members
# -----------------------------
def ensure_profiles_loaded():
    if st.session_state.role == "Patient":
        wallet = st.session_state.wallet_address
        if wallet and not hasattr(st.session_state, "family_loaded_for") or st.session_state.get("family_loaded_for") != wallet:
            st.session_state.family_profiles = load_family_profiles(wallet)
            st.session_state.family_loaded_for = wallet

def _next_member_id() -> str:
    profiles = st.session_state.get("family_profiles", {})
    existing = list(profiles.keys())
    if not existing:
        return "mem-1"
    nums = []
    for k in existing:
        try:
            if k.startswith("mem-"):
                nums.append(int(k.split("-")[1]))
        except Exception:
            pass
    nxt = max(nums) + 1 if nums else 1
    return f"mem-{nxt}"

def _member_label(mem_id: str, data: dict) -> str:
    nm = data.get("name", "Unnamed")
    rel = data.get("relation", "Unknown")
    return f"{nm} ({rel}) • {mem_id}"

def list_member_options(include_unassigned=True):
    profiles = st.session_state.get("family_profiles", {})
    items = []
    for mid, meta in profiles.items():
        items.append((_member_label(mid, meta), mid))
    items.sort(key=lambda x: x[0].lower())
    if include_unassigned:
        items = [("Unassigned / Not specified", "unassigned")] + items
    return items

# Ensure profiles loaded (patient only)
if st.session_state.role == "Patient":
    ensure_profiles_loaded()

# -----------------------------
# Patient Dashboard
# -----------------------------
if st.session_state.role == "Patient":
    account = st.session_state.account

    if tab == "Add Record":
        st.subheader("Add / Update Your Health Record")

        # Assign to Family Member
        st.markdown("**Assign to Family Member (optional):**")
        options = list_member_options(include_unassigned=True)
        labels = [x[0] for x in options]
        ids = [x[1] for x in options]
        sel_label = st.selectbox("Family Member", labels, index=0)
        selected_member_id = ids[labels.index(sel_label)]

        with st.form("record_form"):
            patient_name = st.text_input("Display Name (for this record)")
            age = st.number_input("Age", min_value=0, value=30)
            condition = st.text_area("Health Condition / Notes")
            meds = st.text_input("Medications (comma separated)", value="")
            allergies = st.text_input("Allergies (comma separated)", value="")
            follow_up_date: date | None = st.date_input("Follow-up Date (optional)", value=None, format="YYYY-MM-DD")
            pdf_file = st.file_uploader("Upload PDF (Optional)", type="pdf")
            submit = st.form_submit_button("Upload to IPFS & Save Hash On-Chain")

        if submit:
            if not patient_name or not condition:
                st.error("Please fill in at least Display Name and Health Condition.")
            else:
                pdf_cid = None
                if pdf_file:
                    pdf_bytes = pdf_file.read()
                    pdf_cid = upload_file_to_ipfs(pdf_bytes, pdf_file.name)
                    st.success(f"✅ PDF uploaded to IPFS! CID: `{pdf_cid}`")

                member_meta = {}
                if selected_member_id != "unassigned":
                    member_meta = st.session_state.family_profiles.get(selected_member_id, {})

                follow_up_ts = None
                if isinstance(follow_up_date, date):
                    follow_up_ts = int(datetime.combine(follow_up_date, datetime.min.time()).timestamp())

                record = {
                    "patient": account.address,
                    "name": patient_name,
                    "age": int(age),
                    "condition": condition,
                    "medications": [m.strip() for m in meds.split(",")] if meds else [],
                    "allergies": [a.strip() for a in allergies.split(",")] if allergies else [],
                    "timestamp": int(time.time()),
                    "pdf_cid": pdf_cid,
                    # Family tracking
                    "family_member_id": None if selected_member_id == "unassigned" else selected_member_id,
                    "family_member": member_meta,
                    # Follow-up reminder
                    "follow_up_ts": follow_up_ts
                }

                cid = upload_json_to_ipfs(record)
                st.markdown(f"JSON CID: `{cid}`")
                st.markdown(f"[View JSON on IPFS]({link_cid(cid)})")

                tx_hash = send_tx(contract.functions.addRecord(cid))
                st.success(f"✅ Record saved on-chain! Tx: `{tx_hash}`")
                st.markdown(f"[View on Etherscan]({link_tx(tx_hash)})")

    elif tab == "View Record":
        st.subheader("View Your Records")

        # Filter by member
        options = list_member_options(include_unassigned=True)
        labels = [x[0] for x in options]
        ids = [x[1] for x in options]
        sel_label = st.selectbox("Filter by Family Member", labels, index=0)
        selected_filter_member = ids[labels.index(sel_label)]

        patient_address = st.text_input("Patient Address", value=account.address)
        if st.button("Fetch Records"):
            evt = contract.events.RecordAdded()
            events = evt.get_logs(
                from_block=0, to_block="latest",
                argument_filters={"patient": Web3.to_checksum_address(patient_address)}
            )
            if not events:
                st.info("No records found.")
            else:
                shown = 0
                for idx, ev in enumerate(reversed(events), 1):
                    cid = ev.args.ipfsHash
                    data = fetch_json_from_ipfs(cid)

                    rec_mid = data.get("family_member_id") or "unassigned"
                    if selected_filter_member != "unassigned" and rec_mid != selected_filter_member:
                        continue

                    st.markdown(f"### Record #{idx} • CID: `{cid}`")
                    member_label = "Unassigned"
                    if rec_mid != "unassigned":
                        meta = st.session_state.family_profiles.get(rec_mid, {})
                        member_label = _member_label(rec_mid, meta) if meta else f"{rec_mid}"
                    st.caption(f"Family: {member_label}")

                    fup = data.get("follow_up_ts")
                    if fup:
                        try:
                            fup_dt = datetime.fromtimestamp(int(fup))
                            st.info(f"Follow-up: {fup_dt.strftime('%Y-%m-%d')}")
                        except Exception:
                            pass

                    st.json(data)
                    pdf_cid = data.get("pdf_cid")
                    if pdf_cid:
                        pdf_bytes = fetch_pdf_bytes_by_cid(pdf_cid)
                        if pdf_bytes:
                            st.download_button(
                                label="📄 Download PDF",
                                data=pdf_bytes,
                                file_name=f"record_{idx}.pdf",
                                mime="application/pdf"
                            )
                            st.components.v1.iframe(
                                f"https://docs.google.com/gview?url={link_cid(pdf_cid)}&embedded=true",
                                height=500, scrolling=True
                            )
                    shown += 1
                if shown == 0:
                    st.info("No records for the selected family member.")

    elif tab == "Grant Access":
        st.subheader("Grant Access to Doctor")
        doctor_address = st.text_input("Doctor Wallet Address")
        duration_days = st.number_input("Access Duration (days)", min_value=1, value=2)
        if st.button("Grant Access"):
            try:
                duration_seconds = duration_days * 24 * 60 * 60
                tx_hash = send_tx(contract.functions.grantAccess(
                    Web3.to_checksum_address(doctor_address),
                    duration_seconds
                ))
                st.success(f"✅ Access granted! Tx: `{tx_hash}`")
                st.markdown(f"[View on Etherscan]({link_tx(tx_hash)})")
            except Exception as e:
                st.error(f"❌ Error granting access: {e}")

    elif tab == "Revoke Access":
        st.subheader("Revoke Access from Doctor")
        doctor_address = st.text_input("Doctor Wallet Address")
        if st.button("Revoke Access"):
            try:
                tx_hash = send_tx(contract.functions.revokeAccess(
                    Web3.to_checksum_address(doctor_address)
                ))
                st.success(f"✅ Access revoked! Tx: `{tx_hash}`")
                st.markdown(f"[View on Etherscan]({link_tx(tx_hash)})")
            except Exception as e:
                st.error(f"❌ Error revoking access: {e}")

    elif tab == "View Granted Access":
        st.subheader("Doctors with Access")
        evt = contract.events.AccessGranted()
        events = evt.get_logs(
            from_block=0, to_block="latest",
            argument_filters={"patient": Web3.to_checksum_address(account.address)}
        )
        if not events:
            st.info("No doctors have access.")
        else:
            for idx, ev in enumerate(reversed(events), 1):
                st.write(f"{idx}. Doctor: {ev.args.viewer}, Expiry: {time.ctime(ev.args.expiryTime)}")

    elif tab == "Family Members":
        st.subheader("Family Members")
        profiles = st.session_state.get("family_profiles", {})

        if profiles:
            st.markdown("#### Existing Profiles")
            to_delete = []
            for mid, meta in profiles.items():
                with st.expander(_member_label(mid, meta), expanded=False):
                    col1, col2 = st.columns([3,1])
                    with col1:
                        st.write({
                            "name": meta.get("name"),
                            "relation": meta.get("relation"),
                            "age": meta.get("age"),
                            "gender": meta.get("gender"),
                            "blood": meta.get("blood"),
                            "allergies": meta.get("allergies"),
                            "chronic": meta.get("chronic")
                        })
                    with col2:
                        if st.button(f"🗑️ Delete {mid}", key=f"del-{mid}"):
                            to_delete.append(mid)
            if to_delete:
                for mid in to_delete:
                    profiles.pop(mid, None)
                save_family_profiles(st.session_state.wallet_address, profiles)
                st.session_state.family_profiles = profiles
                st.success("Deleted selected member(s).")
                st.experimental_rerun()
        else:
            st.info("No family members yet. Add one below.")

        st.markdown("---")
        st.markdown("#### Add / Edit Member")

        with st.form("add_member_form", clear_on_submit=True):
            name = st.text_input("Name")
            relation = st.text_input("Relation (e.g., Father, Mother, Child, Spouse)")
            age = st.number_input("Age", min_value=0, value=30)
            gender = st.selectbox("Gender", ["Prefer not to say", "Male", "Female", "Other"], index=0)
            blood = st.text_input("Blood Group (optional)", placeholder="A+, O-, etc.")
            allergies = st.text_input("Allergies (comma separated)")
            chronic = st.text_input("Chronic Conditions (comma separated)")
            submit_add = st.form_submit_button("Save Member")

        if submit_add:
            if not name or not relation:
                st.error("Please provide at least Name and Relation.")
            else:
                mid = _next_member_id()
                profiles[mid] = {
                    "name": name.strip(),
                    "relation": relation.strip(),
                    "age": int(age),
                    "gender": gender,
                    "blood": blood.strip(),
                    "allergies": [a.strip() for a in allergies.split(",")] if allergies else [],
                    "chronic": [c.strip() for c in chronic.split(",")] if chronic else []
                }
                save_family_profiles(st.session_state.wallet_address, profiles)
                st.session_state.family_profiles = profiles
                st.success(f"Saved member `{name}` as `{mid}`.")
                st.experimental_rerun()

    elif tab == "Family Summary":
        st.subheader("Family Summary")
        evt = contract.events.RecordAdded()
        events = evt.get_logs(
            from_block=0, to_block="latest",
            argument_filters={"patient": Web3.to_checksum_address(st.session_state.account.address)}
        )
        counts = {}
        last_ts = {}
        for ev in events:
            cid = ev.args.ipfsHash
            data = fetch_json_from_ipfs(cid)
            mid = data.get("family_member_id") or "unassigned"
            counts[mid] = counts.get(mid, 0) + 1
            ts = int(data.get("timestamp", 0)) if isinstance(data.get("timestamp", 0), int) else 0
            if ts:
                last_ts[mid] = max(last_ts.get(mid, 0), ts)

        if not counts:
            st.info("No records yet.")
        else:
            rows = []
            for mid, cnt in counts.items():
                if mid == "unassigned":
                    label = "Unassigned / Not specified"
                else:
                    meta = st.session_state.family_profiles.get(mid, {})
                    label = _member_label(mid, meta) if meta else mid
                last_str = time.ctime(last_ts.get(mid, 0)) if last_ts.get(mid, 0) else "—"
                rows.append({"Family Member": label, "Total Records": cnt, "Last Updated": last_str})
            rows.sort(key=lambda r: r["Family Member"].lower())
            st.table(rows)

    elif tab == "Notifications":
        st.subheader("Notifications")

        # Follow-up reminders
        reminders = build_followup_reminders_for_patient(st.session_state.wallet_address, lookahead_days=30)
        upcoming = [r for r in reminders if r["status"] == "upcoming"]
        overdue = [r for r in reminders if r["status"] == "overdue"]

        # Doctor view notifications
        activities = load_notifications(st.session_state.wallet_address)

        colA, colB = st.columns(2)
        with colA:
            st.markdown("### ⏰ Follow-up Reminders")
            if not (upcoming or overdue):
                st.info("No upcoming or overdue follow-ups in the next 30 days.")
            else:
                if overdue:
                    st.warning("**Overdue**")
                    for it in overdue:
                        dt = datetime.fromtimestamp(it["follow_up_ts"]).strftime("%Y-%m-%d")
                        st.write(f"• {dt} — {it['family']}: {it['note']}")
                if upcoming:
                    st.success("**Upcoming (≤ 30 days)**")
                    for it in upcoming:
                        dt = datetime.fromtimestamp(it["follow_up_ts"]).strftime("%Y-%m-%d")
                        st.write(f"• {dt} — {it['family']}: {it['note']}")

        with colB:
            st.markdown("### 👀 Doctor Activity")
            if not activities:
                st.info("No doctor views yet.")
            else:
                for it in reversed(activities[-100:]):
                    if it.get("type") == "doctor_view":
                        ts = datetime.fromtimestamp(it["timestamp"]).strftime("%Y-%m-%d %H:%M")
                        doctor = it.get("doctor", "")
                        cid = it.get("ipfs_cid")
                        ref = f" (log CID: `{cid}`)" if cid else ""
                        st.write(f"• {ts}: Doctor `{doctor}` viewed your records{ref}")

        st.markdown("---")
        col1, col2 = st.columns(2)
        with col1:
            if st.button("Mark all as read"):
                mark_all_notifications_read(st.session_state.wallet_address)
                st.experimental_rerun()
        with col2:
            if st.button("Refresh"):
                st.experimental_rerun()

    elif tab == "Settings":
        st.subheader("Notification Settings")
        settings = load_user_settings(st.session_state.wallet_address)
        with st.form("settings_form"):
            email = st.text_input("Notification Email", value=settings.get("email", ""), placeholder="your@email.com")
            email_alerts = st.checkbox("Email me when a doctor views my records", value=settings.get("email_alerts", False))
            reminder_email = st.checkbox("Email me upcoming/overdue follow-up reminders when I open the app", value=settings.get("reminder_email", False))
            save_btn = st.form_submit_button("Save Settings")
        if save_btn:
            settings.update({"email": email.strip(), "email_alerts": bool(email_alerts), "reminder_email": bool(reminder_email)})
            save_user_settings(st.session_state.wallet_address, settings)
            st.success("Settings saved.")

        # If reminder_email enabled, send a summary email right now (best-effort)
        settings = load_user_settings(st.session_state.wallet_address)
        if settings.get("reminder_email") and settings.get("email"):
            reminders = build_followup_reminders_for_patient(st.session_state.wallet_address, lookahead_days=30)
            if reminders:
                # Build plain text summary
                lines = []
                for it in reminders:
                    dt = datetime.fromtimestamp(it["follow_up_ts"]).strftime("%Y-%m-%d")
                    prefix = "OVERDUE" if it["status"] == "overdue" else "UPCOMING"
                    lines.append(f"[{prefix}] {dt} — {it['family']}: {it['note']}")
                body = "Here are your follow-up reminders:\n\n" + "\n".join(lines)
                send_email_alert(settings["email"], "Your Follow-up Reminders — Family EHR", body)

# -----------------------------
# Doctor Dashboard
# -----------------------------
elif st.session_state.role == "Doctor":
    st.subheader("View Records of Patients Who Granted You Access")

    evt_access = contract.events.AccessGranted()
    events_access = evt_access.get_logs(
        from_block=0, to_block="latest",
        argument_filters={"viewer": Web3.to_checksum_address(st.session_state.wallet_address)}
    )
    patients_with_access = list({ev.args.patient for ev in events_access})

    if not patients_with_access:
        st.info("No patients have granted you access yet.")
    else:
        # Build quick map (latest record name)
        patient_map = {}
        for p_addr in patients_with_access:
            rec_events = contract.events.RecordAdded().get_logs(
                from_block=0, to_block="latest",
                argument_filters={"patient": Web3.to_checksum_address(p_addr)}
            )
            if rec_events:
                latest_event = rec_events[-1]
                data = fetch_json_from_ipfs(latest_event.args.ipfsHash)
                patient_map[p_addr] = data.get("name", "Unknown")

        patient_filter = st.text_input("Search Patient by Address or Name")
        filtered_patients = {
            addr: name for addr, name in patient_map.items()
            if patient_filter.lower() in addr.lower() or patient_filter.lower() in name.lower()
        }

        if not filtered_patients:
            st.info("No patients match your search.")
        else:
            import pandas as pd
            summary_rows = []
            for addr, _name in filtered_patients.items():
                rec_events = contract.events.RecordAdded().get_logs(
                    from_block=0, to_block="latest",
                    argument_filters={"patient": Web3.to_checksum_address(addr)}
                )
                if rec_events:
                    latest_event = rec_events[-1]
                    data = fetch_json_from_ipfs(latest_event.args.ipfsHash)
                    fam_meta = data.get("family_member", {})
                    fam_label = "Unassigned"
                    if fam_meta:
                        nm = fam_meta.get("name", "Unnamed")
                        rel = fam_meta.get("relation", "Unknown")
                        fam_label = f"{nm} ({rel})"
                    summary_rows.append({
                        "Patient Address": addr,
                        "Latest Name": data.get("name", ""),
                        "Latest Age": data.get("age", ""),
                        "Family (latest record)": fam_label,
                        "Condition Summary": data.get("condition", "")
                    })
            if summary_rows:
                df = pd.DataFrame(summary_rows)
                st.dataframe(df, use_container_width=True)

            selected_addr = st.selectbox("Select Patient to View Records", list(filtered_patients.keys()))
            fam_filter = st.text_input("Filter by Family Member (type name or relation; optional)")

            if st.button("Fetch Records for Selected Patient"):
                # check access
                has_access = contract.functions.checkAccess(
                    Web3.to_checksum_address(selected_addr),
                    st.session_state.wallet_address
                ).call()
                if not has_access:
                    st.warning("You do not have access to this patient's records.")
                else:
                    # Log activity & email patient (if enabled)
                    try:
                        add_doctor_view_notification(
                            patient_wallet=selected_addr,
                            doctor_wallet=st.session_state.wallet_address,
                            context={"action": "view_records"}
                        )
                        # Try email if patient opted-in
                        settings = load_user_settings(selected_addr)
                        if settings.get("email_alerts") and settings.get("email"):
                            ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
                            body = (
                                f"Doctor wallet {st.session_state.wallet_address} viewed your Family EHR on {ts}.\n"
                                f"If this wasn't you, please revoke access in your dashboard."
                            )
                            send_email_alert(settings["email"], "Doctor viewed your records — Family EHR", body)
                    except Exception:
                        pass

                    # fetch and show records
                    rec_events = contract.events.RecordAdded().get_logs(
                        from_block=0, to_block="latest",
                        argument_filters={"patient": Web3.to_checksum_address(selected_addr)}
                    )
                    if not rec_events:
                        st.info("No records found.")
                    else:
                        shown = 0
                        for idx, ev in enumerate(reversed(rec_events), 1):
                            cid = ev.args.ipfsHash
                            data = fetch_json_from_ipfs(cid)
                            fam_meta = data.get("family_member", {})
                            if fam_filter.strip():
                                needle = fam_filter.lower()
                                label_text = f"{fam_meta.get('name','')} {fam_meta.get('relation','')}".lower()
                                if needle not in label_text:
                                    continue

                            st.markdown(f"### Record #{idx} • CID: `{cid}`")
                            if fam_meta:
                                st.caption(f"Family: {fam_meta.get('name','Unnamed')} ({fam_meta.get('relation','Unknown')})")
                            else:
                                st.caption("Family: Unassigned / Not specified")

                            fup = data.get("follow_up_ts")
                            if fup:
                                try:
                                    fup_dt = datetime.fromtimestamp(int(fup))
                                    st.info(f"Follow-up: {fup_dt.strftime('%Y-%m-%d')}")
                                except Exception:
                                    pass

                            st.json(data)
                            pdf_cid = data.get("pdf_cid")
                            if pdf_cid:
                                pdf_bytes = fetch_pdf_bytes_by_cid(pdf_cid)
                                if pdf_bytes:
                                    st.download_button(
                                        label="📄 Download PDF",
                                        data=pdf_bytes,
                                        file_name=f"record_{idx}.pdf",
                                        mime="application/pdf"
                                    )
                                    st.markdown(f"[Open PDF via IPFS]({link_cid(pdf_cid)})")
                            shown += 1
                        if shown == 0:
                            st.info("No records match the family filter.")
