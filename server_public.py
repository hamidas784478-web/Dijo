from flask import Flask, request, jsonify, send_from_directory, send_file, session as flask_session
from openai import OpenAI
from werkzeug.security import generate_password_hash, check_password_hash
from config import GROQ_API_KEY
from datetime import datetime
import os, uuid, re, json, secrets, time
import database as db

app = Flask(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

_SK_PATH = os.path.join(BASE_DIR, "secret.key")
if os.path.exists(_SK_PATH):
    with open(_SK_PATH, "r", encoding="utf-8") as f:
        app.secret_key = f.read().strip()
else:
    app.secret_key = secrets.token_hex(32)
    with open(_SK_PATH, "w", encoding="utf-8") as f:
        f.write(app.secret_key)

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30
)

db.init_db()

_PUBLIC = {"/", "/dijo", "/status", "/auth/login", "/auth/register", "/auth/status"}

@app.before_request
def _require_auth():
    if request.path in _PUBLIC or request.path.startswith("/static/"):
        return None
    if not flask_session.get("user_id"):
        return jsonify({"error": True, "code": 401, "message": "لطفاً ابتدا وارد شوید.", "login_required": True}), 401


client = OpenAI(
    api_key=GROQ_API_KEY,
    base_url="https://api.groq.com/openai/v1",
    timeout=90.0,
    max_retries=0
)

NORMAL_MODELS = ["openai/gpt-oss-120b", "openai/gpt-oss-20b"]
WEB_MODEL = "groq/compound-mini"

def _extract_retry_seconds(error_text, default=6.0):
    m = re.search(r"try again in ([\d.]+)s", error_text)
    if m:
        try:
            return min(float(m.group(1)) + 1.0, 30.0)
        except ValueError:
            pass
    return default

def create_completion(model, **kwargs):
    try:
        return client.chat.completions.create(model=model, **kwargs)
    except Exception as error:
        msg = str(error)
        if "rate_limit_exceeded" in msg or "429" in msg:
            time.sleep(_extract_retry_seconds(msg))
            return client.chat.completions.create(model=model, **kwargs)
        raise


SYSTEM_PROMPT = """
تو «دیجو» هستی؛ دستیار هوشمند فارسی‌زبان مدیریت کسب‌وکار.
به پایگاه‌داده واقعی مشتریان، محصولات، فروش، پرداخت‌ها، بدهی‌ها، خریدها،
هزینه‌ها، درآمدها و وظایف متصل هستی.

قوانین:
1. همیشه فارسی و طبیعی صحبت کن مگر کاربر زبان دیگری بخواهد.
2. برای ثبت/ویرایش/حذف اطلاعات کسب‌وکار حتماً از ابزار واقعی استفاده کن.
3. برای فروش از add_order استفاده کن. اگر کاربر مبلغ پرداختی همان فروش را گفت،
   paid_amount و payment_method را هم بفرست.
4. هیچ عددی را از خودت نساز؛ گزارش‌ها را از ابزار بخوان.
5. اگر موجودی کافی نباشد، فروش را ثبت نکن.
6. بعد از موفقیت ابزار، نتیجه را کوتاه و دقیق بگو.
7. برای پرداخت بدهی قبلی از add_customer_payment استفاده کن.
8. برای فروش نسیه یا مانده فروش از add_order استفاده کن، نه ثبت بدهی جداگانه.
9. اگر قیمت محصول جدید مشخص نیست، از کاربر بپرس یا طبق ابزار جستجوی قیمت عمل کن.
10. لحن دوستانه، حرفه‌ای و مختصر باشد.
"""

WEB_SYSTEM_EXTRA = """
برای این درخواست اطلاعات به‌روز وب لازم است. اگر جستجوی وب انجام شد،
نتیجه را فارسی، کوتاه و با ذکر تاریخ/منبع در صورت امکان ارائه کن.
"""

# ------------------------- AI TOOLS -------------------------

def _resolve_customer(name):
    name=(name or "").strip()
    if not name: raise ValueError("نام مشتری مشخص نیست.")
    c=db.find_customer_by_name(name)
    if c: return c["id"], c["name"]
    return db.add_customer(name), name

def tool_add_customer(args):
    return {"ok":True,"customer_id":db.add_customer(args.get("name"),args.get("phone"),args.get("address"),args.get("note"))}

def tool_list_customers(args):
    return {"customers":db.list_customers(args.get("query"))}

def tool_add_product(args):
    return {"ok":True,"product_id":db.add_product(args.get("name"),args.get("price",0),args.get("stock",0),args.get("note"),args.get("cost_price",0))}

def tool_list_products(args):
    return {"products":db.list_products(args.get("query"))}

def tool_add_order(args):
    cid,cname=_resolve_customer(args.get("customer_name"))
    resolved=[]
    missing=[]
    for item in args.get("items",[]):
        name=(item.get("product_name") or "").strip()
        if not name: continue
        product=db.find_product_by_name(name)
        price=item.get("price")
        if product:
            if price is None: price=product["price"]
            resolved.append({
                "product_id":product["id"],"product_name":product["name"],
                "qty":item.get("qty",1),"price":price,
                "cost_price":product.get("cost_price",0)
            })
        elif price is not None:
            resolved.append({"product_id":None,"product_name":name,"qty":item.get("qty",1),"price":price,"cost_price":0})
        else:
            missing.append(name)
    if missing:
        return {"ok":False,"error":"قیمت این محصولات مشخص نیست: "+", ".join(missing)}
    if not resolved:
        return {"ok":False,"error":"هیچ قلمی برای فروش مشخص نشد."}
    result=db.add_order(
        cid,resolved,
        note=args.get("note"),
        paid_amount=args.get("paid_amount",0),
        payment_method=args.get("payment_method","نقدی"),
        payment_note=args.get("payment_note")
    )
    result["customer_name"]=cname
    return {"ok":True,**result}

def tool_list_orders(args):
    return {"orders":db.list_orders(args.get("limit",10))}

def tool_add_transaction(args):
    return {"ok":True,"transaction_id":db.add_transaction(args.get("title"),args.get("amount"),args.get("kind"),args.get("category"))}

def tool_add_task(args):
    return {"ok":True,"task_id":db.add_task(args.get("title"),args.get("due_date"))}

def tool_list_tasks(args):
    return {"tasks":db.list_tasks(args.get("only_open",False))}

def tool_complete_task(args):
    title=args.get("title","")
    tasks=db.list_tasks(True)
    m=next((x for x in tasks if title in x["title"]),None)
    if not m: return {"ok":False,"error":"وظیفه‌ای با این عنوان پیدا نشد."}
    db.set_task_done(m["id"],True)
    return {"ok":True,"task_id":m["id"]}

def tool_get_summary(args):
    return db.get_summary()

def tool_add_customer_debt(args):
    cid,name=_resolve_customer(args.get("customer_name"))
    lid=db.add_customer_debt(cid,args.get("amount"),args.get("note"))
    return {"ok":True,"ledger_id":lid,"customer_name":name,"new_balance":db.get_customer_balance(cid)}

def tool_add_customer_payment(args):
    cid,name=_resolve_customer(args.get("customer_name"))
    lid=db.add_customer_payment(cid,args.get("amount"),args.get("note"))
    return {"ok":True,"ledger_id":lid,"customer_name":name,"new_balance":db.get_customer_balance(cid)}

def tool_get_customer_balance(args):
    c=db.find_customer_by_name(args.get("customer_name"))
    if not c: return {"ok":False,"error":"مشتری پیدا نشد."}
    return {"ok":True,"customer_name":c["name"],"balance":db.get_customer_balance(c["id"]),"history":db.customer_ledger_history(c["id"],10)}

def tool_list_debtors(args):
    rows=db.list_customer_balances(args.get("min_amount",0))
    if args.get("only_debtors",True): rows=[x for x in rows if x["balance"]>0]
    return {"customers":rows}

def tool_add_purchase(args):
    supplier_name=(args.get("supplier_name") or "").strip()
    sid=None
    if supplier_name:
        s=db.find_supplier_by_name(supplier_name)
        sid=s["id"] if s else db.add_supplier(supplier_name)
    items=[]
    for x in args.get("items",[]):
        name=(x.get("product_name") or "").strip()
        if not name: continue
        if x.get("cost_price") is None: return {"ok":False,"error":f"قیمت خرید «{name}» مشخص نیست."}
        p=db.find_product_by_name(name)
        pid=p["id"] if p else db.add_product(name,0,0,None,0)
        items.append({"product_id":pid,"product_name":name,"qty":x.get("qty",1),"cost_price":x["cost_price"]})
    if not items: return {"ok":False,"error":"هیچ قلمی برای خرید مشخص نشد."}
    pid,total=db.add_purchase(sid,items,args.get("note"))
    return {"ok":True,"purchase_id":pid,"total":total}

def tool_list_purchases(args):
    return {"purchases":db.list_purchases(args.get("limit",10))}

def tool_product_profit_report(args):
    return {"products":db.product_profit_report(args.get("days",30))}

def tool_search_product_price(args):
    name=(args.get("product_name") or "").strip()
    if not name: return {"ok":False,"error":"نام محصول مشخص نیست."}
    details=(args.get("details") or "").strip()
    q=f"قیمت روز و معیار بازار محصول «{name}» {details} در بازار ایران به تومان چقدر است؟ بازه یا میانگین واقعی و به‌روز را کوتاه بگو و اگر منبع/تاریخ داری ذکر کن."
    try:
        r=create_completion(WEB_MODEL,messages=[
            {"role":"system","content":"فقط قیمت تقریبی بازار ایران را کوتاه و صادقانه بگو."},
            {"role":"user","content":q}],max_tokens=400)
        return {"ok":True,"product_name":name,"price_info":(r.choices[0].message.content or "").strip()}
    except Exception as e:
        return {"ok":False,"error":f"خطا در جستجوی قیمت: {e}"}

def tool_add_order_payment(args):
    order_id=args.get("order_id")
    return {"ok":True,**db.add_order_payment(order_id,args.get("amount"),args.get("method","نقدی"),args.get("note"))}

def tool_get_order(args):
    r=db.get_order_details(args.get("order_id"))
    return {"ok":bool(r),"order":r} if r else {"ok":False,"error":"سفارش پیدا نشد."}

def tool_cancel_order(args):
    db.cancel_order(args.get("order_id"),args.get("reason"))
    return {"ok":True,"order_id":args.get("order_id"),"status":"لغوشده"}

TOOLS=[
{"type":"function","function":{"name":"add_customer","description":"افزودن مشتری","parameters":{"type":"object","properties":{"name":{"type":"string"},"phone":{"type":"string"},"address":{"type":"string"},"note":{"type":"string"}},"required":["name"]}}},
{"type":"function","function":{"name":"list_customers","description":"لیست/جستجوی مشتریان","parameters":{"type":"object","properties":{"query":{"type":"string"}}}}},
{"type":"function","function":{"name":"add_product","description":"افزودن محصول","parameters":{"type":"object","properties":{"name":{"type":"string"},"price":{"type":"number"},"cost_price":{"type":"number"},"stock":{"type":"number"},"note":{"type":"string"}},"required":["name","price"]}}},
{"type":"function","function":{"name":"list_products","description":"لیست/جستجوی محصولات","parameters":{"type":"object","properties":{"query":{"type":"string"}}}}},
{"type":"function","function":{"name":"add_order","description":"ثبت فروش اتمیک؛ موجودی کم و پرداخت/بدهی همان فروش ثبت می‌شود.","parameters":{"type":"object","properties":{
"customer_name":{"type":"string"},
"items":{"type":"array","items":{"type":"object","properties":{"product_name":{"type":"string"},"qty":{"type":"number"},"price":{"type":"number"}},"required":["product_name","qty"]}},
"paid_amount":{"type":"number","description":"مبلغ پرداخت‌شده همان فروش"},
"payment_method":{"type":"string","description":"نقدی/کارت/انتقال/چک"},
"payment_note":{"type":"string"},
"note":{"type":"string"}},"required":["customer_name","items"]}}},
{"type":"function","function":{"name":"list_orders","description":"لیست فروش‌ها","parameters":{"type":"object","properties":{"limit":{"type":"integer"}}}}},
{"type":"function","function":{"name":"add_order_payment","description":"ثبت پرداخت جدید برای یک فروش موجود","parameters":{"type":"object","properties":{"order_id":{"type":"integer"},"amount":{"type":"number"},"method":{"type":"string"},"note":{"type":"string"}},"required":["order_id","amount"]}}},
{"type":"function","function":{"name":"get_order","description":"جزئیات یک فروش","parameters":{"type":"object","properties":{"order_id":{"type":"integer"}},"required":["order_id"]}}},
{"type":"function","function":{"name":"cancel_order","description":"لغو فروش و برگرداندن موجودی","parameters":{"type":"object","properties":{"order_id":{"type":"integer"},"reason":{"type":"string"}},"required":["order_id"]}}},
{"type":"function","function":{"name":"add_transaction","description":"ثبت هزینه/درآمد دستی","parameters":{"type":"object","properties":{"title":{"type":"string"},"amount":{"type":"number"},"kind":{"type":"string","enum":["income","expense"]},"category":{"type":"string"}},"required":["title","amount","kind"]}}},
{"type":"function","function":{"name":"add_task","description":"افزودن وظیفه","parameters":{"type":"object","properties":{"title":{"type":"string"},"due_date":{"type":"string"}},"required":["title"]}}},
{"type":"function","function":{"name":"list_tasks","description":"لیست وظایف","parameters":{"type":"object","properties":{"only_open":{"type":"boolean"}}}}},
{"type":"function","function":{"name":"complete_task","description":"تکمیل وظیفه","parameters":{"type":"object","properties":{"title":{"type":"string"}},"required":["title"]}}},
{"type":"function","function":{"name":"get_summary","description":"گزارش خلاصه کسب‌وکار","parameters":{"type":"object","properties":{}}}},
{"type":"function","function":{"name":"add_customer_debt","description":"ثبت بدهی جداگانه مشتری","parameters":{"type":"object","properties":{"customer_name":{"type":"string"},"amount":{"type":"number"},"note":{"type":"string"}},"required":["customer_name","amount"]}}},
{"type":"function","function":{"name":"add_customer_payment","description":"ثبت پرداخت بدهی قبلی مشتری","parameters":{"type":"object","properties":{"customer_name":{"type":"string"},"amount":{"type":"number"},"note":{"type":"string"}},"required":["customer_name","amount"]}}},
{"type":"function","function":{"name":"get_customer_balance","description":"مانده حساب مشتری","parameters":{"type":"object","properties":{"customer_name":{"type":"string"}},"required":["customer_name"]}}},
{"type":"function","function":{"name":"list_debtors","description":"لیست بدهکاران","parameters":{"type":"object","properties":{"only_debtors":{"type":"boolean"},"min_amount":{"type":"number"}}}}},
{"type":"function","function":{"name":"add_purchase","description":"ثبت خرید از تأمین‌کننده","parameters":{"type":"object","properties":{"supplier_name":{"type":"string"},"items":{"type":"array","items":{"type":"object","properties":{"product_name":{"type":"string"},"qty":{"type":"number"},"cost_price":{"type":"number"}},"required":["product_name","qty","cost_price"]}},"note":{"type":"string"}},"required":["items"]}}},
{"type":"function","function":{"name":"list_purchases","description":"لیست خریدها","parameters":{"type":"object","properties":{"limit":{"type":"integer"}}}}},
{"type":"function","function":{"name":"product_profit_report","description":"گزارش سود محصولات","parameters":{"type":"object","properties":{"days":{"type":"integer"}}}}},
{"type":"function","function":{"name":"search_product_price","description":"جستجوی قیمت بازار محصول","parameters":{"type":"object","properties":{"product_name":{"type":"string"},"details":{"type":"string"}},"required":["product_name"]}}}
]

TOOL_DISPATCH={
"add_customer":tool_add_customer,"list_customers":tool_list_customers,
"add_product":tool_add_product,"list_products":tool_list_products,
"add_order":tool_add_order,"list_orders":tool_list_orders,
"add_order_payment":tool_add_order_payment,"get_order":tool_get_order,"cancel_order":tool_cancel_order,
"add_transaction":tool_add_transaction,"add_task":tool_add_task,"list_tasks":tool_list_tasks,
"complete_task":tool_complete_task,"get_summary":tool_get_summary,
"add_customer_debt":tool_add_customer_debt,"add_customer_payment":tool_add_customer_payment,
"get_customer_balance":tool_get_customer_balance,"list_debtors":tool_list_debtors,
"add_purchase":tool_add_purchase,"list_purchases":tool_list_purchases,
"product_profit_report":tool_product_profit_report,"search_product_price":tool_search_product_price
}

conversations={}
MAX_MESSAGES=24

def get_history(session_id):
    if session_id not in conversations: conversations[session_id]=[]
    return conversations[session_id]

def limit_history(history):
    if len(history)>MAX_MESSAGES: del history[:-MAX_MESSAGES]

def needs_web_search(message):
    business=["مشتری","محصول","سفارش","فروش","موجودی","هزینه","درآمد","وظیفه","کسب‌وکار","کسب و کار","بدهی","خرید"]
    return not any(x in message for x in business)

def local_memory_answer(message,history):
    text=message.strip()
    if "اسمم" in text and ("چیه" in text or "چی بود" in text):
        for x in reversed(history):
            if x.get("role")=="user":
                m=re.search(r"اسمم\s+([آ-یA-Za-z]+)",x.get("content",""))
                if m:return f"اسمت {m.group(1)} بود. 😎"
        return "تا الان اسمت رو واضح نگفتی."
    if "چی داشتیم" in text or "موضوع قبلی" in text:
        u=[x["content"] for x in history if x.get("role")=="user"]
        return f"قبل از این درباره «{u[-2]}» صحبت می‌کردیم." if len(u)>=2 else "هنوز گفت‌وگوی زیادی نداشتیم."
    return None

def run_tool_calls(tool_calls):
    out=[]
    for call in tool_calls:
        name=call.function.name
        try: args=json.loads(call.function.arguments or "{}")
        except Exception: args={}
        handler=TOOL_DISPATCH.get(name)
        if not handler: result={"ok":False,"error":f"ابزار ناشناخته: {name}"}
        else:
            try: result=handler(args)
            except Exception as e: result={"ok":False,"error":str(e)}
        out.append({"tool_call_id":call.id,"role":"tool","name":name,"content":json.dumps(result,ensure_ascii=False,default=str)})
    return out

def normal_chat(history):
    last=None
    for model in NORMAL_MODELS:
        try:
            messages=[{"role":"system","content":SYSTEM_PROMPT}]+list(history)
            for _ in range(5):
                r=create_completion(model,messages=messages,tools=TOOLS,tool_choice="auto",temperature=.4,max_tokens=1500)
                choice=r.choices[0].message
                if choice.tool_calls:
                    messages.append({"role":"assistant","content":choice.content or "","tool_calls":[
                        {"id":tc.id,"type":"function","function":{"name":tc.function.name,"arguments":tc.function.arguments}}
                        for tc in choice.tool_calls]})
                    messages.extend(run_tool_calls(choice.tool_calls))
                    continue
                if choice.content:return choice.content.strip(),model
                break
            raise RuntimeError("مدل پاسخ نهایی نداد.")
        except Exception as e:
            last=e
    raise last

def web_chat(history):
    trimmed=[{"role":x["role"],"content":str(x.get("content",""))[:500]} for x in history[-4:]]
    r=create_completion(WEB_MODEL,messages=[{"role":"system","content":SYSTEM_PROMPT+WEB_SYSTEM_EXTRA}]+trimmed,max_tokens=1200)
    if not r.choices[0].message.content: raise RuntimeError("پاسخ وب خالی بود.")
    return r.choices[0].message.content.strip()

# ------------------------- AUTH -------------------------

@app.route("/auth/status")
def auth_status():
    return jsonify({"logged_in":bool(flask_session.get("user_id")),"username":flask_session.get("username",""),"first_time":db.user_count()==0})

@app.route("/auth/register",methods=["POST"])
def auth_register():
    d=request.get_json(silent=True) or {}; username=(d.get("username") or "").strip(); password=(d.get("password") or "").strip()
    if not username or not password:return jsonify({"ok":False,"error":"نام کاربری و رمز عبور الزامی است."}),400
    if len(password)<4:return jsonify({"ok":False,"error":"رمز عبور باید حداقل ۴ کاراکتر باشد."}),400
    uid=db.add_user(username,generate_password_hash(password))
    db.set_current_user(uid)
    flask_session.permanent=True; flask_session["user_id"]=uid; flask_session["username"]=username
    return jsonify({"ok":True,"username":username})

@app.route("/auth/login",methods=["POST"])
def auth_login():
    d=request.get_json(silent=True) or {}; username=(d.get("username") or "").strip(); password=(d.get("password") or "").strip()
    u=db.get_user_by_username(username)
    if not u or not check_password_hash(u["password_hash"],password): return jsonify({"ok":False,"error":"نام کاربری یا رمز عبور اشتباه است."}),401
    flask_session.permanent=True; flask_session["user_id"]=u["id"]; flask_session["username"]=u["username"]
    return jsonify({"ok":True,"username":u["username"]})

@app.route("/auth/logout",methods=["POST"])
def auth_logout():
    flask_session.clear(); return jsonify({"ok":True})

# ------------------------- BACKUP / PAGES -------------------------

@app.route("/api/backup")
def api_backup():
    fname="dijo_backup_"+datetime.now().strftime("%Y%m%d_%H%M%S")+".db"
    return send_file(db.get_db_path(),as_attachment=True,download_name=fname)

@app.route("/")
def home():
    return jsonify({"status":"online","server":"Dijo","multi_user":True})

@app.route("/dijo")
def dijo(): return send_from_directory(BASE_DIR,"dijo.html")

@app.route("/status")
def status():
    return jsonify({
        "status":"online","server":"Dijo","provider":"Groq",
        "memory":True,"web_search":True,"business_tools":True,
        "web_model":WEB_MODEL,"v2":True,"multi_user":True
    })

# ------------------------- CHAT -------------------------

@app.route("/chat",methods=["POST"])
def chat():
    try:
        data=request.get_json(silent=True) or {}
        message=str(data.get("message","")).strip()
        if not message:return jsonify({"error":"پیام خالی است."}),400
        sid=str(data.get("session_id","")).strip() or str(uuid.uuid4())
        history=get_history(sid)
        local=local_memory_answer(message,history)
        if local:
            history.append({"role":"assistant","content":local}); limit_history(history)
            return jsonify({"reply":local,"session_id":sid,"mode":"memory"})
        history.append({"role":"user","content":message}); limit_history(history)
        if needs_web_search(message):
            try:
                reply=web_chat(history); history.append({"role":"assistant","content":reply}); limit_history(history)
                return jsonify({"reply":reply,"session_id":sid,"mode":"web"})
            except Exception:
                try:
                    reply,model=normal_chat(history); history.append({"role":"assistant","content":reply}); limit_history(history)
                    return jsonify({"reply":"فعلاً جست‌وجوی زنده وب در دسترس نبود.\n\n"+reply,"session_id":sid,"mode":"fallback"})
                except Exception:
                    if history and history[-1]["role"]=="user":history.pop()
                    return jsonify({"reply":"فعلاً دیجو نتونست وصل بشه. چند لحظه بعد دوباره امتحان کن.","session_id":sid,"mode":"error"})
        try:
            reply,model=normal_chat(history); history.append({"role":"assistant","content":reply}); limit_history(history)
            return jsonify({"reply":reply,"session_id":sid,"mode":"normal","model":model})
        except Exception:
            try:
                reply=web_chat(history); history.append({"role":"assistant","content":reply}); limit_history(history)
                return jsonify({"reply":reply,"session_id":sid,"mode":"web_fallback"})
            except Exception:
                if history and history[-1]["role"]=="user":history.pop()
                return jsonify({"reply":"فعلاً اتصال دیجو مشکل دارد. دوباره امتحان کن.","session_id":sid,"mode":"offline"})
    except Exception as e:
        return jsonify({"reply":"یک خطای موقت در دیجو رخ داد.","error":str(e)}),200

# ------------------------- REST API -------------------------

@app.route("/api/customers",methods=["GET","POST"])
def api_customers():
    if request.method=="POST":
        d=request.get_json(silent=True) or {}
        return jsonify({"ok":True,"id":db.add_customer(d.get("name"),d.get("phone"),d.get("address"),d.get("note"))})
    return jsonify(db.list_customers(request.args.get("q")))

@app.route("/api/customers/<int:customer_id>",methods=["PUT","DELETE"])
def api_customer(customer_id):
    if request.method=="PUT":
        d=request.get_json(silent=True) or {}
        return jsonify({"ok":db.update_customer(customer_id,d.get("name"),d.get("phone"),d.get("address"),d.get("note"))})
    db.delete_customer(customer_id); return jsonify({"ok":True})

@app.route("/api/customers/<int:customer_id>/ledger",methods=["GET","POST"])
def api_customer_ledger(customer_id):
    if request.method=="POST":
        d=request.get_json(silent=True) or {}; kind=d.get("kind")
        if kind not in ("debt","payment"):return jsonify({"error":"نوع باید debt یا payment باشد."}),400
        lid=db.add_customer_debt(customer_id,d.get("amount"),d.get("note")) if kind=="debt" else db.add_customer_payment(customer_id,d.get("amount"),d.get("note"))
        return jsonify({"ok":True,"id":lid,"balance":db.get_customer_balance(customer_id)})
    return jsonify({"balance":db.get_customer_balance(customer_id),"history":db.customer_ledger_history(customer_id)})

@app.route("/api/customers/<int:customer_id>/ledger/<int:entry_id>",methods=["DELETE"])
def api_delete_ledger(customer_id,entry_id):
    db.delete_ledger_entry(entry_id); return jsonify({"ok":True,"balance":db.get_customer_balance(customer_id)})

@app.route("/api/debtors")
def api_debtors():
    rows=db.list_customer_balances(0); only=request.args.get("only","debtors")
    if only=="debtors":rows=[r for r in rows if r["balance"]>0]
    elif only=="creditors":rows=[r for r in rows if r["balance"]<0]
    return jsonify(rows)

@app.route("/api/suppliers",methods=["GET","POST"])
def api_suppliers():
    if request.method=="POST":
        d=request.get_json(silent=True) or {}; return jsonify({"ok":True,"id":db.add_supplier(d.get("name"),d.get("phone"),d.get("note"))})
    return jsonify(db.list_suppliers(request.args.get("q")))

@app.route("/api/suppliers/<int:supplier_id>",methods=["DELETE"])
def api_supplier_delete(supplier_id):
    db.delete_supplier(supplier_id); return jsonify({"ok":True})

@app.route("/api/purchases",methods=["GET","POST"])
def api_purchases():
    if request.method=="POST":return jsonify(tool_add_purchase(request.get_json(silent=True) or {}))
    return jsonify(db.list_purchases(int(request.args.get("limit",20))))

@app.route("/api/purchases/<int:purchase_id>",methods=["DELETE"])
def api_purchase_delete(purchase_id):
    db.delete_purchase(purchase_id); return jsonify({"ok":True})

@app.route("/api/products",methods=["GET","POST"])
def api_products():
    if request.method=="POST":
        d=request.get_json(silent=True) or {}
        return jsonify({"ok":True,"id":db.add_product(d.get("name"),d.get("price",0),d.get("stock",0),d.get("note"),d.get("cost_price",0))})
    return jsonify(db.list_products(request.args.get("q")))

@app.route("/api/products/<int:product_id>",methods=["PUT","DELETE"])
def api_product(product_id):
    if request.method=="PUT":
        d=request.get_json(silent=True) or {}
        return jsonify({"ok":db.update_product(product_id,d.get("name"),d.get("price"),d.get("cost_price"),d.get("stock"),d.get("note"))})
    db.delete_product(product_id); return jsonify({"ok":True})

@app.route("/api/products/<int:product_id>/stock-history")
def api_product_stock_history(product_id):
    return jsonify(db.stock_history(product_id))

@app.route("/api/products/profit")
def api_product_profit():
    return jsonify(db.product_profit_report(int(request.args.get("days",30))))

@app.route("/api/orders",methods=["GET","POST"])
def api_orders():
    if request.method=="POST":return jsonify(tool_add_order(request.get_json(silent=True) or {}))
    return jsonify(db.list_orders(int(request.args.get("limit",20))))

@app.route("/api/orders/<int:order_id>",methods=["GET","DELETE"])
def api_order(order_id):
    if request.method=="GET":
        r=db.get_order_details(order_id)
        return (jsonify(r),200) if r else (jsonify({"ok":False,"error":"سفارش پیدا نشد."}),404)
    db.delete_order(order_id); return jsonify({"ok":True})

@app.route("/api/orders/<int:order_id>/payment",methods=["POST"])
def api_order_payment(order_id):
    d=request.get_json(silent=True) or {}
    return jsonify({"ok":True,**db.add_order_payment(order_id,d.get("amount"),d.get("method","نقدی"),d.get("note"))})

@app.route("/api/orders/<int:order_id>/cancel",methods=["POST"])
def api_order_cancel(order_id):
    d=request.get_json(silent=True) or {}
    db.cancel_order(order_id,d.get("reason")); return jsonify({"ok":True})

@app.route("/api/transactions",methods=["GET","POST"])
def api_transactions():
    if request.method=="POST":
        d=request.get_json(silent=True) or {}
        return jsonify({"ok":True,"id":db.add_transaction(d.get("title"),d.get("amount"),d.get("kind"),d.get("category"))})
    return jsonify(db.list_transactions(int(request.args.get("limit",50))))

@app.route("/api/transactions/<int:t_id>",methods=["DELETE"])
def api_transaction_delete(t_id):
    db.delete_transaction(t_id); return jsonify({"ok":True})

@app.route("/api/tasks",methods=["GET","POST"])
def api_tasks():
    if request.method=="POST":
        d=request.get_json(silent=True) or {}; return jsonify({"ok":True,"id":db.add_task(d.get("title"),d.get("due_date"))})
    return jsonify(db.list_tasks(request.args.get("open")=="1"))

@app.route("/api/tasks/<int:task_id>/done",methods=["POST"])
def api_task_done(task_id):
    db.set_task_done(task_id,True); return jsonify({"ok":True})

@app.route("/api/tasks/<int:task_id>",methods=["DELETE"])
def api_task_delete(task_id):
    db.delete_task(task_id); return jsonify({"ok":True})

@app.route("/api/summary")
def api_summary(): return jsonify(db.get_summary())

if __name__=="__main__":
    print("==============================")
    print("       DIJO BUSINESS SERVER v2")
    print("==============================")
    print("Server listening on port 8000")
    print("Dijo page: /dijo")
    print("Provider: Groq")
    print("Business DB:",db.DB_PATH)
    print("==============================")
    app.run(host="127.0.0.1",port=8000,debug=False,threaded=True,use_reloader=False)
