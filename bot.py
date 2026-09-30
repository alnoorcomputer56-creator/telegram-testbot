import requests
import time
import json
import os
import uuid
import threading
import random
import re
import html
import pyotp
from collections import Counter 
from concurrent.futures import ThreadPoolExecutor
from bs4 import BeautifulSoup
from datetime import datetime 
from urllib.parse import urljoin
from flask import Flask, Response

# Portable local database.  The complete database is kept in one JSON file,
# so it can be copied to another host and restored without an external service.
DATA_FILE = os.environ.get("BOT_DATA_FILE", "bot_data.json")
_local_db_lock = threading.RLock()

class _Increment:
    def __init__(self, amount):
        self.amount = amount

class _ServerTimestamp:
    pass

class _Snapshot:
    def __init__(self, doc_id, data=None):
        self.id = str(doc_id)
        self._data = data
        self.exists = data is not None
    def to_dict(self):
        return dict(self._data or {})

class _Document:
    def __init__(self, store, collection, doc_id):
        self.store, self.collection, self.doc_id = store, collection, str(doc_id)
    def get(self):
        with _local_db_lock:
            return _Snapshot(self.doc_id, self.store.get(self.collection, {}).get(self.doc_id))
    def set(self, data, merge=False):
        with _local_db_lock:
            bucket = self.store.setdefault(self.collection, {})
            current = bucket.get(self.doc_id, {}) if merge else {}
            bucket[self.doc_id] = _merge_local(current, data)
            _write_local_db()
    def update(self, data):
        return self.set(data, merge=True)

class _Query:
    def __init__(self, store, collection, items=None):
        self.store, self.collection = store, collection
        self.items = items
    def where(self, field, op, value):
        rows = self._rows()
        if op == ">" : rows = [(i, d) for i, d in rows if d.get(field, 0) > value]
        elif op == "==" : rows = [(i, d) for i, d in rows if d.get(field) == value]
        return _Query(self.store, self.collection, rows)
    def order_by(self, field, direction="ASCENDING"):
        rows = self._rows()
        rows.sort(key=lambda x: x[1].get(field, 0) or 0, reverse=direction == "DESCENDING")
        return _Query(self.store, self.collection, rows)
    def limit(self, count):
        return _Query(self.store, self.collection, self._rows()[:count])
    def _rows(self):
        if self.items is not None: return list(self.items)
        return list(self.store.get(self.collection, {}).items())
    def stream(self):
        return [_Snapshot(i, d) for i, d in self._rows()]
    def select(self, _fields):
        return self

class _Collection(_Query):
    def document(self, doc_id):
        return _Document(self.store, self.collection, doc_id)

class _Batch:
    def __init__(self, store):
        self.store, self.operations = store, []
    def update(self, doc, data):
        self.operations.append((doc, data))
    def commit(self):
        for doc, data in self.operations: doc.update(data)

def _merge_local(current, data):
    result = dict(current or {})
    for key, value in (data or {}).items():
        if isinstance(value, _Increment):
            result[key] = result.get(key, 0) + value.amount
        elif isinstance(value, _ServerTimestamp):
            result[key] = datetime.now().isoformat()
        else:
            result[key] = value
    return result

def _write_local_db():
    directory = os.path.dirname(os.path.abspath(DATA_FILE))
    os.makedirs(directory, exist_ok=True)
    temp_file = DATA_FILE + ".tmp"
    with open(temp_file, "w", encoding="utf-8") as fp:
        json.dump(local_db_store, fp, ensure_ascii=False, indent=2)
    os.replace(temp_file, DATA_FILE)

def _load_local_db():
    if not os.path.exists(DATA_FILE):
        return {}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as fp:
            value = json.load(fp)
        return value if isinstance(value, dict) else {}
    except Exception as exc:
        print(f"⚠️ Could not read {DATA_FILE}: {exc}")
        return {}

local_db_store = _load_local_db()
DATA_FILE_EXISTED = os.path.isfile(DATA_FILE)

class _LocalDB:
    def collection(self, name):
        return _Collection(local_db_store, name)
    def batch(self):
        return _Batch(local_db_store)
    def export_bytes(self):
        with _local_db_lock:
            return json.dumps(local_db_store, ensure_ascii=False, indent=2).encode("utf-8")
    def import_bytes(self, raw):
        parsed = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        if not isinstance(parsed, dict):
            raise ValueError("Backup must contain a JSON object")
        with _local_db_lock:
            local_db_store.clear()
            local_db_store.update(parsed)
            _write_local_db()

db = _LocalDB()
# These two small helpers preserve the old counter/timestamp call sites while
# using only the local JSON database implementation above.
local_ops = type("LocalOperations", (), {
    "Increment": _Increment,
    "SERVER_TIMESTAMP": _ServerTimestamp()
})

# Config
TOKEN = "8735159035:AAF8j-ke6eku4VOvSyTpefSTsufGhlakqNk".strip()
if not TOKEN:
    raise SystemExit("❌ BOT_TOKEN not set!")

BASE_URL = f"https://api.telegram.org/bot{TOKEN}"
FILE_URL = f"https://api.telegram.org/file/bot{TOKEN}/"

OWNER_ID = 8280457872
BOT_USERNAME = ""

# ==========================================
# Render Health-Check (Flask + Waitress)
# 🌟 The mini-app system has been fully removed - this Flask app is kept only
# to bind $PORT so Render's health check considers the service alive.
# ==========================================
health_check_web = Flask(__name__)

@health_check_web.route("/")
@health_check_web.route("/healthz")
@health_check_web.route("/api/healthz")
def _health_check():
    return Response("OK", mimetype="text/plain")

def start_health_check_server():
    port = int(os.environ.get("PORT", 10000))
    try:
        from waitress import serve
        serve(health_check_web, host="0.0.0.0", port=port)
    except ImportError:
        health_check_web.run(host="0.0.0.0", port=port)

# ==========================================
# Country metadata (ISO -> name, calling code) + Premium Emoji Database
# ==========================================

COUNTRY_META = {
    "AC": ("Ascension Island", "247"), "AD": ("Andorra", "376"), "AE": ("United Arab Emirates", "971"),
    "AF": ("Afghanistan", "93"), "AG": ("Antigua and Barbuda", "1"), "AI": ("Anguilla", "1"),
    "AL": ("Albania", "355"), "AM": ("Armenia", "374"), "AO": ("Angola", "244"),
    "AR": ("Argentina", "54"), "AS": ("American Samoa", "1"), "AT": ("Austria", "43"),
    "AU": ("Australia", "61"), "AW": ("Aruba", "297"), "AX": ("Aland Islands", "358"),
    "AZ": ("Azerbaijan", "994"), "BA": ("Bosnia and Herzegovina", "387"), "BB": ("Barbados", "1"),
    "BD": ("Bangladesh", "880"), "BE": ("Belgium", "32"), "BF": ("Burkina Faso", "226"),
    "BG": ("Bulgaria", "359"), "BH": ("Bahrain", "973"), "BI": ("Burundi", "257"),
    "BJ": ("Benin", "229"), "BL": ("Saint Barthélemy", "590"), "BM": ("Bermuda", "1"),
    "BN": ("Brunei", "673"), "BO": ("Bolivia", "591"), "BQ": ("Caribbean Netherlands", "599"),
    "BR": ("Brazil", "55"), "BS": ("Bahamas", "1"), "BT": ("Bhutan", "975"),
    "BW": ("Botswana", "267"), "BY": ("Belarus", "375"), "BZ": ("Belize", "501"),
    "CA": ("Canada", "1"), "CC": ("Cocos Islands", "61"), "CD": ("DR Congo", "243"),
    "CF": ("Central African Republic", "236"), "CG": ("Congo", "242"), "CH": ("Switzerland", "41"),
    "CI": ("Cote d'Ivoire", "225"), "CK": ("Cook Islands", "682"), "CL": ("Chile", "56"),
    "CM": ("Cameroon", "237"), "CN": ("China", "86"), "CO": ("Colombia", "57"),
    "CR": ("Costa Rica", "506"), "CU": ("Cuba", "53"), "CV": ("Cabo Verde", "238"),
    "CW": ("Curaçao", "599"), "CX": ("Christmas Island", "61"), "CY": ("Cyprus", "357"),
    "CZ": ("Czechia", "420"), "DE": ("Germany", "49"), "DJ": ("Djibouti", "253"),
    "DK": ("Denmark", "45"), "DM": ("Dominica", "1"), "DO": ("Dominican Republic", "1"),
    "DZ": ("Algeria", "213"), "EC": ("Ecuador", "593"), "EE": ("Estonia", "372"),
    "EG": ("Egypt", "20"), "EH": ("Western Sahara", "212"), "ER": ("Eritrea", "291"),
    "ES": ("Spain", "34"), "ET": ("Ethiopia", "251"), "FI": ("Finland", "358"),
    "FJ": ("Fiji", "679"), "FK": ("Falkland Islands", "500"), "FM": ("Micronesia", "691"),
    "FO": ("Faroe Islands", "298"), "FR": ("France", "33"), "GA": ("Gabon", "241"),
    "GB": ("United Kingdom", "44"), "GD": ("Grenada", "1"), "GE": ("Georgia", "995"),
    "GF": ("French Guiana", "594"), "GG": ("Guernsey", "44"), "GH": ("Ghana", "233"),
    "GI": ("Gibraltar", "350"), "GL": ("Greenland", "299"), "GM": ("Gambia", "220"),
    "GN": ("Guinea", "224"), "GP": ("Guadeloupe", "590"), "GQ": ("Equatorial Guinea", "240"),
    "GR": ("Greece", "30"), "GT": ("Guatemala", "502"), "GU": ("Guam", "1"),
    "GW": ("Guinea-Bissau", "245"), "GY": ("Guyana", "592"), "HK": ("Hong Kong", "852"),
    "HN": ("Honduras", "504"), "HR": ("Croatia", "385"), "HT": ("Haiti", "509"),
    "HU": ("Hungary", "36"), "ID": ("Indonesia", "62"), "IE": ("Ireland", "353"),
    "IL": ("Israel", "972"), "IM": ("Isle of Man", "44"), "IN": ("India", "91"),
    "IO": ("British Indian Ocean Territory", "246"), "IQ": ("Iraq", "964"), "IR": ("Iran", "98"),
    "IS": ("Iceland", "354"), "IT": ("Italy", "39"), "JE": ("Jersey", "44"),
    "JM": ("Jamaica", "1"), "JO": ("Jordan", "962"), "JP": ("Japan", "81"),
    "KE": ("Kenya", "254"), "KG": ("Kyrgyzstan", "996"), "KH": ("Cambodia", "855"),
    "KI": ("Kiribati", "686"), "KM": ("Comoros", "269"), "KN": ("Saint Kitts and Nevis", "1"),
    "KP": ("North Korea", "850"), "KR": ("South Korea", "82"), "KW": ("Kuwait", "965"),
    "KY": ("Cayman Islands", "1"), "KZ": ("Kazakhstan", "7"), "LA": ("Laos", "856"),
    "LB": ("Lebanon", "961"), "LC": ("Saint Lucia", "1"), "LI": ("Liechtenstein", "423"),
    "LK": ("Sri Lanka", "94"), "LR": ("Liberia", "231"), "LS": ("Lesotho", "266"),
    "LT": ("Lithuania", "370"), "LU": ("Luxembourg", "352"), "LV": ("Latvia", "371"),
    "LY": ("Libya", "218"), "MA": ("Morocco", "212"), "MC": ("Monaco", "377"),
    "MD": ("Moldova", "373"), "ME": ("Montenegro", "382"), "MF": ("Saint Martin (French part)", "590"),
    "MG": ("Madagascar", "261"), "MH": ("Marshall Islands", "692"), "MK": ("North Macedonia", "389"),
    "ML": ("Mali", "223"), "MM": ("Myanmar", "95"), "MN": ("Mongolia", "976"),
    "MO": ("Macao", "853"), "MP": ("Northern Mariana Islands", "1"), "MQ": ("Martinique", "596"),
    "MR": ("Mauritania", "222"), "MS": ("Montserrat", "1"), "MT": ("Malta", "356"),
    "MU": ("Mauritius", "230"), "MV": ("Maldives", "960"), "MW": ("Malawi", "265"),
    "MX": ("Mexico", "52"), "MY": ("Malaysia", "60"), "MZ": ("Mozambique", "258"),
    "NA": ("Namibia", "264"), "NC": ("New Caledonia", "687"), "NE": ("Niger", "227"),
    "NF": ("Norfolk Island", "672"), "NG": ("Nigeria", "234"), "NI": ("Nicaragua", "505"),
    "NL": ("Netherlands", "31"), "NO": ("Norway", "47"), "NP": ("Nepal", "977"),
    "NR": ("Nauru", "674"), "NU": ("Niue", "683"), "NZ": ("New Zealand", "64"),
    "OM": ("Oman", "968"), "PA": ("Panama", "507"), "PE": ("Peru", "51"),
    "PF": ("French Polynesia", "689"), "PG": ("Papua New Guinea", "675"), "PH": ("Philippines", "63"),
    "PK": ("Pakistan", "92"), "PL": ("Poland", "48"), "PM": ("Saint Pierre and Miquelon", "508"),
    "PR": ("Puerto Rico", "1"), "PS": ("Palestine", "970"), "PT": ("Portugal", "351"),
    "PW": ("Palau", "680"), "PY": ("Paraguay", "595"), "QA": ("Qatar", "974"),
    "RE": ("Réunion", "262"), "RO": ("Romania", "40"), "RS": ("Serbia", "381"),
    "RU": ("Russia", "7"), "RW": ("Rwanda", "250"), "SA": ("Saudi Arabia", "966"),
    "SB": ("Solomon Islands", "677"), "SC": ("Seychelles", "248"), "SD": ("Sudan", "249"),
    "SE": ("Sweden", "46"), "SG": ("Singapore", "65"), "SH": ("Saint Helena", "290"),
    "SI": ("Slovenia", "386"), "SJ": ("Svalbard and Jan Mayen", "47"), "SK": ("Slovakia", "421"),
    "SL": ("Sierra Leone", "232"), "SM": ("San Marino", "378"), "SN": ("Senegal", "221"),
    "SO": ("Somalia", "252"), "SR": ("Suriname", "597"), "SS": ("South Sudan", "211"),
    "ST": ("Sao Tome and Principe", "239"), "SV": ("El Salvador", "503"), "SX": ("Sint Maarten", "1"),
    "SY": ("Syria", "963"), "SZ": ("Eswatini", "268"), "TA": ("Tristan da Cunha", "290"),
    "TC": ("Turks and Caicos Islands", "1"), "TD": ("Chad", "235"), "TG": ("Togo", "228"),
    "TH": ("Thailand", "66"), "TJ": ("Tajikistan", "992"), "TK": ("Tokelau", "690"),
    "TL": ("Timor-Leste", "670"), "TM": ("Turkmenistan", "993"), "TN": ("Tunisia", "216"),
    "TO": ("Tonga", "676"), "TR": ("Turkey", "90"), "TT": ("Trinidad and Tobago", "1"),
    "TV": ("Tuvalu", "688"), "TW": ("Taiwan", "886"), "TZ": ("Tanzania", "255"),
    "UA": ("Ukraine", "380"), "UG": ("Uganda", "256"), "US": ("United States", "1"),
    "UY": ("Uruguay", "598"), "UZ": ("Uzbekistan", "998"), "VA": ("Vatican City", "39"),
    "VC": ("Saint Vincent and the Grenadines", "1"), "VE": ("Venezuela", "58"), "VG": ("British Virgin Islands", "1"),
    "VI": ("US Virgin Islands", "1"), "VN": ("Vietnam", "84"), "VU": ("Vanuatu", "678"),
    "WF": ("Wallis and Futuna", "681"), "WS": ("Samoa", "685"), "XK": ("Kosovo", "383"),
    "YE": ("Yemen", "967"), "YT": ("Mayotte", "262"), "ZA": ("South Africa", "27"),
    "ZM": ("Zambia", "260"), "ZW": ("Zimbabwe", "263"),
}

# ==========================================
PEM = {
    "ok": '✅',
    "no": '❌',
    "warn": '⚠️',
    "admin": '📊',
    "user": '👤',
    "file": '📁',
    "rocket": '🚀',
    "graph": '📊',
    "money": '💸',
    "gift": '🎁',
    "msg": '💬',
    "gear": '⚙️',
    "link": '🔗',
    "trash": '🗑',
    "upload": '📤',
    "world": '🌐',
    "lock": '🔐',
    "phone": '📱',
    "num": '🔢',
    "pin": '📍',
    "star": '✨',
    "hi": '👋',
    # 🌟 Added from NewsEmoji pack
    "gold": '🥇',
    "silver": '🥈',
    "bronze": '🥉',
    "crown": '👑',
    "celebrate": '🎉',
    "info": 'ℹ️',
    "search": '🔍',
    "refresh": '🔄',
    "new": '🆕',
    "top": '🔝',
    "fire": '🔥',
    "check": '✔️',
    "idea": '💡',
    "calendar": '🗓'
}

GLOBAL_BODY_EMOJIS = {
    "➖": "5870818207383686839", "🚫": "5334807341109908955", "😒": "5334763399299506604",
    "🖥": "5334880948259427772", "🌐": "5334590977837403844", "🌟": "5337102391244263212",
    "🕓": "5336983442125001376", "⌛": "5337172996211648018", "💬": "5337302974806922068",
    "🔐": "5337255927735163754", "🍏": "5337132498965010628", "❔": "5336850036145823599",
    "⚠️": "5336944168944047463", "🔥": "5337267511261960341", "💸": "5348469219761626211",
    "🥚": "5348390922507817684", "👨‍⚖": "5334763399299506604", "🐁": "5348494358205207761",
    "🧻": "5348486915026884464", "⚗": "5346311574221000149", "🛴": "5348075478634766440",
    "📊": "5353032893096567467", "🔢": "5352862640592949843", "👤": "5352861489541714456",
    "📁": "5352721946054268944", "🚀": "5352597830089347330", "💎": "5352838545826420397",
    "📍": "5352922460897452503", "👋": "5353027129250453493", "✅": "5352694861990501856",
    "1️⃣": "5352651766288652742", "2️⃣": "5355186458418257716", "3️⃣": "5352867219028091093",
    "4️⃣": "5352566657216714037", "5️⃣": "5353086880835474989", "6️⃣": "5354859211975071385",
    "7️⃣": "5352859127309707652", "8️⃣": "5352957533600389988", "9️⃣": "5353060913463204207",
    "🔤": "5352727417842606016", "📣": "5352980533150259581", "📤": "5353001161878182134",
    "✨": "5352552689983067014", "🔹": "5352638632278660622", "🎙": "5355102594886833928",
    "💴": "5352985330628730418", "📅": "5352585194295564660", "📴": "5352974971167611327",
    "✏️️": "5395444784611480792", "📱": "5337132498965010628", "🔗": "5420517437885943844",
    "❌": "5420130255174145507", "⚙️": "5420155432272438703", "🫂": "5420145051336485498",
    "➕": "5420323438508155202", "🗑": "5422557736330106570", "🎁": "5420396762189831222",
    "➤": "5420618897898381296", "🏢": "5420156334215565595", "💳": "5190899075968441286",
    "📝": "5192739271886282680", "🛡": "5190447043545438788", "🤝": "5192805934073685937",
    "💰": "5190576863226933563", "👀": "5190645917711114179", "🕹": "5193100774988617665",
    "🟢": "5192812028632274956", "🧪": "5190781475468915802", "🎨": "5190751148704833975",
    "📂": "5257969839313526622", "🌍": "5780471598922337683", "📌": "5318986077455795572",
    "📢": "5789428375261023681", "🆔": "5352862640592949843", "📈": "5352877703043258544",
    "🔔": "5352980533150259581", "🏦": "5348469219761626211", "🧾": "5192739271886282680",
    "👨‍⚖️️": "5334763399299506604", "🔍": "5463352748751753567",
    "🔑": "5197288647275071607",
    # 🌟 Added from NewsEmoji pack - only new emoji chars, existing ones kept as-is
    "🙂": "5461117441612462242", "⚡️": "5456140674028019486", "☄️": "5224607267797606837",
    "🛍": "5229064374403998351", "⛔️": "5260293700088511294", "❗️": "5274099962655816924",
    "‼️": "5440660757194744323", "⁉️": "5314504236132747481", "❓": "5436113877181941026",
    "💭": "5467538555158943525", "🔼": "5449683594425410231", "🔽": "5447183459602669338",
    "🕯": "5451882707875276247", "📉": "5246762912428603768", "✔️": "5206607081334906820",
    "🆒": "5222079954421818267", "🥸": "5391112412445288650", "🤡": "5269531045165816230",
    "🫦": "5395444514028529554", "💵": "5409048419211682843", "💱": "5402186569006210455",
    "▶️": "5264919878082509254", "🔴": "5411225014148014586", "➡️": "5416117059207572332",
    "💥": "5276032951342088188", "🎤": "5224736245665511429", "🤫": "5431609822288033666",
    "👎": "5449875686837726134", "🗣️️": "5460795800101594035", "©": "5323442290708985472",
    "ℹ️": "5334544901428229844", "👍": "5337080053119336309", "⏸": "5359543311897998264",
    "💯": "5341498088408234504", "🔄": "5375338737028841420", "🔝": "5415655814079723871",
    "🆕": "5382357040008021292", "🔜": "5440621591387980068", "⭐️": "5438496463044752972",
    "👑": "5217822164362739968", "🔖": "5222444124698853913", "✉️": "5253742260054409879",
    "🔒": "5296369303661067030", "😮": "5303479226882603449", "📎": "5305265301917549162",
    "🎮": "5361741454685256344", "🔈": "5388632425314140043", "⬇️": "5406745015365943482",
    "☀️": "5402477260982731644", "🌧": "5399913388845322366", "🌛": "5449569374065152798",
    "❄️": "5449449325434266744", "🌈": "5409109841538994759", "💧": "5393512611968995988",
    "🗓": "5413879192267805083", "💡": "5422439311196834318", "🥇": "5440539497383087970",
    "🥈": "5447203607294265305", "🥉": "5453902265922376865", "🎵": "5463107823946717464",
    "🆓": "5406756500108501710", "🚨": "5395695537687123235", "🏠": "5416041192905265756",
    "🚩": "5460755126761312667", "🎉": "5461151367559141950"
}

DEFAULT_CUSTOM_MESSAGES = {
    "start": {"text": "╔═══════════╗\n       📊 NUMBER BOT\n╚═══════════╝\n🚀 Welcome to Number & OTP Service\n━━━━━━━━━━━━\n✅ Choose an option below\nto continue using the bot.\n━━━━━━━━━━━━\n💎 Premium OTP Service", "buttons": []},
    "get_number": {"text": f"{PEM['pin']} Select a service:", "buttons": []},
    "select_country": {"text": f"📌 Select a country for {{service}}:", "buttons": []}, 
    "refer": {"text": f"➖➖➖➖➖➖➖\n« {PEM['gift']} REFER & EARN »\n➖➖➖➖➖➖➖\n{PEM['link']} YOUR LINK:\n`{{ref_link}}`\n➖➖➖➖➖➖➖\n{PEM['user']} TOTAL REFERS: **{{total_ref}}**\n➖➖➖➖➖➖➖\n{PEM['money']} PER REFER: **{{ref_reward}} TK**\n➖➖➖➖➖➖➖", "buttons": []},
    "withdrawal": {"text": "➖➖➖➖➖➖➖\n《 😒 WITHDRAWAL 》\n➖➖➖➖➖➖➖\n👋 Total Otp: {total_otp}\n➖➖➖➖➖➖➖\n🫂 Total Reffer :{total_ref}\n➖➖➖➖➖➖➖\n📅 BALANCE: {bal}৳\n➖➖➖➖➖➖➖\n🔐 MINIMUM: {min_w} ৳\n➖➖➖➖➖➖➖\nSELECT METHOD:", "buttons": []},
    "support": {"text": f"{PEM['msg']} Contact us for any help:", "buttons": []}
}

print(f"✅ Local database ready: {DATA_FILE}")
if not DATA_FILE_EXISTED:
    with _local_db_lock:
        _write_local_db()
    print("🆕 New data file created because no backup was found.")
else:
    print("♻️ Existing data file found; it will be used as-is.")

bot_settings = {
    "admins": [OWNER_ID],
    "panels": [], 
    "fw_groups": [], 
    "otp_link": "https://t.me/your_otp_group",
    "withdraw_on": True,
    "min_withdraw": 30.0,
    "otp_reward": 0.1,
    "refer_reward": 0.2,
    "weekly_reset_at": 0,
    "cooldown": 10,
    "num_req": 3,
    "num_share": 1, 
    "support_link": "https://t.me/your_support",
    "w_methods": ["bKash", "Nagad"],
    "w_group": "", 
    
    "fj_on": False,
    "fj_channels": [], 
    "stex_keys": [], 
    "voltx_keys": [],
    "zebrasms_keys": [],
    "yesms_keys": [],
    "ksiiprn_keys": [],
    "stex_services": {},
    "voltx_services": {},
    "zebrasms_services": {},
    "yesms_services": {},
    "ksiiprn_services": {},
    "stex_service_rates": {},
    "voltx_service_rates": {},
    "zebrasms_service_rates": {},
    "yesms_service_rates": {},
    "ksiiprn_service_rates": {},
    "premium_flags": {
        "1": {"char": "🇺🇸", "iso": "US", "name": "United States", "id": "5913463998522592692"},
        "880": {"char": "🇧🇩", "iso": "BD", "name": "Bangladesh", "id": "5911365056594973179"},
        "91": {"char": "🇮🇳", "iso": "IN", "name": "India", "id": "5913754823643107921"},
        "92": {"char": "🇵🇰", "iso": "PK", "name": "Pakistan", "id": "5913705895375672082"},
        "44": {"char": "🇬🇧", "iso": "GB", "name": "United Kingdom", "id": "5913443365499703513"}
    },
    "premium_apps": {
        # 🌟 Expanded from the Premium_App.txt pack the admin provided, so OTP
        # cards show a real premium icon for every common service instead of
        # the generic 📱 fallback. Keys match SERVICE_SMS_KEYWORDS where one
        # exists (so incoming SMS text auto-detects them); others are matched
        # directly against a manually configured service name.
        "FACEBOOK": {"char": "📘", "id": "5334807341109908955", "name": "Facebook"},
        "WHATSAPP": {"char": "💬", "id": "5334759662677957452", "name": "WhatsApp"},
        "TELEGRAM": {"char": "✈️", "id": "5337010556253543833", "name": "Telegram"},
        "IMO": {"char": "💭", "id": "5337155807752524558", "name": "Imo"},
        "INSTAGRAM": {"char": "📸", "id": "5334868205091459431", "name": "Instagram"},
        "APPLE": {"char": "🍎", "id": "5334637951894722661", "name": "Apple"},
        "GOOGLE": {"char": "🔍", "id": "5335010201005231986", "name": "Google"},
        "MICROSOFT": {"char": "🪟", "id": "5334880948259427772", "name": "Microsoft"},
        "TEAMS": {"char": "🧑‍🤝‍🧑", "id": "5334590977837403844", "name": "Teams"},
        "TIKTOK": {"char": "🎵", "id": "5339213256001102461", "name": "Tiktok"},
        "BKASH": {"char": "🏦", "id": "5348469219761626211", "name": "Bkash"},
        "ROCKET": {"char": "🚀", "id": "5346042941196507141", "name": "Rocket"},
        "BYBIT": {"char": "📈", "id": "5348372939479751825", "name": "Bybit"},
        "BINANCE": {"char": "💱", "id": "5348212415077064131", "name": "Binance"},
        "MELBET": {"char": "🌟", "id": "5337102391244263212", "name": "Melbet"},
        "SNAPCHAT": {"char": "👻", "id": "5359441366554255082", "name": "Snapchat"},
        "UBER": {"char": "🚗", "id": "5298715455316303708", "name": "Uber"},
        "PAYPAL": {"char": "💵", "id": "5776103539872896061", "name": "PayPal"},
        "DISCORD": {"char": "🎬", "id": "5116246243646898866", "name": "Discord"},
        "AMAZON": {"char": "🌟", "id": "4995019580536524226", "name": "Amazon"},
        "VIBER": {"char": "💜", "id": "5463060437572528782", "name": "Viber"},
        "LINKEDIN": {"char": "💼", "id": "6224222994265279792", "name": "Linkedin"},
        "LINE": {"char": "🔒", "id": "5399818044866327279", "name": "Line"},
        "WECHAT": {"char": "🌟", "id": "5782757599560602950", "name": "Wechat"},
        "TWITTER": {"char": "🐦", "id": "5215726959056662534", "name": "Twitter"},
        "REDDIT": {"char": "👽", "id": "4992421103847604984", "name": "Reddit"},
        "PINTEREST": {"char": "📌", "id": "5346103513120258857", "name": "Pinterest"},
        "TWITCH": {"char": "🎮", "id": "5233333563306301418", "name": "Twitch"},
        "ZOOM": {"char": "📹", "id": "5881799193219043268", "name": "Zoom"},
        "SIGNAL": {"char": "💬", "id": "5293998404404272267", "name": "Signal"},
        "SLACK": {"char": "💻", "id": "4994972469040251302", "name": "Slack"},
        "SKYPE": {"char": "☎️", "id": "4992613535562334989", "name": "Skype"},
        "NETFLIX": {"char": "🎥", "id": "6255738712664050133", "name": "Netflix"},
        "SPOTIFY": {"char": "🎵", "id": "5411392711146095115", "name": "Spotify"},
        "AMAZONPRIME": {"char": "📺", "id": "6111801057061374810", "name": "Amazon Prime"},
        "HOICHOI": {"char": "🍿", "id": "6104822598493801746", "name": "Hoichoi"},
        "DARAZ": {"char": "📦", "id": "5336879280578138635", "name": "Daraz"},
        "FOODPANDA": {"char": "🐼", "id": "5336879280578138635", "name": "Foodpanda"},
        "PATHAO": {"char": "🛵", "id": "5336879280578138635", "name": "Pathao"},
        "ALIEXPRESS": {"char": "🛒", "id": "5336879280578138635", "name": "AliExpress"},
        "SHOPEE": {"char": "🛍️", "id": "5336879280578138635", "name": "Shopee"},
        "PAYONEER": {"char": "💳", "id": "5336879280578138635", "name": "Payoneer"},
        "WISE": {"char": "🦉", "id": "5336879280578138635", "name": "Wise"},
        "CHATGPT": {"char": "🤖", "id": "5296516998996445955", "name": "ChatGPT"},
        "NOTION": {"char": "📓", "id": "5336879280578138635", "name": "Notion"},
        "GITHUB": {"char": "🐙", "id": "5417836094098007862", "name": "GitHub"},
        "CANVA": {"char": "🖌️", "id": "5111661409008092227", "name": "Canva"},
        "FIGMA": {"char": "🎨", "id": "5336879280578138635", "name": "Figma"},
        "UPWORK": {"char": "💼", "id": "5336879280578138635", "name": "Upwork"},
        "FIVERR": {"char": "🟢", "id": "5336879280578138635", "name": "Fiverr"},
        "YAHOO": {"char": "🌐", "id": "5336879280578138635", "name": "Yahoo"},
        "DROPBOX": {"char": "☁️", "id": "5336879280578138635", "name": "Dropbox"},
        "COURSERA": {"char": "📚", "id": "5336879280578138635", "name": "Coursera"},
        "DUOLINGO": {"char": "🗣️", "id": "5336879280578138635", "name": "Duolingo"}
    },
    "status_services": [],
    "custom_messages": DEFAULT_CUSTOM_MESSAGES.copy()
}

FS_KEYS = [
    "admins", "panels", "fw_groups", "otp_link", "withdraw_on", 
    "min_withdraw", "otp_reward", "refer_reward", "cooldown", 
    "num_req", "num_share", "support_link", "w_methods", "w_group", "stex_keys", "voltx_keys", "stex_services", "voltx_services",
    "fj_on", "fj_channels", "zebrasms_keys", "zebrasms_services", "yesms_keys", "yesms_services", "ksiiprn_keys", "ksiiprn_services",
    "stex_service_rates", "voltx_service_rates", "zebrasms_service_rates", "yesms_service_rates", "ksiiprn_service_rates",
    # 🌟 FIX: these were missing before, so every admin edit to premium flags/app icons and
    # custom menu text (start/get_number/refer/withdrawal/support) was silently lost on
    # every restart/redeploy - they are written to the local data file.
    "premium_flags", "premium_apps", "custom_messages", "status_services", "weekly_reset_at"
]

number_batches = {}
used_numbers_list = []
stex_assigned_numbers = {} 
voltx_assigned_numbers = {}
zebrasms_assigned_numbers = {}
yesms_assigned_numbers = {}
ksiiprn_assigned_numbers = {}
STEX_BASE_URL = "https://api.2oo9.cloud/MXS47FLFX0U/tness/@public/api"
VOLTX_BASE_URL = "https://api.2oo9.cloud/MXS47FLFX0U/tnevs/@public/api"
ZEBRASMS_BASE_URL = "https://zebrasms.com/api/v1"
YESMS_BASE_URL = "https://yesms.online/api"
KSIIPRN_BASE_URL = "https://ksiiprn.site/api"
total_uploaded_stats = 0
total_assigned_stats = 0
processed_otps = set() 
user_banned_cache = {}

# Active HTTP sessions for Auto Captcha Panels
panel_sessions = {}

# 🌟 sAjaxSource (AJAX/DataTable) এবং Fallback HTML Parser Helper Function
def fetch_cpt_panel_cdrs(p, session, check_url):
    res = session.get(check_url, timeout=15)
    html_text = res.text
    
    # সেশন শেষ হয়েছে বা লগইন পেজে রিডাইরেক্ট করেছে কি না তা চেক করা
    if "login" in html_text.lower() or "signin" in html_text.lower() or any(x in html_text for x in ["Sign in to your account", "Please sign in", "Welcome back!"]):
        raise Exception("Session expired")
        
    soup = BeautifulSoup(html_text, 'html.parser')
    s_ajax_source = ""
    for script in soup.find_all("script"):
        script_text = script.string or ""
        match = re.search(r'sAjaxSource":\s*"([^"]+)"', script_text)
        if match:
            s_ajax_source = match.group(1)
            break
            
    results = []
    
    n_col_name = p.get("num_col_name", "number").lower()
    m_col_name = p.get("msg_col_name", "message").lower()
    n_idx = int(p.get("num_col_idx", 1)) - 1 if p.get("num_col_idx") else 1
    m_idx = int(p.get("msg_col_idx", 2)) - 1 if p.get("msg_col_idx") else 2

    # ৫.১ যদি sAjaxSource AJAX লিংক পাওয়া যায়
    if s_ajax_source:
        baseUrl = p.get("login_url", "").split("/client")[0].split("/login")[0].strip()
        if not baseUrl.startswith("http"):
            baseUrl = "http://" + baseUrl
            
        full_ajax_url = ""
        if s_ajax_source.startswith("http"):
            full_ajax_url = s_ajax_source
        elif s_ajax_source.startswith("/"):
            full_ajax_url = f"{baseUrl}{s_ajax_source}"
        else:
            last_slash_idx = check_url.rfind("/")
            current_dir = check_url[:last_slash_idx]
            full_ajax_url = f"{current_dir}/{s_ajax_source}"

        if "iDisplayLength" not in full_ajax_url:
            # 250 এর জায়গায় 10000 করে দেওয়া হলো যাতে সব মেসেজ ফেচ করতে পারে
            query_params = "sEcho=1&iColumns=7&iDisplayStart=0&iDisplayLength=10000&sSearch=&iSortingCols=1&iSortCol_0=0&sSortDir_0=desc"
            divider = "&" if "?" in full_ajax_url else "?"
            full_ajax_url += f"{divider}{query_params}"

        ajax_headers = {
            "Referer": check_url,
            "X-Requested-With": "XMLHttpRequest"
        }
        
        ajax_res = session.get(full_ajax_url, headers=ajax_headers, timeout=15)
        data_dict = ajax_res.json()
        rows = data_dict.get("aaData", [])
        for row_val in rows:
            if not isinstance(row_val, list):
                continue
                
            if len(row_val) < max(n_idx, m_idx) + 1:
                continue
                
            num_val = row_val[n_idx] if (0 <= n_idx < len(row_val)) else row_val[2]
            msg_val = row_val[m_idx] if (0 <= m_idx < len(row_val)) else row_val[4]
            
            clean_num = re.sub(r'\D', '', str(num_val))
            if clean_num and 5 <= len(clean_num) <= 18:
                otp = extract_otp_code(msg_val)
                if otp and len(msg_val) > 4:
                    results.append({"number": clean_num, "message": msg_val, "otp": otp})
                    
    else:
        # ৫.২ ডাইরেক্ট HTML টেবিল থেকে রিড করার ব্যাকআপ লজিক
        tables = soup.find_all('table')
        for table in tables:
            rows = table.find_all('tr')
            if not rows: continue
            
            final_n_idx = n_idx
            final_m_idx = m_idx
            
            header_cells = rows[0].find_all(['th', 'td'])
            for i, cell in enumerate(header_cells):
                c_text = cell.get_text(strip=True).lower()
                if n_col_name in c_text: final_n_idx = i
                if m_col_name in c_text: final_m_idx = i

            for row in rows:
                cols = row.find_all(['td', 'th'])
                if all(c.name == 'th' for c in cols): continue
                
                if len(cols) > max(final_n_idx, final_m_idx):
                    num_text = cols[final_n_idx].get_text(separator=" ", strip=True)
                    msg_text = cols[final_m_idx].get_text(separator=" ", strip=True)
                    
                    clean_num = re.sub(r'\D', '', num_text)
                    if clean_num and 5 <= len(clean_num) <= 18:
                        otp = extract_otp_code(msg_text)
                        if otp and len(msg_text) > 4:
                            results.append({"number": clean_num, "message": msg_text, "otp": otp})
                            
    return results, html_text

# Track active number sessions to expire them automatically
user_active_sessions = {}
_otp_claim_lock = threading.Lock()

assigned_number_rates = {}

def claim_processed_otp(number, otp, legacy_ids=()):
    """Atomically suppress the same number+OTP, even when two listeners see it."""
    clean_number = re.sub(r"\D", "", str(number))
    clean_otp = str(otp).strip()
    if not clean_number or not clean_otp:
        return False
    unique_id = f"OTP_{clean_number}_{clean_otp}"
    with _otp_claim_lock:
        # Accept the identifiers written by older versions too.  Without this
        # migration check, the first restart after the fix could replay an OTP
        # that was already stored as PANEL_/VOLTX_/YESMS_/STEX_/KSIIPRN_/POLL_ data.
        old_suffix = f"_{clean_number}_{clean_otp}"
        if (
            unique_id in processed_otps
            or any(str(item) == f"{clean_number}_{clean_otp}" or str(item).endswith(old_suffix)
                   for item in processed_otps)
            or any(item in processed_otps for item in legacy_ids)
        ):
            return False
        processed_otps.add(unique_id)
        # Do not clear the whole set: that caused old provider messages to be
        # delivered again whenever the set crossed the previous limit.
        if len(processed_otps) > 10000:
            processed_otps.pop()
        return True

def resolve_manual_service_rate(provider_key, service, rng):
    # 🌟 Manually added Voltx/StexSMS/Zebrasms/Ksiiprn service+country এর জন্য কাস্টম OTP রেট বের করা
    # provider_key: "stex" | "voltx" | "zebrasms" | "ksiiprn"
    if not service:
        return bot_settings.get("otp_reward", 0.0)
    services_dict = bot_settings.get(f"{provider_key}_services", {}).get(service, {})
    for cnt, ranges in services_dict.items():
        if rng in ranges:
            rate = bot_settings.get(f"{provider_key}_service_rates", {}).get(service, {}).get(cnt)
            if rate is not None:
                return float(rate)
            break
    return bot_settings.get("otp_reward", 0.0)

# All persistent state lives in the portable local database file.
DATA_KEYS = [
    "number_batches", "used_numbers_list", "total_uploaded_stats", "total_assigned_stats",
    "stex_assigned_numbers", "voltx_assigned_numbers", "zebrasms_assigned_numbers",
    "yesms_assigned_numbers", "ksiiprn_assigned_numbers", "processed_otps", "assigned_number_rates",
]

def load_db():
    global bot_settings, number_batches, used_numbers_list, total_uploaded_stats, total_assigned_stats, stex_assigned_numbers, voltx_assigned_numbers, zebrasms_assigned_numbers, yesms_assigned_numbers, ksiiprn_assigned_numbers, processed_otps, assigned_number_rates

    try:
        doc = None
        for attempt in range(3):
            try:
                doc = db.collection('settings').document('bot_config').get()
                break
            except Exception as e:
                if attempt == 2:
                    raise
                time.sleep(2 ** attempt)
        if doc.exists:
            fs_data = doc.to_dict()
            for k, val in fs_data.items():
                if k == "custom_messages":
                    for m_key, m_val in val.items():
                        bot_settings["custom_messages"][m_key] = m_val
                else:
                    bot_settings[k] = val
            for m_key, m_val in DEFAULT_CUSTOM_MESSAGES.items():
                if m_key not in bot_settings["custom_messages"]:
                    bot_settings["custom_messages"][m_key] = m_val
        else:
            db.collection('settings').document('bot_config').set({k: bot_settings[k] for k in FS_KEYS if k in bot_settings})
        print("✅ Config loaded from local file!")
    except Exception as e:
        print(f"❌ Error loading config from local file: {e}")

    try:
        doc = None
        for attempt in range(3):
            try:
                doc = db.collection('settings').document('bot_data').get()
                break
            except Exception as e:
                if attempt == 2:
                    raise
                time.sleep(2 ** attempt)
        data = doc.to_dict() if doc.exists else {}
        number_batches = data.get("number_batches", {})
        used_numbers_list = data.get("used_numbers_list", [])
        total_uploaded_stats = data.get("total_uploaded_stats", 0)
        total_assigned_stats = data.get("total_assigned_stats", 0)
        stex_assigned_numbers = data.get("stex_assigned_numbers", {})
        voltx_assigned_numbers = data.get("voltx_assigned_numbers", {})
        zebrasms_assigned_numbers = data.get("zebrasms_assigned_numbers", {})
        yesms_assigned_numbers = data.get("yesms_assigned_numbers", {})
        ksiiprn_assigned_numbers = data.get("ksiiprn_assigned_numbers", {})
        processed_otps = set(data.get("processed_otps", []))
        assigned_number_rates = data.get("assigned_number_rates", {})
        print("✅ Data loaded from local file!")
    except Exception as e:
        print(f"❌ Error loading data from local file: {e}")

def _sync_fs():
    try:
        db.collection('settings').document('bot_config').set({k: bot_settings[k] for k in FS_KEYS if k in bot_settings})
        data = {
            "number_batches": number_batches, "used_numbers_list": used_numbers_list,
            "total_uploaded_stats": total_uploaded_stats, "total_assigned_stats": total_assigned_stats,
            "stex_assigned_numbers": stex_assigned_numbers, "voltx_assigned_numbers": voltx_assigned_numbers,
            "zebrasms_assigned_numbers": zebrasms_assigned_numbers, "yesms_assigned_numbers": yesms_assigned_numbers,
            "ksiiprn_assigned_numbers": ksiiprn_assigned_numbers,
            "processed_otps": list(processed_otps), "assigned_number_rates": assigned_number_rates,
        }
        db.collection('settings').document('bot_data').set(data)
    except Exception as e:
        print(f"❌ Error saving to local file: {e}")

def save_db():
    # Saved in a background thread so it never slows down the bot's replies.
    threading.Thread(target=_sync_fs).start()

load_db()

def retry_local_load():
    # Keep the in-memory state aligned if the data file is replaced manually.
    while True:
        time.sleep(60)
        try:
            load_db()
        except Exception as e:
            print(f"Deferred local database reload failed: {e}")

user_states = {}
temp_data = {}
user_cooldowns = {}
pending_withdrawals = {}

# ==========================================
# Telegram API & Helpers
# ==========================================
tg_session = requests.Session() # 🌟 Keep-Alive Connection (Makes bot 10x faster)

def api_call(method, payload=None):
    url = f"{BASE_URL}/{method}"
    # getUpdates uses Telegram long-polling (timeout=50), so the HTTP
    # read timeout must be longer than the Telegram polling timeout.
    request_timeout = (10, 65) if method.startswith("getUpdates") else (10, 20)
    for attempt in range(3):
        try:
            res = tg_session.post(url, json=payload, timeout=request_timeout)
            res.raise_for_status()
            result = res.json()
            if result.get("ok"):
                return result
            description = result.get("description", "unknown Telegram API error")
            parameters = result.get("parameters", {})
            retry_after = parameters.get("retry_after")
            if retry_after and attempt < 2:
                time.sleep(min(int(retry_after), 10))
                continue
            print(f"Telegram API rejected {method}: {description}")
            return result
        except (requests.RequestException, ValueError) as e:
            if attempt == 2:
                print(f"Telegram API error ({method}): {e}")
                return {}
            time.sleep(2 ** attempt)

def send_message(chat_id, text, reply_markup=None, parse_mode="HTML"):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode, "disable_web_page_preview": True}
    if reply_markup: payload["reply_markup"] = reply_markup
    return api_call("sendMessage", payload)

def send_photo(chat_id, photo_url_or_file_id, caption="", reply_markup=None, parse_mode="HTML"):
    payload = {"chat_id": chat_id, "photo": photo_url_or_file_id, "caption": caption, "parse_mode": parse_mode}
    if reply_markup: payload["reply_markup"] = reply_markup
    return api_call("sendPhoto", payload)

def edit_message(chat_id, message_id, text, reply_markup=None, parse_mode="HTML"):
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": parse_mode, "disable_web_page_preview": True}
    if reply_markup: payload["reply_markup"] = reply_markup
    return api_call("editMessageText", payload)

def delete_message(chat_id, message_id):
    return api_call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})

def answer_callback(callback_id, text="", show_alert=False):
    api_call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text, "show_alert": show_alert})

def generate_emoji_txt(mode):
    """Builds a .txt export in the exact format the flag/app TXT importer expects
    (see wait_for_flag_txt / wait_for_app_txt handling above)."""
    lines = []
    if mode == "flags":
        for code, data in bot_settings.get("premium_flags", {}).items():
            name = data.get("name", "")
            iso = data.get("iso", "")
            char = data.get("char", "")
            eid = data.get("id", "")
            payload = json.dumps({"emoji": char, "id": eid}, ensure_ascii=False)
            lines.append(f"{name} ({code})({iso}) {payload}")
    else:
        for name_key, data in bot_settings.get("premium_apps", {}).items():
            name = data.get("name", name_key)
            char = data.get("char", "")
            eid = data.get("id", "")
            payload = json.dumps({"emoji": char, "id": eid}, ensure_ascii=False)
            lines.append(f"{name} {payload}")
    if not lines:
        return None
    return "\n".join(lines)

def send_document(chat_id, filename, text_content):
    url = f"{BASE_URL}/sendDocument"
    files = {'document': (filename, text_content)}
    data = {'chat_id': chat_id}
    try:
        requests.post(url, data=data, files=files, timeout=(10, 30))
    except requests.RequestException as e:
        print(f"Telegram document upload error: {e}")

# In-memory cache of known user IDs, loaded once from the local database.
all_known_users = set()

def sync_users_list():
    global all_known_users
    try:
        for doc in db.collection('users').select([]).stream():
            all_known_users.add(doc.id)
    except: pass

threading.Thread(target=sync_users_list, daemon=True).start()

def register_user_local(uid):
    all_known_users.add(str(uid))

def broadcast_copymessage(from_chat_id, msg_id):
    success = 0
    failed = 0
    users = list(all_known_users)
    
    # 🌟 Dedicated Connection Pool for Broadcast (Fixes Port Exhaustion & Network Lag)
    b_session = requests.Session()
    url = f"{BASE_URL}/copyMessage"
    
    for user_id in users:
        payload = {"chat_id": user_id, "from_chat_id": from_chat_id, "message_id": msg_id}
        try:
            res = b_session.post(url, json=payload, timeout=5).json()
            if res.get("ok"): success += 1
            else: failed += 1
        except:
            failed += 1
        time.sleep(0.035) # Safe speed (28 msgs/sec) to prevent Telegram Ban
        
    send_message(from_chat_id, render_body_text(f"📢 **Broadcast Completed!**\n✅ Success: {success}\n❌ Failed: {failed}\n👥 Total Sent: {len(users)}"))

def render_body_text(text):
    if not text: return str(text)
    parts = re.split(r'()', str(text))
    for i in range(len(parts)):
        if not parts[i].startswith('{normal_emj}')
    return "".join(parts)

def extract_premium_html(msg):
    text = msg.get("text", msg.get("caption", ""))
    entities = msg.get("entities", msg.get("caption_entities", []))
    if not entities: return text
    try:
        b_text = text.encode('utf-16-le')
        c_entities = [e for e in entities if e.get("type") == "custom_emoji"]
        c_entities.sort(key=lambda x: x["offset"], reverse=True)
        for ent in c_entities:
            offset = ent["offset"] * 2
            length = ent["length"] * 2
            eid = ent["custom_emoji_id"]
            emoji_char = b_text[offset:offset+length].decode('utf-16-le')
            html_tag = f'{emoji_char}'
            replacement = html_tag.encode('utf-16-le')
            b_text = b_text[:offset] + replacement + b_text[offset+length:]
        return b_text.decode('utf-16-le')
    except Exception as e:
        return text 

def country_code_from_label(country):
    """Return the ISO code from BD, Bangladesh, or Bangladesh (BD)."""
    value = str(country or "").strip().upper()
    match = re.search(r"\(([A-Z]{2})\)\s*$", value)
    if match:
        return match.group(1)
    if value in COUNTRY_META:
        return value
    for iso, (name, _) in COUNTRY_META.items():
        if value == name.upper():
            return iso
    for flag_data in bot_settings.get("premium_flags", {}).values():
        if value == str(flag_data.get("name", "")).strip().upper():
            return str(flag_data.get("iso", "")).upper()
    return ""

def country_display_name(country):
    """Store/display countries consistently as Full Name (ISO).

    Also supports a trailing number to create a 2nd/3rd entry for the same
    country, e.g. "MG 2" -> "Madagascar 2 (MG)", "MG 3" -> "Madagascar 3 (MG)".
    This is used identically by every panel (StexSMS/Voltx/Zebrasms/Yesms/Ksiiprn).
    """
    raw = str(country or "").strip()
    suffix = ""
    m = re.match(r"^(.*\S)\s+(\d+)$", raw)
    base = raw
    if m and country_code_from_label(m.group(1)):
        base = m.group(1)
        suffix = f" {m.group(2)}"
    iso = country_code_from_label(base)
    # Use the admin's premium flag database for every ISO code, not only the
    # small built-in metadata list.
    for flag_data in bot_settings.get("premium_flags", {}).values():
        if flag_data.get("iso", "").upper() == iso:
            return f"{flag_data.get('name', base)}{suffix} ({iso})"
    if not iso:
        return raw
    return f"{COUNTRY_META.get(iso, (base.title(), ''))[0]}{suffix} ({iso})"

def country_calling_code(country):
    iso = country_code_from_label(country)
    for calling_code, flag_data in bot_settings.get("premium_flags", {}).items():
        if flag_data.get("iso", "").upper() == iso and str(calling_code).isdigit():
            return str(calling_code)
    return COUNTRY_META.get(iso, ("", ""))[1]

def normalize_provider_number(number, country=""):
    """Convert a national Yesms number to an international number."""
    clean = re.sub(r"[^\d]", "", str(number or ""))
    if not clean:
        return ""
    calling = country_calling_code(country)
    if calling and not clean.startswith(calling):
        clean = clean.lstrip("0")
        clean = calling + clean
    return clean

def provider_country(provider, service, range_id):
    """Find the country configured for a provider range."""
    services_key = f"{provider}_services"
    for country, ranges in bot_settings.get(services_key, {}).get(service or "", {}).items():
        if str(range_id) in [str(r) for r in ranges]:
            return country
    return ""

def unicode_flag(iso):
    iso = str(iso or "").upper()
    if len(iso) != 2 or not iso.isalpha():
        return "🌍"
    return "".join(chr(127397 + ord(c)) for c in iso)

def get_country_flag_info(country):
    """Resolve a country flag by exact ISO/name matching only."""
    raw = str(country or "").strip()
    iso = country_code_from_label(raw)
    for flag_data in bot_settings.get("premium_flags", {}).values():
        flag_iso = flag_data.get("iso", "").upper()
        flag_name = str(flag_data.get("name", "")).strip().upper()
        if (iso and flag_iso == iso) or (not iso and raw.upper() == flag_name):
            return flag_data.get("char", unicode_flag(flag_iso)), flag_data.get("id")
    return (unicode_flag(iso) if iso else "🌍"), None

def get_flag_info_from_num(num):
    clean = re.sub(r"[^\d]", "", str(num or ""))
    sorted_codes = sorted(bot_settings.get("premium_flags", {}).keys(), key=len, reverse=True)
    for code in sorted_codes:
        if clean.startswith(code):
            data = bot_settings["premium_flags"][code]
            return data["char"], data.get("iso", "XX"), data.get("id")
    # Fallback for countries that do not have a custom Telegram emoji entry.
    for iso, (_, calling) in COUNTRY_META.items():
        if calling and clean.startswith(calling):
            return unicode_flag(iso), iso, None
    return "🌍", "XX", None

def get_flag_and_code(num):
    char, iso = get_flag_info_from_num(num)[:2]
    return char, iso

def get_flag_info_html(num_or_iso, return_full_name=False):
    iso_hint = country_code_from_label(num_or_iso)
    if iso_hint:
        num_or_iso = iso_hint
    if len(str(num_or_iso)) == 2:
        for code, data in bot_settings.get("premium_flags", {}).items():
            if data.get("iso", "").upper() == str(num_or_iso).upper():
                eid = data.get("id")
                char = data.get("char")
                name = data.get("name", num_or_iso)
                if return_full_name: return name
                if eid: return f'{char}'
                return char
        if str(num_or_iso).upper() in COUNTRY_META:
            iso = str(num_or_iso).upper()
            if return_full_name:
                return COUNTRY_META[iso][0]
            return unicode_flag(iso)
        if return_full_name: return num_or_iso
        return "🌍"
        
    char, detected_iso, eid = get_flag_info_from_num(num_or_iso)
    if return_full_name:
        for code, data in bot_settings.get("premium_flags", {}).items():
            clean = re.sub(r"[^\d]", "", str(num_or_iso))
            if clean.startswith(code): return data.get("name", num_or_iso)
        if detected_iso in COUNTRY_META:
            return COUNTRY_META[detected_iso][0]
        return num_or_iso
        
    if eid:
        return f'{char}'
    return char

def mask_number(num):
    clean = num.replace("+", "").replace(" ", "")
    if len(clean) > 6: return f"{clean[:3]}TGZ{clean[-3:]}"
    elif len(clean) > 2: return f"{clean[:1]}TGZ{clean[-1:]}"
    return clean

LANG_MAP = {
    "#EN": "English", "#BN": "Bengali", "#AR": "Arabic", "#HI": "Hindi", 
    "#PA": "Punjabi", "#GU": "Gujarati", "#OR": "Odia", "#TA": "Tamil", 
    "#TE": "Telugu", "#KN": "Kannada", "#ML": "Malayalam", "#SI": "Sinhala", 
    "#TH": "Thai", "#LO": "Lao", "#BO": "Tibetan", "#MY": "Burmese", 
    "#AM": "Amharic", "#KM": "Khmer", "#KA": "Georgian", "#HY": "Armenian", 
    "#HE": "Hebrew", "#EL": "Greek", "#RU": "Russian", "#ZH": "Chinese", 
    "#JA": "Japanese", "#KO": "Korean", "#ID": "Indonesian", "#MS": "Malay", 
    "#VN": "Vietnamese", "#TL": "Filipino", "#ES": "Spanish", "#PT": "Portuguese", 
    "#FR": "French", "#DE": "German", "#IT": "Italian", "#PL": "Polish", 
    "#TR": "Turkish", "#NL": "Dutch", "#SV": "Swedish", "#DA": "Danish", 
    "#NO": "Norwegian", "#FI": "Finnish", "#CS": "Czech", "#SK": "Slovak", 
    "#HU": "Hungarian", "#RO": "Romanian", "#HR": "Croatian", "#BG": "Bulgarian", 
    "#UK": "Ukrainian", "#SW": "Swahili", "#AF": "Afrikaans"
}

# ==========================================
# 🌟 ADVANCED SERVICE & LANGUAGE DETECTION
# ==========================================

SERVICE_SMS_KEYWORDS = {
    # 🟢 Social Media & Chat (Added Arabic Keywords)
    "whatsapp": ["whatsapp", "whatsa", "whatsap", "whats", "whatsapp business", "whatsapp me", "whatsapp code", "whatsap", "واتساب", "واتساپ", "واٹس ایپ", "व्हाट्सएप", "वाट्सएप", "वॉट्सऐप", "व्हाट्सप्प", "হোয়াটসঅ্যাপ", "হোটসঅ্যাপ", "ватсап", "уотсап", "вотсап", "ватс апп", "వాట్సాప్", "വാട്‌സ്ആപ്പ്", "வாட்ஸ்அப்", "ವಾಟ್ಸಾಪ್", "વોટ્સએપ", "ਵਟਸਐਪ", "ହ୍ଵାଟସ୍ ଆପ୍", "වට්ස්ඇප්", "วอตส์แอปป์", "วอทส์แอพ", "ဝက်စ်အက်ပ်", "វ៉តសាប់", "ວອດແອັບ", "ワッツアップ", "왓츠앱", "whatsapp的", "whatsapp验证码", "וואטסאפ", "γουάτσαπ", "ዋትስአፕ", "ვოთსአფი", "վոթսափ"],
    "facebook": ["facebook", "fb", "meta", "fbook", "fb code", "facebook code", "فيسبوك", "فيس بوك"],
    "instagram": ["instagram", "insta", "ig", "ig code", "instagram code", "انستغرام", "انستقرام"],
    "telegram": ["telegram", "tg", "tele", "telegram code", "tg code", "t.me", "تيليجرام", "تليجرام"],
    "tiktok": ["tiktok", "tik tok", "tikvideo", "tiktok code", "tik code", "تيك توك"],
    "snapchat": ["snapchat", "snap", "snap code", "سناب شات"],
    "twitter": ["twitter", "x.com", "x code", "twitter code", "تويتر"],
    "discord": ["discord", "discord code", "ديسكورد"],
    "viber": ["viber", "viber code", "فايبر"],
    "line": ["line", "line code", "line verification", "لاين"],
    "wechat": ["wechat", "we chat", "wechat code", "وي تشات"],
    "signal": ["signal", "signal code", "سيجنال"],
    "linkedin": ["linkedin", "linked in", "لينكد إن"],
    "imo": ["imo", "imo code", "imo verification", "ايمو"],
    "kakaotalk": ["kakao", "kakaotalk", "كاكاو"],
    "qq": ["qq", "tencent qq"],
    "vk": ["vk", "vkontakte"],

    # 🔵 Tech & Mail
    "google": ["google", "gmail", "youtube", "g-", "google voice", "جوجل", "غوغل"],
    "microsoft": ["microsoft", "ms", "outlook", "live.com", "hotmail"],
    "apple": ["apple", "icloud", "itunes", "apple id"],
    "yahoo": ["yahoo", "yahoo code", "ymail"],
    "protonmail": ["proton", "protonmail"],
    
    # 💰 Crypto & Trading
    "binance": ["binance", "bnb", "binances"],
    "coinbase": ["coinbase"],
    "okx": ["okx", "okex"],
    "kucoin": ["kucoin"],
    "bybit": ["bybit"],
    "huobi": ["huobi", "htx"],
    "mexc": ["mexc"],
    "trustwallet": ["trust wallet", "trustwallet"],

    # 💳 Finance & Wallets
    "bkash": ["bkash", "b-kash", "bkash code"],
    "nagad": ["nagad", "nagad code"],
    "rocket": ["rocket", "dutch bangla"],
    "upay": ["upay", "upay code"],
    "paypal": ["paypal", "pay pal"],
    "paytm": ["paytm"],
    "cashapp": ["cash app", "cashapp"],
    "wise": ["wise", "transferwise"],

    # 🛒 E-commerce & Delivery
    "amazon": ["amazon", "amzn", "amazon code"],
    "ebay": ["ebay"],
    "aliexpress": ["aliexpress", "ali express"],
    "alibaba": ["alibaba"],
    "daraz": ["daraz", "daraz code"],
    "foodpanda": ["foodpanda", "food panda"],
    "uber": ["uber", "uber code", "uber verification", "uber eats"],
    "pathao": ["pathao", "pathao ride"],

    # 🎮 Gaming & Entertainment
    "netflix": ["netflix", "netflix code"],
    "spotify": ["spotify", "spotify code"],
    "steam": ["steam", "steam guard"],
    "epicgames": ["epic games", "epicgames"],
    "roblox": ["roblox", "roblox code"],
    "riotgames": ["riot", "riot games", "valorant", "league of legends"],
    "garena": ["garena", "free fire", "freefire"],
    "playstation": ["playstation", "psn"],

    # 🎲 Betting & Casino
    "1xbet": ["1xbet", "1x bet"],
    "melbet": ["melbet", "melbet code"],
    "linebet": ["linebet"],
    "bet365": ["bet365"],
    "megapari": ["megapari"],

    # ❤️ Dating
    "tinder": ["tinder", "tinder code"],
    "bumble": ["bumble"],
    "badoo": ["badoo"]
}

def detect_service(text):
    text_lower = str(text).lower()
    for service_key, keywords in SERVICE_SMS_KEYWORDS.items():
        for kw in keywords:
            if kw in text_lower:
                return service_key.upper()
    return None

def get_service_info_html(service_text, msg_text=""):
    s = str(service_text).upper().strip()
    m = str(msg_text).lower().strip()
    apps = bot_settings.get("premium_apps", {})
    
    detected_service = s
    if m:
        for service_key, keywords in SERVICE_SMS_KEYWORDS.items():
            for kw in keywords:
                if kw in m:
                    detected_service = service_key.upper()
                    break
            if detected_service != s: break

    clean_s = re.sub(r'[^\w\s]', '', detected_service).strip()
    
    for app_name, data in apps.items():
        if app_name == detected_service or app_name == clean_s or app_name in detected_service or detected_service in app_name:
            full_name = data.get("name", app_name.title())
            char = data.get("char", "📱")
            eid = data.get("id")
            if eid: return full_name, f'{char}'
            return full_name, char
            
    if len(detected_service) > 20:
        return "Message", "💬"
        
    return detected_service.title(), "📱"

def detect_language(text):
    if not text: return "#EN"
    text_str = str(text)

    # ১. Unicode Block দিয়ে নিখুঁত বর্ণমালা শনাক্তকরণ (100% Accurate for scripts)
    if any('\u0600' <= c <= '\u06ff' for c in text_str): return "#AR" # Arabic / Persian / Urdu
    if any('\u0980' <= c <= '\u09ff' for c in text_str): return "#BN" # Bengali
    if any('\u0900' <= c <= '\u097f' for c in text_str): return "#HI" # Hindi / Marathi / Nepali
    if any('\u0a00' <= c <= '\u0a7f' for c in text_str): return "#PA" # Punjabi (Gurmukhi)
    if any('\u0a80' <= c <= '\u0aff' for c in text_str): return "#GU" # Gujarati
    if any('\u0b00' <= c <= '\u0b7f' for c in text_str): return "#OR" # Odia
    if any('\u0b80' <= c <= '\u0bff' for c in text_str): return "#TA" # Tamil
    if any('\u0c00' <= c <= '\u0c7f' for c in text_str): return "#TE" # Telugu
    if any('\u0c80' <= c <= '\u0cff' for c in text_str): return "#KN" # Kannada
    if any('\u0d00' <= c <= '\u0d7f' for c in text_str): return "#ML" # Malayalam
    if any('\u0d80' <= c <= '\u0dff' for c in text_str): return "#SI" # Sinhala
    if any('\u0e00' <= c <= '\u0e7f' for c in text_str): return "#TH" # Thai
    if any('\u0e80' <= c <= '\u0eff' for c in text_str): return "#LO" # Lao
    if any('\u0f00' <= c <= '\u0fff' for c in text_str): return "#BO" # Tibetan
    if any('\u1000' <= c <= '\u109f' for c in text_str): return "#MY" # Burmese (Myanmar)
    if any('\u1200' <= c <= '\u137f' for c in text_str): return "#AM" # Amharic (Ethiopic)
    if any('\u1780' <= c <= '\u17ff' for c in text_str): return "#KM" # Khmer
    if any('\u10a0' <= c <= '\u10ff' for c in text_str): return "#KA" # Georgian
    if any('\u0530' <= c <= '\u058f' for c in text_str): return "#HY" # Armenian
    if any('\u0590' <= c <= '\u05ff' for c in text_str): return "#HE" # Hebrew
    if any('\u0370' <= c <= '\u03ff' for c in text_str): return "#EL" # Greek
    if any('\u0400' <= c <= '\u04ff' for c in text_str): return "#RU" # Russian / Ukrainian (Cyrillic)
    if any('\u4e00' <= c <= '\u9fff' for c in text_str): return "#ZH" # Chinese
    if any('\u3040' <= c <= '\u309f' or '\u30a0' <= c <= '\u30ff' for c in text_str): return "#JA" # Japanese
    if any('\uac00' <= c <= '\ud7af' for c in text_str): return "#KO" # Korean

    # ২. OTP Keyword দিয়ে ভাষা শনাক্তকরণ (Latin script languages)
    text_lower = text_str.lower()
    
    # Asian / Pacific
    if any(w in text_lower for w in ["kode verifikasi", "jangan bagikan", "rahasia"]): return "#ID" # Indonesian
    if any(w in text_lower for w in ["kod pengesahan", "jangan kongsi"]): return "#MS" # Malay
    if any(w in text_lower for w in ["mã của bạn", "không chia sẻ", "mã xác minh"]): return "#VN" # Vietnamese
    if any(w in text_lower for w in ["ang iyong code", "huwag ibahagi"]): return "#TL" # Tagalog / Filipino
    
    # European / Americas
    if any(w in text_lower for w in ["código", "tu código", "verificación", "no compartas"]): return "#ES" # Spanish
    if any(w in text_lower for w in ["seu código", "código de verificação", "não compartilhe"]): return "#PT" # Portuguese
    if any(w in text_lower for w in ["code secret", "ne partagez pas", "votre code"]): return "#FR" # French
    if any(w in text_lower for w in ["dein code", "bestätigungscode", "nicht teilen"]): return "#DE" # German
    if any(w in text_lower for w in ["il tuo codice", "codice di verifica", "non condividere"]): return "#IT" # Italian
    if any(w in text_lower for w in ["twój kod", "nie udostępniaj", "kod weryfikacyjny"]): return "#PL" # Polish
    if any(w in text_lower for w in ["doğrulama kodu", "paylaşmayın", "onay kodu"]): return "#TR" # Turkish
    if any(w in text_lower for w in ["jouw code", "verificatiecode", "niet delen"]): return "#NL" # Dutch
    if any(w in text_lower for w in ["din kod", "verifieringskod", "dela inte"]): return "#SV" # Swedish
    if any(w in text_lower for w in ["bekræftelseskode", "del ikke"]): return "#DA" # Danish
    if any(w in text_lower for w in ["bekreftelseskode", "ikke del"]): return "#NO" # Norwegian
    if any(w in text_lower for w in ["vahvistuskoodi", "älä jaa"]): return "#FI" # Finnish
    if any(w in text_lower for w in ["váš kód", "ověřovací kód", "nesdílejte"]): return "#CS" # Czech
    if any(w in text_lower for w in ["overovací kód", "nezdieľajte"]): return "#SK" # Slovak
    if any(w in text_lower for w in ["ellenőrző kód", "ne oszd meg"]): return "#HU" # Hungarian
    if any(w in text_lower for w in ["codul tău", "codul de verificare", "nu partaja"]): return "#RO" # Romanian
    if any(w in text_lower for w in ["kontrolni kod", "kod za potvrdu", "ne delite"]): return "#HR" # Croatian/Serbian
    if any(w in text_lower for w in ["код за потвърждение", "не споделяйте"]): return "#BG" # Bulgarian
    if any(w in text_lower for w in ["ваш код", "код підтвердження"]): return "#UK" # Ukrainian
    
    # African
    if any(w in text_lower for w in ["msimbo wako", "usishiriki"]): return "#SW" # Swahili
    if any(w in text_lower for w in ["verifikasiekode", "moenie deel nie"]): return "#AF" # Afrikaans
    
    # ৩. উপরের কোনোটি না মিললে ডিফল্ট
    return "#EN"

def parse_chat_id(text):
    text = text.strip()
    if text.startswith("-100") or (text.startswith("-") and text[1:].isdigit()):
        return text
    if "t.me/" in text:
        parts = text.split("/")
        username = parts[-1]
        if username: return "@" + username if not username.startswith("@") else username
    if text.startswith("@"):
        return text
    return "@" + text

def is_admin(user_id):
    return user_id in bot_settings["admins"] or user_id == OWNER_ID

def check_force_join(user_id):
    if not bot_settings["fj_on"] or not bot_settings["fj_channels"]: return True
    if is_admin(user_id): return True
    for ch in bot_settings["fj_channels"]:
        res = api_call("getChatMember", {"chat_id": ch, "user_id": user_id})
        if res.get("ok") and res["result"]["status"] not in ["left", "kicked"]: continue
        else: return False
    return True

def send_force_join_msg(chat_id):
    kb = []
    for ch in bot_settings["fj_channels"]:
        url = f"https://t.me/{ch.replace('@', '')}" if ch.startswith("@") else ch
        kb.append([{"text": f"Join Channel", "icon_custom_emoji_id": "5789428375261023681", "url": url, "style": "primary"}])
    kb.append([{"text": "Check Joined", "icon_custom_emoji_id": "5352694861990501856", "callback_data": "check_fj", "style": "success"}])
    send_message(chat_id, render_body_text(f"{PEM['warn']} **Please join our channels to use the bot!**"), reply_markup={"inline_keyboard": kb})

def is_user_banned(user_id):
    if is_admin(user_id): return False
    if user_id in user_banned_cache and time.time() - user_banned_cache[user_id]['time'] < 60:
        return user_banned_cache[user_id]['banned']
    banned = False
    if db:
        try:
            doc = db.collection('users').document(str(user_id)).get()
            banned = doc.exists and doc.to_dict().get("banned", False)
        except: pass
    user_banned_cache[user_id] = {'banned': banned, 'time': time.time()}
    return banned

# ==========================================
# Captcha Auto Login & Parsing Core
# ==========================================
def extract_otp_code(text):
    clean_text = re.sub(r'[\u200B-\u200D\uFEFF]', '', str(text))

    # 1. Multi-part OTPs (e.g. 123-456 or 809-761)
    multi_part = re.search(r'(\d{3}[-\s]+\d{3})|(\d{2}[-\s]+\d{2}[-\s]+\d{2})', clean_text)
    if multi_part:
        # হাইফেন (-) থাকলে সেটা রেখে দিবে, কিন্তু স্পেস থাকলে মুছে একসাথে করে দিবে
        return multi_part.group(0).replace(" ", "")

    # 2. Keyword-based extraction
    otp_keywords = ['code', 'is', 'otp', 'pin', 'verification', 'auth', 'কোড', 'رمز', 'your code']
    keywords_pattern = '|'.join(otp_keywords)
    keyword_match = re.search(rf'(?:{keywords_pattern})\s*(?:is|:|-|=)?\s*([a-z0-9]{{4,10}})', clean_text, re.I)
    if keyword_match and keyword_match.group(1).isdigit():
        return keyword_match.group(1)
        
    keyword_match_rev = re.search(rf'([a-z0-9]{{4,10}})\s*(?:is your|is the|কোড)', clean_text, re.I)
    if keyword_match_rev and keyword_match_rev.group(1).isdigit():
        return keyword_match_rev.group(1)

    # 3. Google OTP
    g_match = re.search(r'G-(\d{6})', clean_text, re.IGNORECASE)
    if g_match: return g_match.group(1)

    # 4. Digit sequences fallback
    digit_matches = re.findall(r'\b\d{4,6}\b', search_text)
                        # HTML টেবিল থেকে টেক্সট বের করা
                        num_text = cols[final_n_idx].get_text(separator=" ", strip=True)
                        msg_text = cols[final_m_idx].get_text(separator=" ", strip=True)
                        
                        clean_num = re.sub(r'\D', '', num_text)
                        
                        # নাম্বারটা আসলেই ৫-১৮ ডিজিটের কিনা তা নিশ্চিত করা (যাতে উল্টাপাল্টা টেক্সট না আসে)
                        if clean_num and 5 <= len(clean_num) <= 18:
                            otp = extract_otp_code(msg_text)
                            if otp and len(msg_text) > 4:
                                results.append({"number": clean_num, "message": msg_text, "otp": otp})
        except Exception as e:
            pass
    else:
        try:
            data = json.loads(response_text)
            temp_results = []
            
            def process_item(item):
                pot_nums_list = []
                pot_msg = None
                values = []
                
                if isinstance(item, dict):
                    # ১. প্রথমে পরিচিত JSON Key (যেমন: num, phone, sms) দিয়ে খোঁজার চেষ্টা
                    lower_keys = {str(k).lower(): v for k, v in item.items()}
                    for k in ["number", "num", "phone", "msisdn", "sender"]:
                        if k in lower_keys:
                            clean_val = re.sub(r'\D', '', str(lower_keys[k]))
                            if 5 <= len(clean_val) <= 18:
                                if clean_val not in pot_nums_list: pot_nums_list.append(clean_val)
                    for k in ["message", "msg", "sms", "content", "text"]:
                        if k in lower_keys:
                            val = str(lower_keys[k])
                            if len(val) > 4:
                                pot_msg = val
                                break
                    values = list(item.values())
                elif isinstance(item, list):
                    values = item

                # ২. যদি Key দিয়ে না পাওয়া যায়, তবে Smart Blind Scan (সব ভ্যালু চেক করবে)
                for v in values:
                    if isinstance(v, (dict, list)) or v is None: continue
                    v_str = str(v).strip()
                    
                    # Number Detection: 7 থেকে 18 ডিজিট
                    clean_v = re.sub(r'\D', '', v_str)
                    if 7 <= len(clean_v) <= 18 and not re.search(r'[a-zA-Z]', v_str):
                        # Date/Time/IP এড়ানোর লজিক
                        if not re.search(r'\d{4}[-/]\d{2}[-/]\d{2}', v_str) and not re.search(r'\d{2}:\d{2}:\d{2}', v_str) and "." not in v_str:
                            if clean_v not in pot_nums_list:
                                pot_nums_list.append(clean_v)
                    
                    # Message Detection: 5 অক্ষরের বেশি এবং শুধু সংখ্যা নয়
                    if len(v_str) > 4 and not v_str.isdigit():
                        if extract_otp_code(v_str):
                            if pot_msg is None or len(v_str) > len(pot_msg):
                                pot_msg = v_str
                                
                # 🌟 ৩. Multiple Numbers Logic (User Priority > Second Number > First Number)
                pot_num = None
                if pot_nums_list:
                    matched_user_num = None
                    for n in pot_nums_list:
                        # চেক করবে ইউজারের অ্যাসাইন করা নাম্বারের তালিকায় এই নাম্বারটি আছে কি না
                        if n in stex_assigned_numbers or any(n in str(key) for key in stex_assigned_numbers.keys()):
                            matched_user_num = n
                            break
                    
                    if matched_user_num:
                        pot_num = matched_user_num
                    elif len(pot_nums_list) >= 2:
                        pot_num = pot_nums_list[1] # ইউজারের কাছে না থাকলে সরাসরি দ্বিতীয় নাম্বারটি নেবে
                    else:
                        pot_num = pot_nums_list[0]
                            
                if pot_num and pot_msg:
                    otp = extract_otp_code(pot_msg)
                    if otp:
                        temp_results.append({"number": pot_num, "message": pot_msg, "otp": otp})
                        
            def traverse_json(node):
                if isinstance(node, list):
                    if len(node) > 0 and not isinstance(node[0], (dict, list)):
                        # It's a flat list representing one record
                        process_item(node)
                    for child in node:
                        if isinstance(child, (dict, list)):
                            traverse_json(child)
                elif isinstance(node, dict):
                    process_item(node)
                    for val in node.values():
                        if isinstance(val, (dict, list)):
                            traverse_json(val)

            traverse_json(data)
            
            # Remove duplicates
            seen = set()
            for r in temp_results:
                uid = f"{r['number']}_{r['otp']}"
                if uid not in seen:
                    seen.add(uid)
                    results.append(r)
        except: pass
        
    return results

# 🌟 Advanced Automated Background Captcha Solver 🌟
def attempt_auto_login(p, idx):
    login_url = p.get("login_url", "").strip()
    if not login_url.startswith("http"):
        login_url = "http://" + login_url
        
    if not login_url.lower().endswith('/login') and not login_url.lower().endswith('.php'):
        login_url = f"{login_url.rstrip('/')}/login"
        
    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'
    })
    
    try:
        res = session.get(login_url, timeout=15)
        soup = BeautifulSoup(res.text, 'html.parser')
        all_text = res.text
        
        # 1. SOLVE CAPTCHA (Exact bot 3.py logic)
        captcha_match = re.search(r'(\d+\s*[\+\-\*]\s*\d+)\s*[=\?:]', all_text)
        if not captcha_match:
            captcha_match = re.search(r'what is\s*(\d+\s*[\+\-\*]\s*\d+)', all_text, re.I)
        if not captcha_match:
            elements = soup.find_all(["label", "div", "span", "p", "strong"])
            for el in elements:
                txt = el.get_text(separator=" ", strip=True)
                if any(op in txt for op in ["+", "-", "*"]):
                    m = re.search(r'(\d+\s*[\+\-\*]\s*\d+)', txt)
                    if m:
                        captcha_match = m
                        break
                        
        captcha_text = captcha_match.group(1) if captcha_match else "0 + 0"
        answer = "0"
        m2 = re.search(r'(\d+)\s*([\+\-\*])\s*(\d+)', captcha_text)
        if m2:
            a, op, b = int(m2.group(1)), m2.group(2), int(m2.group(3))
            if op == '+': answer = str(a + b)
            elif op == '-': answer = str(a - b)
            elif op == '*': answer = str(a * b)

        # 2. FIND FORM
        form = soup.find("form")
        if not form:
            p["login_status"] = "❌ No login form found"
            return False
            
        action = form.get("action")
        from urllib.parse import urljoin
        post_url = urljoin(login_url, action) if action else login_url

        form_data = {}
        for hidden in form.find_all("input", type="hidden"):
            name = hidden.get("name")
            if name: form_data[name] = hidden.get("value") or ""
        
        user_input = form.find("input", {"name": re.compile(r"user|email|id", re.I)}) or \
                     form.find("input", {"type": "text", "placeholder": re.compile(r"user|email", re.I)}) or \
                     form.find("input", {"type": "text"})
                     
        pass_input = form.find("input", {"name": re.compile(r"pass", re.I)}) or \
                     form.find("input", {"type": "password"})
                     
        captcha_input = form.find("input", {"placeholder": re.compile(r"answer|ans|code|verification|value|captcha", re.I)}) or \
                        form.find("input", {"name": re.compile(r"ans|captcha|ver|code", re.I)})
        
        user_field = user_input.get("name") if user_input else "username"
        pass_field = pass_input.get("name") if pass_field else "password"
        captcha_field = captcha_input.get("name") if captcha_input else "answer"

        form_data[user_field] = p.get("username", "")
        form_data[pass_field] = p.get("password", "")
        if captcha_field:
            form_data[captcha_field] = answer

        # 3. SUBMIT
        login_req = session.post(post_url, data=form_data, allow_redirects=True, timeout=15)
        
        # 4. VERIFY (Exact bot 3.py check logic)
        msg_link = p.get("msg_link", "").strip()
        if not msg_link.startswith("http") and msg_link != "":
            msg_link = "http://" + msg_link
            
        check_url = msg_link if msg_link else f"{login_url.split('/login')[0]}/client/SMSCDRStats"
        
        check_res = session.get(check_url, timeout=10)
        
        if 'logout' in login_req.text.lower() or 'logout' in check_res.text.lower() or 'sms reports' in check_res.text.lower() or 'dashboard' in check_res.text.lower() or 'cdrs' in check_res.text.lower():
            panel_sessions[idx] = session
            p["login_status"] = "✅ Active & Fetching"
            return True
        else:
            # এখানে ফেইল হলে অংক কী পেয়েছিল তা দেখা যাবে
            p["login_status"] = f"❌ Login Failed (Math: {captcha_text} = {answer})"
            return False
            
    except Exception as e:
        p["login_status"] = f"❌ Error: {str(e)[:50]}"
        return False

# ==========================================
# OTP Distribution Engine
# ==========================================
def dispatch_otp(data, provider_name="PANEL"):
    try:
        raw_num = str(data.get("number", ""))
        clean_num = re.sub(r'\D', '', raw_num)
        otp = str(data.get("otp", "")).strip()
        msg_text = str(data.get("message", "")).strip()
        
        if not clean_num or not otp: return
        
        # 🌟 Deduplication System: Prevents re-sending OTP
        legacy_id = f"{provider_name}_{clean_num}_{otp}"
        if not claim_processed_otp(clean_num, otp, legacy_ids=(legacy_id,)):
            return

        # 🌟 Target Detection Logic
        target_uid = None
        service_name = "SMS Service"
        
        # Check Manual Batch Numbers
        for b_id, batch in list(number_batches.items()):
            for item in batch.get("numbers", []):
                item_clean = re.sub(r'\D', '', str(item.get("number", "")))
                if item_clean and (item_clean == clean_num or item_clean in clean_num or clean_num in item_clean):
                    target_uid = batch.get("user_id")
                    service_name = batch.get("service", "SMS Service")
                    break
            if target_uid: break
            
        # Check StexSMS Assigned Numbers
        if not target_uid:
            for num_key, info in list(stex_assigned_numbers.items()):
                num_clean = re.sub(r'\D', '', str(num_key))
                if num_clean and (num_clean == clean_num or num_clean in clean_num or clean_num in num_clean):
                    target_uid = info.get("user_id")
                    service_name = info.get("service", "StexSMS")
                    break

        # Check Voltx Assigned Numbers
        if not target_uid:
            for num_key, info in list(voltx_assigned_numbers.items()):
                num_clean = re.sub(r'\D', '', str(num_key))
                if num_clean and (num_clean == clean_num or num_clean in clean_num or clean_num in num_clean):
                    target_uid = info.get("user_id")
                    service_name = info.get("service", "Voltx")
                    break

        # Check Zebrasms Assigned Numbers
        if not target_uid:
            for num_key, info in list(zebrasms_assigned_numbers.items()):
                num_clean = re.sub(r'\D', '', str(num_key))
                if num_clean and (num_clean == clean_num or num_clean in clean_num or clean_num in num_clean):
                    target_uid = info.get("user_id")
                    service_name = info.get("service", "ZebraSMS")
                    break

        # Check Yesms Assigned Numbers
        if not target_uid:
            for num_key, info in list(yesms_assigned_numbers.items()):
                num_clean = re.sub(r'\D', '', str(num_key))
                if num_clean and (num_clean == clean_num or num_clean in clean_num or clean_num in num_clean):
                    target_uid = info.get("user_id")
                    service_name = info.get("service", "Yesms")
                    break

        # Check Ksiiprn Assigned Numbers
        if not target_uid:
            for num_key, info in list(ksiiprn_assigned_numbers.items()):
                num_clean = re.sub(r'\D', '', str(num_key))
                if num_clean and (num_clean == clean_num or num_clean in clean_num or clean_num in num_clean):
                    target_uid = info.get("user_id")
                    service_name = info.get("service", "Ksiiprn")
                    break

        # Smart Detect Service & Language
        app_title, app_emoji = get_service_info_html(service_name, msg_text)
        flag_emoji = get_flag_info_html(clean_num)
        country_full_name = get_flag_info_html(clean_num, return_full_name=True)
        lang_code = detect_language(msg_text)
        lang_name = LANG_MAP.get(lang_code, "English")

        # Formatting Output
        masked_num = mask_number(clean_num)
        
        card_text = (
            f"**★ NEW OTP RECEIVED ★**\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"**{app_emoji} Service:** `{app_title}`\n"
            f"**{flag_emoji} Country:** `{country_full_name}`\n"
            f"**{PEM['phone']} Number:** `+{clean_num}`\n"
            f"**{PEM['key']} OTP Code:** `{otp}`\n"
            f"**{PEM['msg']} Full SMS:** `{html.escape(msg_text)}`\n"
            f"**🌐 Language:** `{lang_name} ({lang_code})`\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"*🤖 Powered by Premium OTP Service*"
        )
        card_text = render_body_text(card_text)

        # 1. Forward to Forward Groups
        for grp in bot_settings.get("fw_groups", []):
            send_message(grp, card_text)

        # 2. Reward User & Send Direct Notification
        if target_uid:
            # 🌟 Custom Rate Logic per assigned number
            reward = assigned_number_rates.get(clean_num, bot_settings.get("otp_reward", 0.1))
            
            try:
                # Update DB Balance
                user_ref = db.collection('users').document(str(target_uid))
                user_ref.set({
                    "balance": local_ops.Increment(reward),
                    "total_otp": local_ops.Increment(1),
                    "weekly_otp": local_ops.Increment(1)
                }, merge=True)
                
                # Send User Direct Message
                user_msg = (
                    f"🎉 **New OTP Received!**\n\n"
                    f"**{app_emoji} Service:** {app_title}\n"
                    f"**{PEM['phone']} Number:** `+{clean_num}`\n"
                    f"**{PEM['key']} Code:** `{otp}`\n"
                    f"**💰 Earnings:** +{reward:.2f} TK\n\n"
                    f"*SMS: {html.escape(msg_text)}*"
                )
                send_message(target_uid, render_body_text(user_msg))
            except Exception as e:
                print(f"Failed to reward user {target_uid}: {e}")

        save_db()
    except Exception as e:
        print(f"Error in dispatch_otp: {e}")

# ==========================================
# Background Workers & API Checkers
# ==========================================
def auto_login_panel_worker():
    while True:
        try:
            for idx, p in enumerate(bot_settings.get("panels", [])):
                if p.get("type") == "Auto Captcha Panel":
                    if idx not in panel_sessions or p.get("login_status", "").startswith("❌"):
                        attempt_auto_login(p, idx)
        except Exception as e:
            print(f"Auto-login worker error: {e}")
        time.sleep(300) # Check every 5 minutes

def panel_poll_worker():
    while True:
        try:
            for idx, p in enumerate(bot_settings.get("panels", [])):
                p_type = p.get("type", "API Panel")
                
                if p_type == "Auto Captcha Panel":
                    session = panel_sessions.get(idx)
                    if not session:
                        continue
                    msg_link = p.get("msg_link", "").strip()
                    if not msg_link.startswith("http") and msg_link != "":
                        msg_link = "http://" + msg_link
                    login_url = p.get("login_url", "").strip()
                    if not login_url.startswith("http"): login_url = "http://" + login_url
                    
                    check_url = msg_link if msg_link else f"{login_url.split('/login')[0]}/client/SMSCDRStats"
                    
                    try:
                        results, _ = fetch_cpt_panel_cdrs(p, session, check_url)
                        for r in results:
                            dispatch_otp(r, provider_name=f"PANEL_{idx}")
                    except Exception as e:
                        if "Session expired" in str(e):
                            p["login_status"] = "❌ Session Expired"
                            panel_sessions.pop(idx, None)
                else:
                    # Standard API Panel
                    api_url = p.get("api_url", "").strip()
                    if api_url:
                        try:
                            res = requests.get(api_url, timeout=10)
                            results = parse_panel_response(res.text, p)
                            for r in results:
                                dispatch_otp(r, provider_name=f"PANEL_{idx}")
                        except: pass
        except Exception as e:
            print(f"Panel poll error: {e}")
        time.sleep(10)

def provider_poll_worker():
    while True:
        try:
            # 1. Poll StexSMS API
            for key in bot_settings.get("stex_keys", []):
                try:
                    url = f"{STEX_BASE_URL}/get_sms?api_key={key}"
                    res = requests.get(url, timeout=10).json()
                    if res.get("status") == "success" or isinstance(res, list):
                        data_list = res.get("data", res) if isinstance(res, dict) else res
                        if isinstance(data_list, list):
                            for item in data_list:
                                dispatch_otp({
                                    "number": item.get("number") or item.get("phone"),
                                    "otp": item.get("otp") or extract_otp_code(item.get("sms", "")),
                                    "message": item.get("sms") or item.get("text", "")
                                }, provider_name="STEX")
                except: pass

            # 2. Poll Voltx API
            for key in bot_settings.get("voltx_keys", []):
                try:
                    url = f"{VOLTX_BASE_URL}/get_sms?api_key={key}"
                    res = requests.get(url, timeout=10).json()
                    if res.get("status") == "success" or isinstance(res, list):
                        data_list = res.get("data", res) if isinstance(res, dict) else res
                        if isinstance(data_list, list):
                            for item in data_list:
                                dispatch_otp({
                                    "number": item.get("number") or item.get("phone"),
                                    "otp": item.get("otp") or extract_otp_code(item.get("sms", "")),
                                    "message": item.get("sms") or item.get("text", "")
                                }, provider_name="VOLTX")
                except: pass

            # 3. Poll ZebraSMS API
            for key in bot_settings.get("zebrasms_keys", []):
                try:
                    url = f"{ZEBRASMS_BASE_URL}/get_sms?api_key={key}"
                    res = requests.get(url, timeout=10).json()
                    if res.get("status") == "success" or isinstance(res, list):
                        data_list = res.get("data", res) if isinstance(res, dict) else res
                        if isinstance(data_list, list):
                            for item in data_list:
                                dispatch_otp({
                                    "number": item.get("number") or item.get("phone"),
                                    "otp": item.get("otp") or extract_otp_code(item.get("sms", "")),
                                    "message": item.get("sms") or item.get("text", "")
                                }, provider_name="ZEBRASMS")
                except: pass

            # 4. Poll Yesms API
            for key in bot_settings.get("yesms_keys", []):
                try:
                    url = f"{YESMS_BASE_URL}/get_sms?api_key={key}"
                    res = requests.get(url, timeout=10).json()
                    if res.get("status") == "success" or isinstance(res, list):
                        data_list = res.get("data", res) if isinstance(res, dict) else res
                        if isinstance(data_list, list):
                            for item in data_list:
                                dispatch_otp({
                                    "number": item.get("number") or item.get("phone"),
                                    "otp": item.get("otp") or extract_otp_code(item.get("sms", "")),
                                    "message": item.get("sms") or item.get("text", "")
                                }, provider_name="YESMS")
                except: pass

            # 5. Poll Ksiiprn API
            for key in bot_settings.get("ksiiprn_keys", []):
                try:
                    url = f"{KSIIPRN_BASE_URL}/get_sms?api_key={key}"
                    res = requests.get(url, timeout=10).json()
                    if res.get("status") == "success" or isinstance(res, list):
                        data_list = res.get("data", res) if isinstance(res, dict) else res
                        if isinstance(data_list, list):
                            for item in data_list:
                                dispatch_otp({
                                    "number": item.get("number") or item.get("phone"),
                                    "otp": item.get("otp") or extract_otp_code(item.get("sms", "")),
                                    "message": item.get("sms") or item.get("text", "")
                                }, provider_name="KSIIPRN")
                except: pass

        except Exception as e:
            print(f"Provider poll error: {e}")
        time.sleep(10)

# Start Threads
threading.Thread(target=auto_login_panel_worker, daemon=True).start()
threading.Thread(target=panel_poll_worker, daemon=True).start()
threading.Thread(target=provider_poll_worker, daemon=True).start()
threading.Thread(target=retry_local_load, daemon=True).start()

# ==========================================
# Telegram Update Handling & UI
# ==========================================
def main_keyboard(user_id):
    kb = [
        [{"text": "Get Number", "icon_custom_emoji_id": "5352862640592949843", "callback_data": "get_number", "style": "primary"},
         {"text": "My Profile", "icon_custom_emoji_id": "5352861489541714456", "callback_data": "profile", "style": "primary"}],
        [{"text": "Refer & Earn", "icon_custom_emoji_id": "5420396762189831222", "callback_data": "refer", "style": "success"},
         {"text": "Withdraw", "icon_custom_emoji_id": "5348469219761626211", "callback_data": "withdraw", "style": "success"}],
        [{"text": "Support", "icon_custom_emoji_id": "5337302974806922068", "callback_data": "support", "style": "primary"}]
    ]
    if is_admin(user_id):
        kb.append([{"text": "Admin Panel", "icon_custom_emoji_id": "5353032893096567467", "callback_data": "admin_panel", "style": "danger"}])
    return {"inline_keyboard": kb}

def build_custom_message(key, placeholders=None):
    if placeholders is None:
        placeholders = {}
    
    cfg = bot_settings.get("custom_messages", {}).get(key, DEFAULT_CUSTOM_MESSAGES.get(key, {}))
    raw_text = cfg.get("text", "")
    
    # Placeholders replacement
    for k, v in placeholders.items():
        raw_text = raw_text.replace(f"{{{k}}}", str(v))
        
    buttons = cfg.get("buttons", [])
    inline_keyboard = []
    
    for row in buttons:
        btn_row = []
        for btn in row:
            b_data = {"text": btn.get("text", "")}
            if btn.get("url"): b_data["url"] = btn.get("url")
            elif btn.get("callback_data"): b_data["callback_data"] = btn.get("callback_data")
            if btn.get("icon_custom_emoji_id"): b_data["icon_custom_emoji_id"] = btn.get("icon_custom_emoji_id")
            if btn.get("style"): b_data["style"] = btn.get("style")
            btn_row.append(b_data)
        inline_keyboard.append(btn_row)
        
    return render_body_text(raw_text), inline_keyboard

def handle_update(update):
    try:
        if "message" in update:
            msg = update["message"]
            chat_id = msg["chat"]["id"]
            user_id = msg["from"]["id"]
            text = msg.get("text", "").strip()

            register_user_local(user_id)
            if is_user_banned(user_id): return

            # Mandatory Channel Join Check
            if not check_force_join(user_id):
                send_force_join_msg(chat_id)
                return

            # Commands
            if text.startswith("/start"):
                args = text.split()
                if len(args) > 1 and args[1].isdigit():
                    ref_by = int(args[1])
                    if ref_by != user_id:
                        u_doc = db.collection('users').document(str(user_id)).get()
                        if not u_doc.exists:
                            # Give Referral Bonus
                            ref_reward = bot_settings.get("refer_reward", 0.2)
                            db.collection('users').document(str(ref_by)).set({
                                "balance": local_ops.Increment(ref_reward),
                                "total_ref": local_ops.Increment(1)
                            }, merge=True)
                            send_message(ref_by, render_body_text(f"🎉 **New Referral!** You earned +{ref_reward} TK!"))

                # Register user if not exists
                db.collection('users').document(str(user_id)).set({
                    "id": user_id, "first_name": msg["from"].get("first_name", ""),
                    "username": msg["from"].get("username", "")
                }, merge=True)

                msg_text, extra_buttons = build_custom_message("start")
                kb = main_keyboard(user_id)
                if extra_buttons:
                    kb["inline_keyboard"] = extra_buttons + kb["inline_keyboard"]
                send_message(chat_id, msg_text, reply_markup=kb)
                return

            # Admin Broadcast / Text Inputs State Machine
            state = user_states.get(user_id)
            if is_admin(user_id) and state:
                if state == "wait_for_broadcast":
                    user_states.pop(user_id, None)
                    broadcast_copymessage(chat_id, msg["message_id"])
                    return

        elif "callback_query" in update:
            cb = update["callback_query"]
            cb_id = cb["id"]
            user_id = cb["from"]["id"]
            chat_id = cb["message"]["chat"]["id"]
            msg_id = cb["message"]["message_id"]
            data = cb.get("data", "")

            register_user_local(user_id)
            if is_user_banned(user_id):
                answer_callback(cb_id, "You are banned!", show_alert=True)
                return

            if data == "check_fj":
                if check_force_join(user_id):
                    answer_callback(cb_id, "Thank you for joining!", show_alert=True)
                    delete_message(chat_id, msg_id)
                    msg_text, extra_buttons = build_custom_message("start")
                    kb = main_keyboard(user_id)
                    send_message(chat_id, msg_text, reply_markup=kb)
                else:
                    answer_callback(cb_id, "You haven't joined all channels yet!", show_alert=True)
                return

            # Main Navigation Callbacks
            if data == "main_menu":
                msg_text, extra_buttons = build_custom_message("start")
                kb = main_keyboard(user_id)
                edit_message(chat_id, msg_id, msg_text, reply_markup=kb)
                return

            elif data == "profile":
                doc = db.collection('users').document(str(user_id)).get()
                u_data = doc.to_dict() if doc.exists else {}
                bal = u_data.get("balance", 0.0)
                tot_otp = u_data.get("total_otp", 0)
                tot_ref = u_data.get("total_ref", 0)

                prof_text = (
                    f"**👤 YOUR PROFILE**\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"**🆔 ID:** `{user_id}`\n"
                    f"**💰 Balance:** {bal:.2f} ৳\n"
                    f"**🔢 Total OTPs:** {tot_otp}\n"
                    f"**🫂 Referrals:** {tot_ref}\n"
                    f"━━━━━━━━━━━━━━━━━━"
                )
                kb = {"inline_keyboard": [[{"text": "Back", "callback_data": "main_menu"}]]}
                edit_message(chat_id, msg_id, render_body_text(prof_text), reply_markup=kb)
                return

            elif data == "refer":
                doc = db.collection('users').document(str(user_id)).get()
                u_data = doc.to_dict() if doc.exists else {}
                tot_ref = u_data.get("total_ref", 0)
                ref_reward = bot_settings.get("refer_reward", 0.2)
                ref_link = f"https://t.me/{BOT_USERNAME}?start={user_id}"

                msg_text, _ = build_custom_message("refer", {
                    "ref_link": ref_link, "total_ref": tot_ref, "ref_reward": ref_reward
                })
                kb = {"inline_keyboard": [[{"text": "Back", "callback_data": "main_menu"}]]}
                edit_message(chat_id, msg_id, msg_text, reply_markup=kb)
                return

            elif data == "withdraw":
                doc = db.collection('users').document(str(user_id)).get()
                u_data = doc.to_dict() if doc.exists else {}
                bal = u_data.get("balance", 0.0)
                tot_otp = u_data.get("total_otp", 0)
                tot_ref = u_data.get("total_ref", 0)
                min_w = bot_settings.get("min_withdraw", 30.0)

                msg_text, _ = build_custom_message("withdrawal", {
                    "total_otp": tot_otp, "total_ref": tot_ref, "bal": f"{bal:.2f}", "min_w": min_w
                })

                kb = []
                for m in bot_settings.get("w_methods", ["bKash", "Nagad"]):
                    kb.append([{"text": f"Withdraw via {m}", "callback_data": f"w_method_{m}"}])
                kb.append([{"text": "Back", "callback_data": "main_menu"}])

                edit_message(chat_id, msg_id, msg_text, reply_markup={"inline_keyboard": kb})
                return

            # Admin Callbacks
            elif data == "admin_panel" and is_admin(user_id):
                admin_text = (
                    f"**📊 ADMIN CONTROL PANEL**\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"Welcome Admin! Choose a section below to manage your bot settings."
                )
                kb = {"inline_keyboard": [
                    [{"text": "Panels & Keys", "callback_data": "admin_panels"},
                     {"text": "Services & Rates", "callback_data": "admin_services"}],
                    [{"text": "Broadcast Message", "callback_data": "admin_broadcast"},
                     {"text": "Withdraw Settings", "callback_data": "admin_withdraw"}],
                    [{"text": "Back to Main", "callback_data": "main_menu"}]
                ]}
                edit_message(chat_id, msg_id, render_body_text(admin_text), reply_markup=kb)
                return

            elif data == "admin_broadcast" and is_admin(user_id):
                user_states[user_id] = "wait_for_broadcast"
                edit_message(chat_id, msg_id, render_body_text("📢 **Send the message or photo you want to broadcast to all users:**"))
                return

    except Exception as e:
        print(f"Error handling update: {e}")

# ==========================================
# Main Bot Polling Loop
# ==========================================
def run_bot():
    global BOT_USERNAME
    # Fetch Bot Profile Info
    me = api_call("getMe")
    if me.get("ok"):
        BOT_USERNAME = me["result"].get("username", "")
        print(f"🤖 Bot Started Successfully as @{BOT_USERNAME}")

    # Start Health Check Web Server
    threading.Thread(target=start_health_check_server, daemon=True).start()

    offset = 0
    while True:
        try:
            updates = api_call("getUpdates", {"offset": offset, "timeout": 50})
            if updates.get("ok"):
                for update in updates.get("result", []):
                    offset = update["update_id"] + 1
                    threading.Thread(target=handle_update, args=(update,), daemon=True).start()
        except Exception as e:
            print(f"Polling error: {e}")
            time.sleep(5)

if __name__ == "__main__":
    run_bot()
