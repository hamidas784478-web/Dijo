
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash, Response, send_file
import sqlite3, os, secrets, csv, io, shutil, json, urllib.request, urllib.error, hmac, hashlib
from functools import wraps
from datetime import datetime, date
from werkzeug.security import generate_password_hash, check_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "dijo.db")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")

# --- Licensing (offline, no server/phone-home needed) ---------------------
# Codes are verified with an HMAC signature, so activation works even with
# no internet connection. IMPORTANT: change this secret to your own random
# string before distributing the app - anyone who has this exact secret can
# mint their own valid codes. Keep it out of any public repo.
# Generate codes for customers with: python3 tools/generate_license.py PRO
LICENSE_SECRET = os.environ.get("DIJO_LICENSE_SECRET", "dijo-change-this-license-secret-before-selling")
PRO_FEATURES = {"chat", "api_ai", "api_ai_test", "export_csv", "backup", "restore"}
# Free-plan usage caps. These are the levers that make Pro worth paying for —
# once real usage passes these, the day-to-day job (adding stock, ringing up
# a sale, adding a second cashier) stops working until upgrading.
FREE_LIMITS = {"products": 20, "customers": 30, "suppliers": 10, "sales_per_month": 15, "staff_users": 1}

def free_cap_reached(con, kind):
    """Returns (True, current_count) once a free-plan cap is hit; Pro has no caps."""
    if is_pro():
        return False, 0
    if kind == "sales_per_month":
        month = datetime.now().strftime("%Y-%m")
        n = con.execute("SELECT COUNT(*) n FROM sales WHERE substr(created_at,1,7)=?", (month,)).fetchone()["n"]
    elif kind == "staff_users":
        n = con.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
    else:
        table = {"products": "products", "customers": "customers", "suppliers": "suppliers"}[kind]
        n = con.execute(f"SELECT COUNT(*) n FROM {table}").fetchone()["n"]
    return n >= FREE_LIMITS[kind], n

app = Flask(__name__)
app.secret_key = os.environ.get("DIJO_SECRET_KEY", "dijo-change-this-secret-key")
app.config["JSON_AS_ASCII"] = False
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024

def to_float(value, default=0.0, minimum=None):
    """Safely parse Persian/English numeric form values without causing 500 errors."""
    if value is None:
        n = default
    else:
        text = str(value).strip().replace(",", "").replace("٬", "").replace("٫", ".")
        trans = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
        text = text.translate(trans)
        try:
            n = float(text) if text else default
        except (TypeError, ValueError):
            n = default
    if minimum is not None:
        n = max(minimum, n)
    return n

def safe_text(value, limit=500):
    return str(value or "").strip()[:limit]


def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con

def ensure_column(con, table, column, definition):
    try:
        cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in cols:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    except sqlite3.Error:
        pass

def migrate_legacy_schema(con):
    # Compatibility with older Dijo builds that used password_hash.
    ensure_column(con, "users", "first_name", "TEXT NOT NULL DEFAULT 'مدیر'")
    ensure_column(con, "users", "last_name", "TEXT NOT NULL DEFAULT 'دیجو'")
    ensure_column(con, "users", "password", "TEXT NOT NULL DEFAULT ''")
    ensure_column(con, "users", "role", "TEXT NOT NULL DEFAULT 'مدیر'")
    ensure_column(con, "users", "created_at", "TEXT NOT NULL DEFAULT ''")
    try:
        cols={r[1] for r in con.execute("PRAGMA table_info(users)").fetchall()}
        if "password_hash" in cols:
            con.execute("UPDATE users SET password=password_hash WHERE (password IS NULL OR password='') AND password_hash IS NOT NULL")
    except sqlite3.Error:
        pass
    for col, definition in [
        ("phone","TEXT"),("address","TEXT"),("notes","TEXT"),("created_at","TEXT NOT NULL DEFAULT ''"),
    ]:
        ensure_column(con,"customers",col,definition)
        ensure_column(con,"suppliers",col,definition)
    for col, definition in [
        ("customer_id","INTEGER"),("discount","REAL DEFAULT 0"),("paid","REAL DEFAULT 0"),("payment_method","TEXT DEFAULT 'نقدی'"),("status","TEXT DEFAULT 'تسویه'"),
    ]:
        ensure_column(con,"sales",col,definition)
    for col, definition in [
        ("supplier_id","INTEGER"),("total","REAL DEFAULT 0"),("paid","REAL DEFAULT 0"),("status","TEXT DEFAULT 'تسویه'"),("created_at","TEXT NOT NULL DEFAULT ''"),
    ]:
        ensure_column(con,"purchases",col,definition)
    ensure_column(con, "products", "unit", "TEXT DEFAULT 'عدد'")
    for col, definition in [
        ("code", "TEXT"),("barcode","TEXT"),("category","TEXT"),("brand","TEXT"),
        ("buy_price","REAL DEFAULT 0"),("sell_price","REAL DEFAULT 0"),("wholesale_price","REAL DEFAULT 0"),
        ("stock","REAL DEFAULT 0"),("min_stock","REAL DEFAULT 0"),("location","TEXT"),("description","TEXT"),("image","TEXT"),
    ]:
        ensure_column(con,"products",col,definition)

def init_db():
    con = db()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        first_name TEXT NOT NULL DEFAULT 'مدیر',
        last_name TEXT NOT NULL DEFAULT 'دیجو',
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'مدیر',
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS customers(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL, phone TEXT, address TEXT, notes TEXT,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS suppliers(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL, phone TEXT, address TEXT, notes TEXT,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS products(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL, code TEXT, barcode TEXT, category TEXT, brand TEXT,
        unit TEXT DEFAULT 'عدد', buy_price REAL DEFAULT 0, sell_price REAL DEFAULT 0,
        wholesale_price REAL DEFAULT 0, stock REAL DEFAULT 0, min_stock REAL DEFAULT 0,
        location TEXT, description TEXT, image TEXT,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS sales(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_id INTEGER, total REAL DEFAULT 0, discount REAL DEFAULT 0,
        paid REAL DEFAULT 0, payment_method TEXT DEFAULT 'نقدی',
        status TEXT DEFAULT 'تسویه', created_at TEXT NOT NULL,
        FOREIGN KEY(customer_id) REFERENCES customers(id) ON DELETE SET NULL
    );
    CREATE TABLE IF NOT EXISTS sale_items(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sale_id INTEGER NOT NULL, product_id INTEGER NOT NULL,
        qty REAL NOT NULL, price REAL NOT NULL,
        FOREIGN KEY(sale_id) REFERENCES sales(id) ON DELETE CASCADE,
        FOREIGN KEY(product_id) REFERENCES products(id)
    );
    CREATE TABLE IF NOT EXISTS purchases(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        supplier_id INTEGER, total REAL DEFAULT 0, paid REAL DEFAULT 0,
        status TEXT DEFAULT 'تسویه', created_at TEXT NOT NULL,
        FOREIGN KEY(supplier_id) REFERENCES suppliers(id) ON DELETE SET NULL
    );
    CREATE TABLE IF NOT EXISTS transactions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL, category TEXT NOT NULL, title TEXT NOT NULL,
        amount REAL DEFAULT 0, note TEXT, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS tasks(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL, description TEXT, priority TEXT DEFAULT 'متوسط',
        due_date TEXT, status TEXT DEFAULT 'باز', category TEXT DEFAULT 'عمومی',
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS notifications(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL, body TEXT, level TEXT DEFAULT 'info',
        is_read INTEGER DEFAULT 0, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS activities(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, action TEXT NOT NULL, created_at TEXT NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE SET NULL
    );
    CREATE TABLE IF NOT EXISTS stock_movements(
        id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER NOT NULL,
        change REAL NOT NULL, reason TEXT NOT NULL, user_id INTEGER, created_at TEXT NOT NULL,
        FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE CASCADE,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE SET NULL
    );
    CREATE TABLE IF NOT EXISTS app_settings(
        key TEXT PRIMARY KEY, value TEXT
    );
    CREATE TABLE IF NOT EXISTS product_categories(
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS payments(
        id INTEGER PRIMARY KEY AUTOINCREMENT, entity_type TEXT NOT NULL, entity_id INTEGER NOT NULL,
        amount REAL NOT NULL DEFAULT 0, method TEXT DEFAULT 'نقدی', note TEXT, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS purchase_items(
        id INTEGER PRIMARY KEY AUTOINCREMENT, purchase_id INTEGER NOT NULL, product_id INTEGER NOT NULL,
        qty REAL NOT NULL, price REAL NOT NULL,
        FOREIGN KEY(purchase_id) REFERENCES purchases(id) ON DELETE CASCADE,
        FOREIGN KEY(product_id) REFERENCES products(id)
    );
    CREATE TABLE IF NOT EXISTS returns(
        id INTEGER PRIMARY KEY AUTOINCREMENT, sale_id INTEGER NOT NULL, product_id INTEGER NOT NULL,
        qty REAL NOT NULL, created_at TEXT NOT NULL,
        FOREIGN KEY(sale_id) REFERENCES sales(id) ON DELETE CASCADE,
        FOREIGN KEY(product_id) REFERENCES products(id)
    );
    """)
    migrate_legacy_schema(con)
    # Lightweight migrations for upgrades from older Dijo databases.
    for col, typ in [("unit","TEXT DEFAULT 'عدد'")]:
        try: con.execute(f"ALTER TABLE products ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError: pass
    if not con.execute("SELECT 1 FROM product_categories LIMIT 1").fetchone():
        for r in con.execute("SELECT DISTINCT category FROM products WHERE category IS NOT NULL AND category!=''"):
            con.execute("INSERT OR IGNORE INTO product_categories(name,created_at) VALUES(?,?)",(r[0],datetime.now().isoformat()))
    con.commit()

    # No demo/admin user is created here on purpose. A brand-new install has
    # zero users; the /setup route (see below) walks the first person who
    # opens the app through creating the real admin account.
    # AI settings are seeded ONLY from environment variables set by whoever
    # deploys the app (e.g. pointing at your own ai_proxy — see ai_proxy/),
    # never with a literal key in source, and never requested from the end
    # user. DIJO_AI_ENDPOINT/DIJO_AI_API_KEY win when present; otherwise it
    # falls back to talking to Groq directly with GROQ_API_KEY if that's set.
    env_endpoint = os.environ.get("DIJO_AI_ENDPOINT", "").strip() or GROQ_ENDPOINT
    env_model = os.environ.get("DIJO_AI_MODEL", "").strip() or GROQ_MODEL
    env_key = os.environ.get("DIJO_AI_API_KEY", "").strip() or GROQ_API_KEY
    con.execute("INSERT OR IGNORE INTO app_settings(key,value) VALUES(?,?)", ("ai_provider", "groq"))
    con.execute("INSERT OR IGNORE INTO app_settings(key,value) VALUES(?,?)", ("ai_model", env_model))
    con.execute("INSERT OR IGNORE INTO app_settings(key,value) VALUES(?,?)", ("ai_endpoint", env_endpoint))
    if env_key and not con.execute("SELECT 1 FROM app_settings WHERE key=?", ("ai_api_key",)).fetchone():
        con.execute("INSERT INTO app_settings(key,value) VALUES(?,?)", ("ai_api_key", env_key))
    # No demo products/customers/sales either — every install starts empty.
    con.commit()
    con.close()

def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapper

def log_action(action):
    con = db()
    con.execute("INSERT INTO activities(user_id,action,created_at) VALUES(?,?,?)",
                (session.get("user_id"), action, datetime.now().isoformat()))
    con.commit(); con.close()

def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if session.get("role") not in ("مدیر", "admin"):
            flash("دسترسی این بخش فقط برای مدیر سیستم است.", "error")
            return redirect(url_for("dashboard"))
        return fn(*args, **kwargs)
    return wrapper

def generate_license_code(plan="PRO", secret=None):
    secret = secret or LICENSE_SECRET
    nonce = secrets.token_hex(4).upper()
    sig = hmac.new(secret.encode(), f"{plan}:{nonce}".encode(), hashlib.sha256).hexdigest()[:12].upper()
    return f"DIJO-{plan}-{nonce}-{sig}"

def verify_license_code(code, secret=None):
    secret = secret or LICENSE_SECRET
    try:
        parts = (code or "").strip().upper().replace(" ", "").split("-")
        if len(parts) != 4 or parts[0] != "DIJO":
            return None
        _, plan, nonce, sig = parts
        expected = hmac.new(secret.encode(), f"{plan}:{nonce}".encode(), hashlib.sha256).hexdigest()[:12].upper()
        if hmac.compare_digest(expected, sig):
            return plan
    except Exception:
        pass
    return None

def is_pro():
    return bool(setting_get("license_plan", ""))

def pro_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not is_pro():
            wants_json = request.is_json or "application/json" in (request.headers.get("Accept") or "") or request.path.startswith("/api/")
            if wants_json:
                return jsonify({"ok": False, "error": "این قابلیت جزو نسخه Pro است. از صفحه «ارتقا به Pro» کد لایسنس را فعال کنید.", "license_required": True}), 402
            flash("این قابلیت جزو نسخه Pro دیجو است. برای فعال‌سازی، کد لایسنس را وارد کنید.", "error")
            return redirect(url_for("license_page"))
        return fn(*args, **kwargs)
    return wrapper

def stats():
    con = db()
    sales = con.execute("SELECT COALESCE(SUM(total),0) n FROM sales WHERE date(created_at)=date('now','localtime')").fetchone()["n"]
    month = con.execute("SELECT COALESCE(SUM(total),0) n FROM sales WHERE strftime('%Y-%m',created_at)=strftime('%Y-%m','now','localtime')").fetchone()["n"]
    orders = con.execute("SELECT COUNT(*) n FROM sales").fetchone()["n"]
    customers = con.execute("SELECT COUNT(*) n FROM customers").fetchone()["n"]
    low = con.execute("SELECT COUNT(*) n FROM products WHERE stock<=min_stock").fetchone()["n"]
    expenses = con.execute("SELECT COALESCE(SUM(amount),0) n FROM transactions WHERE kind='expense'").fetchone()["n"]
    profit = con.execute("SELECT COALESCE(SUM(si.qty*(si.price-p.buy_price)),0) n FROM sale_items si JOIN products p ON p.id=si.product_id").fetchone()["n"]
    con.close()
    return dict(today=sales, month=month, orders=orders, customers=customers, low=low, expenses=expenses, profit=profit)

@app.context_processor
def inject():
    s = {}
    if session.get("user_id"):
        try:
            s = stats()
        except Exception:
            s = {}
    return {"now": datetime.now(), "stats": s}

@app.errorhandler(404)
def not_found(e):
    return render_template("404.html") if os.path.exists(os.path.join(BASE,"templates","404.html")) else ("صفحه پیدا نشد", 404)

@app.errorhandler(500)
def server_error(e):
    return render_template("500.html") if os.path.exists(os.path.join(BASE,"templates","500.html")) else ("خطای سرور", 500)

@app.route("/sw.js")
def service_worker():
    return app.send_static_file("sw.js"), 200, {"Content-Type":"application/javascript","Service-Worker-Allowed":"/"}

@app.before_request
def require_setup():
    # Brand-new install: force the very first visitor through /setup to
    # create the real admin account instead of shipping a fixed login.
    if request.endpoint in (None, "setup", "static", "service_worker"):
        return
    con = db(); has_user = con.execute("SELECT 1 FROM users LIMIT 1").fetchone(); con.close()
    if not has_user:
        return redirect(url_for("setup"))

@app.route("/setup", methods=["GET","POST"])
def setup():
    con = db(); has_user = con.execute("SELECT 1 FROM users LIMIT 1").fetchone(); con.close()
    if has_user:
        return redirect(url_for("login"))
    if request.method == "POST":
        f = request.form
        first = safe_text(f.get("first_name"), 80); last = safe_text(f.get("last_name"), 80)
        username = safe_text(f.get("username"), 80); password = f.get("password", "")
        confirm = f.get("confirm", "")
        if not first or not username or len(password) < 4:
            flash("نام، نام کاربری و رمز حداقل ۴ رقمی الزامی است.", "error")
            return render_template("setup.html")
        if password != confirm:
            flash("رمز عبور و تکرار آن یکسان نیستند.", "error")
            return render_template("setup.html")
        con = db()
        con.execute("INSERT INTO users(first_name,last_name,username,password,role,created_at) VALUES(?,?,?,?,?,?)",
                    (first, last or "", username, generate_password_hash(password), "مدیر", datetime.now().isoformat()))
        con.commit()
        user = con.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone(); con.close()
        session["user_id"] = user["id"]; session["user_name"] = f"{first} {last}".strip(); session["role"] = "مدیر"
        flash("حساب مدیر ساخته شد. به دیجو خوش آمدید!", "success")
        return redirect(url_for("dashboard"))
    return render_template("setup.html")

@app.route("/")
def index():
    return redirect(url_for("dashboard") if session.get("user_id") else url_for("login"))

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username","").strip()
        password = request.form.get("password","")
        con = db(); user = con.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        ok = False
        if user:
            stored = user["password"] or ""
            try: ok = check_password_hash(stored, password)
            except Exception: ok = False
            if not ok and secrets.compare_digest(stored, password):
                ok = True
                con.execute("UPDATE users SET password=? WHERE id=?", (generate_password_hash(password), user["id"]))
                con.commit()
        con.close()
        if ok:
            session["user_id"] = user["id"]; session["user_name"] = user["first_name"] + " " + user["last_name"]; session["role"] = user["role"]
            log_action("ورود به سیستم")
            return redirect(url_for("dashboard"))
        flash("نام کاربری یا رمز عبور اشتباه است.", "error")
    return render_template("login.html")

@app.route("/register", methods=["GET","POST"])
def register():
    if request.method == "POST":
        f=request.form.get("first_name","").strip(); l=request.form.get("last_name","").strip()
        u=request.form.get("username","").strip(); p=request.form.get("password","")
        if not all([f,l,u,p]) or p != request.form.get("confirm"):
            flash("اطلاعات ثبت‌نام کامل یا یکسان نیست.", "error")
        else:
            con=db()
            hit,n = free_cap_reached(con, "staff_users")
            if hit:
                con.close()
                flash("در نسخه رایگان فقط یک حساب کاربری (مدیر) مجاز است. برای افزودن کارمند/فروشنده، Pro را فعال کنید.","error")
                return redirect(url_for("license_page"))
            try:
                con.execute("INSERT INTO users(first_name,last_name,username,password,role,created_at) VALUES(?,?,?,?,?,?)",
                                      (f,l,u,generate_password_hash(p),"فروشنده",datetime.now().isoformat())); con.commit(); con.close()
                flash("حساب ساخته شد؛ حالا وارد شوید.","success"); return redirect(url_for("login"))
            except sqlite3.IntegrityError:
                flash("این نام کاربری قبلاً استفاده شده است.","error")
    return render_template("register.html")

@app.route("/forgot-password", methods=["GET","POST"])
def forgot_password():
    if request.method=="POST":
        flash("در نسخه فعلی بازیابی رمز به‌صورت نمایشی است.","success")
    return render_template("forgot_password.html")

@app.route("/logout")
def logout():
    session.clear(); return redirect(url_for("login"))

@app.route("/dashboard")
@login_required
def dashboard():
    con=db()
    recent_sales=con.execute("""SELECT s.*, c.name customer FROM sales s LEFT JOIN customers c ON c.id=s.customer_id
                                ORDER BY s.id DESC LIMIT 6""").fetchall()
    top_products=con.execute("""SELECT p.name, COALESCE(SUM(si.qty),0) qty FROM sale_items si
                                JOIN products p ON p.id=si.product_id GROUP BY p.id ORDER BY qty DESC LIMIT 5""").fetchall()
    low_products=con.execute("SELECT * FROM products WHERE stock<=min_stock ORDER BY stock ASC LIMIT 5").fetchall()
    activities=con.execute("""SELECT a.*, COALESCE(u.first_name||' '||u.last_name,'سیستم') user_name
                             FROM activities a LEFT JOIN users u ON u.id=a.user_id ORDER BY a.id DESC LIMIT 6""").fetchall()
    chart=con.execute("""SELECT substr(created_at,1,10) d, COALESCE(SUM(total),0) total FROM sales
                         GROUP BY d ORDER BY d DESC LIMIT 12""").fetchall()
    con.close()
    return render_template("dashboard.html", recent_sales=recent_sales, top_products=top_products,
                           low_products=low_products, activities=activities, chart=list(reversed(chart)))

@app.route("/chat", methods=["GET","POST"])
@login_required
@pro_required
def chat():
    answer=None; message=""
    if request.method=="POST":
        message=request.form.get("message","").strip()
        if message: answer=ai_answer(message)
    return render_template("chat.html", answer=answer, message=message)

@app.route("/customers", methods=["GET","POST"])
@login_required
def customers():
    con=db()
    if request.method=="POST":
        hit,n = free_cap_reached(con, "customers")
        if hit:
            con.close(); flash(f"در نسخه رایگان حداکثر {FREE_LIMITS['customers']} مشتری قابل ثبت است. برای مشتری نامحدود، Pro را فعال کنید.","error")
            return redirect(url_for("license_page"))
        con.execute("INSERT INTO customers(name,phone,address,notes,created_at) VALUES(?,?,?,?,?)",
                    (request.form["name"],request.form.get("phone"),request.form.get("address"),
                     request.form.get("notes"),datetime.now().isoformat()))
        con.commit(); log_action(f"مشتری «{request.form['name']}» اضافه شد")
        return redirect(url_for("customers"))
    q=request.args.get("q","").strip()
    rows=con.execute("SELECT * FROM customers WHERE name LIKE ? OR phone LIKE ? ORDER BY id DESC", (f"%{q}%",f"%{q}%")).fetchall()
    con.close(); return render_template("customers.html", rows=rows, q=q)

@app.route("/customers/delete/<int:id>", methods=["POST"])
@login_required
def delete_customer(id):
    con=db(); r=con.execute("SELECT name FROM customers WHERE id=?",(id,)).fetchone()
    con.execute("DELETE FROM customers WHERE id=?",(id,)); con.commit(); con.close()
    if r: log_action(f"مشتری «{r['name']}» حذف شد")
    return redirect(url_for("customers"))

@app.route("/products", methods=["GET","POST"])
@login_required
def products():
    con=db()
    if request.method=="POST":
        f=request.form
        name=f.get("name","").strip()
        if not name:
            flash("نام محصول الزامی است.","error"); con.close(); return redirect(url_for("products"))
        hit,n = free_cap_reached(con, "products")
        if hit:
            con.close(); flash(f"در نسخه رایگان حداکثر {FREE_LIMITS['products']} محصول قابل ثبت است. برای محصول نامحدود، Pro را فعال کنید.","error")
            return redirect(url_for("license_page"))
        vals=(name,safe_text(f.get("code"),100),safe_text(f.get("barcode"),100),safe_text(f.get("category"),100),safe_text(f.get("brand"),100),safe_text(f.get("unit","عدد"),30) or "عدد",
              to_float(f.get("buy_price"),0,0),to_float(f.get("sell_price"),0,0),to_float(f.get("wholesale_price"),0,0),
              to_float(f.get("stock"),0,0),to_float(f.get("min_stock"),0,0),safe_text(f.get("location"),200),safe_text(f.get("description"),1000),datetime.now().isoformat())
        try:
            cur=con.execute("""INSERT INTO products(name,code,barcode,category,brand,unit,buy_price,sell_price,wholesale_price,stock,min_stock,location,description,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",vals)
            pid=cur.lastrowid
            if vals[9]: con.execute("INSERT INTO stock_movements(product_id,change,reason,user_id,created_at) VALUES(?,?,?,?,?)",(pid,vals[9],"موجودی اولیه",session.get("user_id"),datetime.now().isoformat()))
            if vals[3]: con.execute("INSERT OR IGNORE INTO product_categories(name,created_at) VALUES(?,?)",(vals[3],datetime.now().isoformat()))
            con.commit(); log_action(f"محصول «{name}» اضافه شد"); flash("محصول با موفقیت اضافه شد.","success")
        except sqlite3.IntegrityError:
            flash("کد یا بارکد تکراری است.","error")
        return redirect(url_for("products"))
    q=request.args.get("q","").strip(); cat=request.args.get("category","").strip(); low=request.args.get("low","")
    where=["1=1"]; args=[]
    if q: where.append("(p.name LIKE ? OR p.code LIKE ? OR p.barcode LIKE ? OR p.brand LIKE ?)"); args += [f"%{q}%"]*4
    if cat: where.append("p.category=?"); args.append(cat)
    if low: where.append("p.stock<=p.min_stock")
    rows=con.execute("SELECT p.*, (p.stock*p.buy_price) stock_value, (p.stock*p.sell_price) retail_value FROM products p WHERE " + " AND ".join(where) + " ORDER BY CASE WHEN p.stock<=p.min_stock THEN 0 ELSE 1 END,p.id DESC",args).fetchall()
    categories=con.execute("SELECT name FROM product_categories ORDER BY name").fetchall()
    total_stock=con.execute("SELECT COALESCE(SUM(stock*buy_price),0)n FROM products").fetchone()["n"]
    retail_stock=con.execute("SELECT COALESCE(SUM(stock*sell_price),0)n FROM products").fetchone()["n"]
    low_count=con.execute("SELECT COUNT(*)n FROM products WHERE stock<=min_stock").fetchone()["n"]
    movement_count=con.execute("SELECT COUNT(*)n FROM stock_movements").fetchone()["n"]
    con.close(); return render_template("products.html",rows=rows,q=q,category=cat,categories=categories,total_stock=total_stock,retail_stock=retail_stock,low_count=low_count,movement_count=movement_count)

@app.route("/products/edit/<int:id>", methods=["POST"])
@login_required
def edit_product(id):
    f=request.form; con=db(); old=con.execute("SELECT * FROM products WHERE id=?",(id,)).fetchone()
    if not old: con.close(); flash("محصول پیدا نشد.","error"); return redirect(url_for("products"))
    new_stock=to_float(f.get("stock"),old["stock"],0); change=new_stock-old["stock"]
    con.execute("""UPDATE products SET name=?,code=?,barcode=?,category=?,brand=?,unit=?,buy_price=?,sell_price=?,wholesale_price=?,stock=?,min_stock=?,location=?,description=? WHERE id=?""",
                (safe_text(f.get("name"),200),safe_text(f.get("code"),100),safe_text(f.get("barcode"),100),safe_text(f.get("category"),100),safe_text(f.get("brand"),100),safe_text(f.get("unit","عدد"),30) or "عدد",to_float(f.get("buy_price"),0,0),to_float(f.get("sell_price"),0,0),to_float(f.get("wholesale_price"),0,0),new_stock,to_float(f.get("min_stock"),0,0),safe_text(f.get("location"),200),safe_text(f.get("description"),1000),id))
    if change: con.execute("INSERT INTO stock_movements(product_id,change,reason,user_id,created_at) VALUES(?,?,?,?,?)",(id,change,"ویرایش محصول",session.get("user_id"),datetime.now().isoformat()))
    con.commit(); con.close(); log_action(f"محصول «{old['name']}» ویرایش شد"); flash("اطلاعات محصول به‌روزرسانی شد.","success"); return redirect(url_for("products"))

@app.route("/products/delete/<int:id>", methods=["POST"])
@login_required
def delete_product(id):
    con=db(); p=con.execute("SELECT * FROM products WHERE id=?",(id,)).fetchone()
    if not p:
        con.close(); flash("محصول پیدا نشد.","error"); return redirect(url_for("products"))
    sold=con.execute("SELECT COUNT(*) n FROM sale_items WHERE product_id=?",(id,)).fetchone()["n"]
    if sold:
        # Keep historical invoices valid: archive instead of physically deleting.
        try:
            con.execute("UPDATE products SET name=? WHERE id=?", (p["name"] + " (بایگانی)", id))
        except sqlite3.Error:
            pass
        con.commit(); con.close(); log_action(f"محصول «{p['name']}» بایگانی شد"); flash("این محصول سابقه فروش دارد؛ برای حفظ فاکتورها بایگانی شد.","success")
    else:
        con.execute("DELETE FROM products WHERE id=?",(id,)); con.commit(); con.close(); log_action(f"محصول «{p['name']}» حذف شد"); flash("محصول حذف شد.","success")
    return redirect(url_for("products"))

@app.route("/suppliers", methods=["GET","POST"])
@login_required
def suppliers():
    con=db()
    if request.method=="POST":
        hit,n = free_cap_reached(con, "suppliers")
        if hit:
            con.close(); flash(f"در نسخه رایگان حداکثر {FREE_LIMITS['suppliers']} تأمین‌کننده قابل ثبت است. برای Pro اقدام کنید.","error")
            return redirect(url_for("license_page"))
        f=request.form
        con.execute("INSERT INTO suppliers(name,phone,address,notes,created_at) VALUES(?,?,?,?,?)",
                    (f["name"],f.get("phone"),f.get("address"),f.get("notes"),datetime.now().isoformat()))
        con.commit(); log_action(f"تأمین‌کننده «{f['name']}» اضافه شد")
        return redirect(url_for("suppliers"))
    rows=con.execute("SELECT * FROM suppliers ORDER BY id DESC").fetchall(); con.close()
    return render_template("suppliers.html", rows=rows)

@app.route("/sales", methods=["GET","POST"])
@login_required
def sales():
    con=db()
    if request.method=="POST":
        f=request.form
        customer_id=f.get("customer_id") or None
        ids=request.form.getlist("product_id[]"); qtys=request.form.getlist("qty[]")
        total=0; items=[]
        for pid,qty in zip(ids,qtys):
            q=to_float(qty,0,0)
            p=con.execute("SELECT * FROM products WHERE id=?",(pid,)).fetchone()
            if p and q>0:
                if q > p["stock"]:
                    flash(f"موجودی «{p['name']}» کافی نیست.","error"); con.close(); return redirect(url_for("sales"))
                price=p["sell_price"]; total += price*q; items.append((p,q,price))
        discount=to_float(f.get("discount"),0,0); paid=to_float(f.get("paid"),0,0)
        discount=min(discount,total); total=max(0,total-discount); paid=min(paid,total); status="تسویه" if paid>=total else "بدهکار"
        if not items:
            flash("حداقل یک محصول با تعداد بیشتر از صفر انتخاب کنید.","error"); con.close(); return redirect(url_for("sales"))
        hit,n = free_cap_reached(con, "sales_per_month")
        if hit:
            con.close(); flash(f"در نسخه رایگان حداکثر {FREE_LIMITS['sales_per_month']} فاکتور فروش در ماه قابل ثبت است. برای فاکتور نامحدود، Pro را فعال کنید.","error")
            return redirect(url_for("license_page"))
        cur=con.execute("INSERT INTO sales(customer_id,total,discount,paid,payment_method,status,created_at) VALUES(?,?,?,?,?,?,?)",
                        (customer_id,total,discount,paid,f.get("payment_method","نقدی"),status,datetime.now().isoformat()))
        sid=cur.lastrowid
        for p,q,price in items:
            con.execute("INSERT INTO sale_items(sale_id,product_id,qty,price) VALUES(?,?,?,?)",(sid,p["id"],q,price))
            con.execute("UPDATE products SET stock=stock-? WHERE id=?",(q,p["id"]))
            con.execute("INSERT INTO stock_movements(product_id,change,reason,user_id,created_at) VALUES(?,?,?,?,?)",(p["id"],-q,f"فروش فاکتور #{sid}",session.get("user_id"),datetime.now().isoformat()))
        if customer_id and paid>0:
            con.execute("INSERT INTO payments(entity_type,entity_id,amount,method,note,created_at) VALUES(?,?,?,?,?,?)",("customer",customer_id,paid,f.get("payment_method","نقدی"),f"پرداخت فاکتور #{sid}",datetime.now().isoformat()))
        con.commit(); con.close(); log_action(f"فاکتور فروش #{sid} ثبت شد")
        return redirect(url_for("sales"))
    rows=con.execute("""SELECT s.*,c.name customer FROM sales s LEFT JOIN customers c ON c.id=s.customer_id ORDER BY s.id DESC""").fetchall()
    customers=con.execute("SELECT * FROM customers ORDER BY name").fetchall()
    products=con.execute("SELECT * FROM products WHERE stock>0 ORDER BY name").fetchall()
    con.close(); return render_template("sales.html", rows=rows, customers=customers, products=products)

@app.route("/purchases", methods=["GET","POST"])
@login_required
def purchases():
    con=db()
    if request.method=="POST":
        f=request.form; total=to_float(f.get("total"),0,0); paid=min(to_float(f.get("paid"),0,0),total)
        if total <= 0:
            flash("مبلغ خرید باید بیشتر از صفر باشد.","error"); con.close(); return redirect(url_for("purchases"))
        status="تسویه" if paid>=total else "بدهکار"
        cur=con.execute("INSERT INTO purchases(supplier_id,total,paid,status,created_at) VALUES(?,?,?,?,?)",
                        (f.get("supplier_id") or None,total,paid,status,datetime.now().isoformat()))
        pid=cur.lastrowid
        if f.get("supplier_id") and paid>0:
            con.execute("INSERT INTO payments(entity_type,entity_id,amount,method,note,created_at) VALUES(?,?,?,?,?,?)",("supplier",f.get("supplier_id"),paid,f.get("payment_method","نقدی"),f"پرداخت فاکتور خرید #{pid}",datetime.now().isoformat()))
        con.commit(); con.close(); log_action(f"فاکتور خرید #{pid} ثبت شد")
        return redirect(url_for("purchases"))
    rows=con.execute("""SELECT p.*,s.name supplier FROM purchases p LEFT JOIN suppliers s ON s.id=p.supplier_id
                        ORDER BY p.id DESC""").fetchall()
    suppliers=con.execute("SELECT * FROM suppliers ORDER BY name").fetchall(); con.close()
    return render_template("purchases.html", rows=rows, suppliers=suppliers)

@app.route("/finance", methods=["GET","POST"])
@login_required
def finance():
    con=db()
    if request.method=="POST":
        f=request.form
        con.execute("INSERT INTO transactions(kind,category,title,amount,note,created_at) VALUES(?,?,?,?,?,?)",
                    (f.get("kind","expense"),safe_text(f.get("category","سایر"),100),safe_text(f.get("title"),200),to_float(f.get("amount"),0,0),safe_text(f.get("note"),1000),datetime.now().isoformat()))
        con.commit(); con.close(); log_action(f"تراکنش «{f['title']}» ثبت شد"); return redirect(url_for("finance"))
    rows=con.execute("SELECT * FROM transactions ORDER BY id DESC").fetchall()
    income=con.execute("SELECT COALESCE(SUM(amount),0)n FROM transactions WHERE kind='income'").fetchone()["n"]
    expense=con.execute("SELECT COALESCE(SUM(amount),0)n FROM transactions WHERE kind='expense'").fetchone()["n"]
    con.close(); return render_template("finance.html", rows=rows, income=income, expense=expense)

@app.route("/tasks", methods=["GET","POST"])
@login_required
def tasks():
    con=db()
    if request.method=="POST":
        f=request.form
        con.execute("""INSERT INTO tasks(title,description,priority,due_date,status,category,created_at)
                       VALUES(?,?,?,?,?,?,?)""",(f["title"],f.get("description"),f.get("priority","متوسط"),
                       f.get("due_date"),"باز",f.get("category","عمومی"),datetime.now().isoformat()))
        con.commit(); con.close(); log_action(f"وظیفه «{f['title']}» ساخته شد"); return redirect(url_for("tasks"))
    rows=con.execute("SELECT * FROM tasks ORDER BY CASE WHEN status='باز' THEN 0 ELSE 1 END,due_date").fetchall()
    con.close(); return render_template("tasks.html", rows=rows)

@app.route("/tasks/done/<int:id>", methods=["POST"])
@login_required
def task_done(id):
    con=db(); con.execute("UPDATE tasks SET status='تکمیل‌شده' WHERE id=?",(id,)); con.commit(); con.close()
    return redirect(url_for("tasks"))

@app.route("/inventory")
@login_required
def inventory():
    con=db()
    rows=con.execute("""SELECT p.*, (p.stock*p.buy_price) value, (p.stock*p.sell_price) retail_value FROM products p ORDER BY CASE WHEN p.stock<=p.min_stock THEN 0 ELSE 1 END,p.name""").fetchall()
    movements=con.execute("""SELECT m.*,p.name product,COALESCE(u.first_name||' '||u.last_name,'سیستم') user_name FROM stock_movements m JOIN products p ON p.id=m.product_id LEFT JOIN users u ON u.id=m.user_id ORDER BY m.id DESC LIMIT 30""").fetchall()
    summary=con.execute("SELECT COUNT(*) products,COALESCE(SUM(stock),0) units,COALESCE(SUM(stock*buy_price),0) value,COALESCE(SUM(CASE WHEN stock<=min_stock THEN 1 ELSE 0 END),0) low FROM products").fetchone()
    con.close(); return render_template("inventory.html",rows=rows,movements=movements,summary=summary)

def setting_get(key, default=""):
    con=db(); r=con.execute("SELECT value FROM app_settings WHERE key=?",(key,)).fetchone(); con.close(); return r["value"] if r else default

def setting_set(key,value):
    con=db(); con.execute("INSERT INTO app_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(key,value)); con.commit(); con.close()

def _ai_config():
    return {
        "provider": setting_get("ai_provider", os.environ.get("DIJO_AI_PROVIDER", "groq")),
        "key": setting_get("ai_api_key", os.environ.get("DIJO_AI_API_KEY", GROQ_API_KEY)),
        "model": setting_get("ai_model", os.environ.get("DIJO_AI_MODEL", GROQ_MODEL)),
        "endpoint": setting_get("ai_endpoint", os.environ.get("DIJO_AI_ENDPOINT", GROQ_ENDPOINT)),
    }

def _ai_request(messages, timeout=35):
    cfg=_ai_config(); key=cfg["key"]
    if not key:
        raise RuntimeError("کلید API تنظیم نشده است.")
    endpoint=cfg["endpoint"].strip() or GROQ_ENDPOINT
    model=cfg["model"].strip() or GROQ_MODEL
    if not endpoint.startswith(("https://","http://")):
        raise RuntimeError("Endpoint نامعتبر است.")
    payload={"model":model,"messages":messages,"temperature":0.2,"max_completion_tokens":700}
    req=urllib.request.Request(endpoint,data=json.dumps(payload,ensure_ascii=False).encode("utf-8"),headers={"Content-Type":"application/json","Authorization":"Bearer "+key.strip()},method="POST")
    try:
        with urllib.request.urlopen(req,timeout=timeout) as r:
            raw=r.read().decode("utf-8",errors="replace")
            out=json.loads(raw)
    except urllib.error.HTTPError as e:
        body=e.read().decode("utf-8",errors="replace")
        try:
            detail=json.loads(body).get("error",{}).get("message") or body[:500]
        except Exception:
            detail=body[:500] or str(e)
        raise RuntimeError(f"Groq HTTP {e.code}: {detail}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"عدم دسترسی شبکه/DNS: {e.reason}")
    except TimeoutError:
        raise RuntimeError("زمان اتصال به سرویس هوش مصنوعی تمام شد.")
    except json.JSONDecodeError:
        raise RuntimeError("پاسخ سرویس هوش مصنوعی JSON معتبر نبود.")
    except Exception as e:
        raise RuntimeError(f"خطای اتصال: {e}")
    try:
        content=out["choices"][0]["message"]["content"]
    except (KeyError,IndexError,TypeError):
        raise RuntimeError("پاسخ سرویس فاقد متن پاسخ بود.")
    return str(content).strip(), cfg

def ai_answer(message):
    cfg=_ai_config()
    if not cfg["key"]:
        s=stats(); q=message.lower()
        if "فروش" in q: return f"فروش ماه جاری: {s['month']:,.0f} تومان"
        if "موجودی" in q or "انبار" in q: return f"{s['low']} محصول در وضعیت هشدار موجودی هستند و ارزش خرید کل موجودی {inventory_value():,.0f} تومان است."
        if "مشتری" in q: return f"تعداد مشتریان ثبت‌شده: {s['customers']:,}"
        if "سود" in q: return f"سود ناخالص محاسبه‌شده: {s['profit']:,.0f} تومان"
        return "کلید API تنظیم نشده است. از بخش «اتصال هوش مصنوعی» کلید را ذخیره کنید."
    con=db()
    context=con.execute("SELECT p.name,p.category,p.stock,p.min_stock,p.buy_price,p.sell_price FROM products p ORDER BY p.stock ASC LIMIT 40").fetchall()
    sales=con.execute("SELECT COALESCE(SUM(total),0)n,COUNT(*)c FROM sales").fetchone()
    customers=con.execute("SELECT COUNT(*)n FROM customers").fetchone()["n"]
    low=con.execute("SELECT COUNT(*)n FROM products WHERE stock<=min_stock").fetchone()["n"]
    con.close()
    data={"sales_total":sales["n"],"orders":sales["c"],"customers":customers,"low_stock":low,"products":[dict(x) for x in context]}
    system=("تو دستیار هوشمند نرم‌افزار دیجو هستی. فارسی، دقیق و کاربردی پاسخ بده. "
            "فقط از داده‌های کسب‌وکار زیر برای اعداد استفاده کن و عددی را که در داده نیست اختراع نکن. "
            "اگر کاربر درخواست ثبت/حذف/ویرایش عملیاتی داشت، بگو آن عملیات از بخش مربوطه انجام شود.\nداده‌ها: "
            +json.dumps(data,ensure_ascii=False))
    try:
        text,_=_ai_request([{"role":"system","content":system},{"role":"user","content":message}])
        return text or "پاسخی از هوش مصنوعی دریافت نشد."
    except RuntimeError as e:
        return "❌ اتصال هوش مصنوعی ناموفق بود: " + str(e)


def inventory_value():
    con=db(); n=con.execute("SELECT COALESCE(SUM(stock*buy_price),0)n FROM products").fetchone()["n"]; con.close(); return n

@app.route("/api/ai", methods=["POST"])
@login_required
@pro_required
def api_ai():
    message=(request.json or {}).get("message","").strip() if request.is_json else request.form.get("message","").strip()
    if not message: return jsonify({"ok":False,"error":"پیام خالی است"}),400
    answer=ai_answer(message); log_action("استفاده از دستیار هوش مصنوعی"); return jsonify({"ok":True,"answer":answer})

@app.route("/api/ai/test", methods=["POST"])
@login_required
@admin_required
@pro_required
def api_ai_test():
    started=datetime.now().timestamp()
    try:
        answer,cfg=_ai_request([{"role":"user","content":"Reply with exactly: DIJO_OK"}],timeout=25)
        ms=round((datetime.now().timestamp()-started)*1000)
        return jsonify({"ok":True,"answer":answer,"model":cfg["model"],"latency_ms":ms})
    except Exception as e:
        ms=round((datetime.now().timestamp()-started)*1000)
        return jsonify({"ok":False,"error":str(e),"latency_ms":ms}),200

@app.route("/ai-settings", methods=["GET","POST"])
@login_required
@admin_required
def ai_settings():
    if request.method=="POST":
        for k in ("ai_provider","ai_model","ai_endpoint"):
            value=request.form.get(k,"").strip()
            if value: setting_set(k,value)
        new_key=request.form.get("ai_api_key","").strip()
        if new_key: setting_set("ai_api_key",new_key)
        flash("اتصال هوش مصنوعی ذخیره شد.","success")
    return render_template("ai_settings.html", provider=setting_get("ai_provider","groq"), model=setting_get("ai_model",GROQ_MODEL), endpoint=setting_get("ai_endpoint",GROQ_ENDPOINT), has_key=bool(setting_get("ai_api_key",os.environ.get("DIJO_AI_API_KEY",GROQ_API_KEY))))

@app.route("/invoice/<int:id>")
@login_required
def invoice(id):
    con=db(); sale=con.execute("SELECT s.*,COALESCE(c.name,'مشتری متفرقه') customer,COALESCE(c.phone,'') phone,COALESCE(c.address,'') address FROM sales s LEFT JOIN customers c ON c.id=s.customer_id WHERE s.id=?",(id,)).fetchone()
    if not sale: con.close(); flash("فاکتور پیدا نشد.","error"); return redirect(url_for("sales"))
    items=con.execute("SELECT si.*,p.name,p.code,p.unit FROM sale_items si JOIN products p ON p.id=si.product_id WHERE si.sale_id=?",(id,)).fetchall()
    returned=con.execute("SELECT product_id,COALESCE(SUM(qty),0) qty FROM returns WHERE sale_id=? GROUP BY product_id",(id,)).fetchall()
    business={r['key']:r['value'] for r in con.execute("SELECT key,value FROM app_settings WHERE key IN ('business_name','business_phone','business_address','currency')").fetchall()}
    con.close(); return render_template("invoice.html",sale=sale,items=items,returned={r['product_id']:r['qty'] for r in returned},business=business)

@app.route("/api/products/search")
@login_required
def product_search_api():
    q=safe_text(request.args.get("q",""),100)
    con=db(); rows=con.execute("SELECT id,name,code,barcode,sell_price,stock,unit FROM products WHERE stock>0 AND (name LIKE ? OR code LIKE ? OR barcode LIKE ?) ORDER BY name LIMIT 15",(f"%{q}%",f"%{q}%",f"%{q}%")).fetchall(); con.close()
    return jsonify([dict(r) for r in rows])

@app.route("/payments/<entity_type>/<int:id>", methods=["GET","POST"])
@login_required
def payments(entity_type,id):
    if entity_type not in ("customer","supplier"): return "نوع نامعتبر",400
    con=db()
    table='customers' if entity_type=='customer' else 'suppliers'
    ent=con.execute(f"SELECT * FROM {table} WHERE id=?",(id,)).fetchone()
    if not ent: con.close(); return "مورد پیدا نشد",404
    if request.method=='POST':
        amount=to_float(request.form.get('amount'),0,0)
        if amount<=0: flash("مبلغ پرداخت معتبر نیست.","error")
        else:
            con.execute("INSERT INTO payments(entity_type,entity_id,amount,method,note,created_at) VALUES(?,?,?,?,?,?)",(entity_type,id,amount,safe_text(request.form.get('method','نقدی'),30),safe_text(request.form.get('note'),300),datetime.now().isoformat()))
            con.commit(); log_action(f"پرداخت برای {entity_type} #{id} ثبت شد"); flash("پرداخت ثبت شد.","success")
        con.close(); return redirect(url_for("payments",entity_type=entity_type,id=id))
    if entity_type=='customer':
        gross=con.execute("SELECT COALESCE(SUM(total),0)n FROM sales WHERE customer_id=?",(id,)).fetchone()['n']
        legacy_paid=con.execute("SELECT COALESCE(SUM(paid),0)n FROM sales WHERE customer_id=?",(id,)).fetchone()['n']
    else:
        gross=con.execute("SELECT COALESCE(SUM(total),0)n FROM purchases WHERE supplier_id=?",(id,)).fetchone()['n']
        legacy_paid=con.execute("SELECT COALESCE(SUM(paid),0)n FROM purchases WHERE supplier_id=?",(id,)).fetchone()['n']
    ledger_paid=con.execute("SELECT COALESCE(SUM(amount),0)n FROM payments WHERE entity_type=? AND entity_id=?",(entity_type,id)).fetchone()['n']
    paid=max(legacy_paid,ledger_paid)
    history=con.execute("SELECT * FROM payments WHERE entity_type=? AND entity_id=? ORDER BY id DESC",(entity_type,id)).fetchall(); con.close()
    return render_template("payments.html",entity=ent,entity_type=entity_type,gross=gross,paid=paid,balance=max(0,gross-paid),history=history)

@app.route("/sale/return/<int:id>", methods=["GET","POST"])
@login_required
def sale_return(id):
    con=db(); sale=con.execute("SELECT * FROM sales WHERE id=?",(id,)).fetchone()
    if not sale: con.close(); return "فاکتور پیدا نشد",404
    items=con.execute("SELECT si.*,p.name,p.stock FROM sale_items si JOIN products p ON p.id=si.product_id WHERE si.sale_id=?",(id,)).fetchall()
    if request.method=='POST':
        for item in items:
            q=to_float(request.form.get(f"qty_{item['product_id']}"),0,0)
            prev=con.execute("SELECT COALESCE(SUM(qty),0)n FROM returns WHERE sale_id=? AND product_id=?",(id,item['product_id'])).fetchone()['n']
            if q>max(0,item['qty']-prev): con.close(); flash(f"مقدار مرجوعی «{item['name']}» بیش از مقدار فروخته‌شده است.","error"); return redirect(url_for('sale_return',id=id))
            if q>0:
                con.execute("INSERT INTO returns(sale_id,product_id,qty,created_at) VALUES(?,?,?,?)",(id,item['product_id'],q,datetime.now().isoformat()))
                con.execute("UPDATE products SET stock=stock+? WHERE id=?",(q,item['product_id']))
                con.execute("INSERT INTO stock_movements(product_id,change,reason,user_id,created_at) VALUES(?,?,?,?,?)",(item['product_id'],q,f"مرجوعی فاکتور #{id}",session.get('user_id'),datetime.now().isoformat()))
        con.commit(); con.close(); log_action(f"مرجوعی فاکتور #{id} ثبت شد"); flash("مرجوعی ثبت شد و موجودی اصلاح شد.","success"); return redirect(url_for('invoice',id=id))
    con.close(); return render_template('sale_return.html',sale=sale,items=items)

@app.route("/debts")
@login_required
def debts():
    con=db()
    customers=con.execute("SELECT c.id,c.name,COALESCE(SUM(s.total),0) gross,COALESCE(SUM(s.paid),0) legacy_paid,COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.entity_type='customer' AND p.entity_id=c.id),0) ledger_paid FROM customers c LEFT JOIN sales s ON s.customer_id=c.id GROUP BY c.id HAVING gross-CASE WHEN legacy_paid>ledger_paid THEN legacy_paid ELSE ledger_paid END>0 ORDER BY (gross-CASE WHEN legacy_paid>ledger_paid THEN legacy_paid ELSE ledger_paid END) DESC").fetchall()
    suppliers=con.execute("SELECT s.id,s.name,COALESCE(SUM(pu.total),0) gross,COALESCE(SUM(pu.paid),0) legacy_paid,COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.entity_type='supplier' AND p.entity_id=s.id),0) ledger_paid FROM suppliers s LEFT JOIN purchases pu ON pu.supplier_id=s.id GROUP BY s.id HAVING gross-CASE WHEN legacy_paid>ledger_paid THEN legacy_paid ELSE ledger_paid END>0 ORDER BY (gross-CASE WHEN legacy_paid>ledger_paid THEN legacy_paid ELSE ledger_paid END) DESC").fetchall()
    con.close(); return render_template('debts.html',customers=customers,suppliers=suppliers)

@app.route("/notifications")
@login_required
def notifications():
    con=db()
    now=datetime.now().isoformat()
    con.execute("""INSERT INTO notifications(title,body,level,created_at) SELECT 'هشدار موجودی کم','یک یا چند محصول به حداقل موجودی رسیده‌اند.','warning',? WHERE EXISTS(SELECT 1 FROM products WHERE stock<=min_stock) AND NOT EXISTS(SELECT 1 FROM notifications WHERE title='هشدار موجودی کم' AND date(created_at)=date('now','localtime'))""",(now,))
    con.execute("""INSERT INTO notifications(title,body,level,created_at) SELECT 'وظایف سررسیدشده','یک یا چند وظیفه باز سررسید شده یا موعدش امروز است.','warning',? WHERE EXISTS(SELECT 1 FROM tasks WHERE status='باز' AND due_date IS NOT NULL AND date(due_date)<=date('now','localtime')) AND NOT EXISTS(SELECT 1 FROM notifications WHERE title='وظایف سررسیدشده' AND date(created_at)=date('now','localtime'))""",(now,))
    con.execute("""INSERT INTO notifications(title,body,level,created_at) SELECT 'حساب‌های بدهکار','برای یک یا چند مشتری یا تأمین‌کننده مانده بدهی وجود دارد.','info',? WHERE EXISTS(SELECT 1 FROM sales WHERE status='بدهکار') AND NOT EXISTS(SELECT 1 FROM notifications WHERE title='حساب‌های بدهکار' AND date(created_at)=date('now','localtime'))""",(now,))
    con.commit()
    rows=con.execute("SELECT * FROM notifications ORDER BY id DESC LIMIT 50").fetchall(); con.close()
    return render_template("notifications.html", rows=rows)

@app.route("/reports")
@login_required
def reports():
    con=db(); frm=request.args.get('from','').strip(); to=request.args.get('to','').strip()
    where='1=1'; args=[]
    if frm: where += " AND date(created_at)>=date(?)"; args.append(frm)
    if to: where += " AND date(created_at)<=date(?)"; args.append(to)
    report={
      'sales':con.execute(f"SELECT COALESCE(SUM(total),0)n FROM sales WHERE {where}",args).fetchone()['n'],
      'purchases':con.execute(f"SELECT COALESCE(SUM(total),0)n FROM purchases WHERE {where}",args).fetchone()['n'],
      'customers':con.execute("SELECT COUNT(*)n FROM customers").fetchone()['n'],
      'products':con.execute("SELECT COUNT(*)n FROM products").fetchone()['n'],
      'stock_value':con.execute("SELECT COALESCE(SUM(stock*buy_price),0)n FROM products").fetchone()['n'],
      'profit':con.execute(f"SELECT COALESCE(SUM(si.qty*(si.price-p.buy_price)),0)n FROM sale_items si JOIN sales s ON s.id=si.sale_id JOIN products p ON p.id=si.product_id WHERE {where.replace('created_at','s.created_at')}",args).fetchone()['n'],
    }
    daily=con.execute(f"SELECT substr(created_at,1,10) d,COALESCE(SUM(total),0) total FROM sales WHERE {where} GROUP BY d ORDER BY d DESC LIMIT 14",args).fetchall()
    top=con.execute(f"SELECT p.name,COALESCE(SUM(si.qty),0) qty,COALESCE(SUM(si.qty*(si.price-p.buy_price)),0) profit FROM sale_items si JOIN sales s ON s.id=si.sale_id JOIN products p ON p.id=si.product_id WHERE {where.replace('created_at','s.created_at')} GROUP BY p.id ORDER BY profit DESC LIMIT 8",args).fetchall()
    
    daily_rev=list(reversed(daily)); mx=max([float(x['total'] or 0) for x in daily_rev] or [1]); daily_chart=[{'d':x['d'],'total':x['total'],'height':max(8,round(float(x['total'] or 0)/mx*90))} for x in daily_rev]
    con.close(); return render_template('reports.html',report=report,daily=daily_rev,daily_chart=daily_chart,top=top,frm=frm,to=to)

@app.route("/users")
@login_required
@admin_required
def users():
    con=db(); rows=con.execute("SELECT id,first_name,last_name,username,role,created_at FROM users ORDER BY id").fetchall()
    con.close(); return render_template("users.html", rows=rows)

@app.route("/users/add", methods=["POST"])
@login_required
@admin_required
def add_user():
    f=request.form; first=safe_text(f.get("first_name"),80); last=safe_text(f.get("last_name"),80)
    username=safe_text(f.get("username"),80); password=f.get("password",""); role=safe_text(f.get("role","فروشنده"),40)
    if not first or not username or len(password)<4:
        flash("نام، نام کاربری و رمز حداقل ۴ رقمی الزامی است.","error"); return redirect(url_for("users"))
    try:
        con=db(); con.execute("INSERT INTO users(first_name,last_name,username,password,role,created_at) VALUES(?,?,?,?,?,?)",(first,last,username,generate_password_hash(password),role,datetime.now().isoformat())); con.commit(); con.close()
        log_action(f"کاربر «{username}» ساخته شد"); flash("کاربر جدید ساخته شد.","success")
    except sqlite3.IntegrityError: flash("این نام کاربری قبلاً ثبت شده است.","error")
    return redirect(url_for("users"))

@app.route("/users/delete/<int:id>", methods=["POST"])
@login_required
@admin_required
def delete_user(id):
    if id==session.get("user_id"):
        flash("نمی‌توانید حساب خودتان را حذف کنید.","error"); return redirect(url_for("users"))
    con=db(); u=con.execute("SELECT username FROM users WHERE id=?",(id,)).fetchone(); con.execute("DELETE FROM users WHERE id=?",(id,)); con.commit(); con.close()
    if u: log_action(f"کاربر «{u['username']}» حذف شد"); flash("کاربر حذف شد.","success")
    return redirect(url_for("users"))

@app.route("/users/role/<int:id>", methods=["POST"])
@login_required
@admin_required
def change_user_role(id):
    role=safe_text(request.form.get("role","فروشنده"),40); con=db(); con.execute("UPDATE users SET role=? WHERE id=?",(role,id)); con.commit(); con.close(); log_action("نقش کاربر تغییر کرد"); flash("نقش کاربر به‌روزرسانی شد.","success"); return redirect(url_for("users"))

@app.route("/business")
@login_required
@admin_required
def business():
    return render_template("business.html")

@app.route("/settings", methods=["GET","POST"])
@login_required
@admin_required
def settings():
    if request.method=="POST":
        flash("تنظیمات ذخیره شد.","success")
    return render_template("settings.html")

@app.route("/license", methods=["GET","POST"])
@login_required
@admin_required
def license_page():
    if request.method == "POST":
        code = request.form.get("code", "").strip()
        plan = verify_license_code(code)
        if plan:
            setting_set("license_plan", plan)
            setting_set("license_code", code)
            setting_set("license_activated_at", datetime.now().isoformat())
            log_action("لایسنس Pro فعال شد")
            flash("لایسنس با موفقیت فعال شد. ویژگی‌های Pro باز شدند.", "success")
        else:
            flash("کد لایسنس نامعتبر است.", "error")
        return redirect(url_for("license_page"))
    return render_template("license.html", is_pro=is_pro(), plan=setting_get("license_plan",""),
                            activated_at=setting_get("license_activated_at",""), pro_features=sorted(PRO_FEATURES))

@app.route("/activity")
@login_required
def activity():
    con=db(); rows=con.execute("""SELECT a.*,COALESCE(u.first_name||' '||u.last_name,'سیستم') user_name
                                  FROM activities a LEFT JOIN users u ON u.id=a.user_id ORDER BY a.id DESC LIMIT 100""").fetchall()
    con.close(); return render_template("activity.html", rows=rows)

@app.route("/api/dashboard")
@login_required
def dashboard_api():
    return jsonify(stats())

@app.route("/products/adjust/<int:id>", methods=["POST"])
@login_required
def adjust_stock(id):
    f=request.form
    try: change=float(f.get("change") or 0)
    except ValueError: change=0
    reason=(f.get("reason") or "اصلاح دستی").strip()[:120]
    con=db(); p=con.execute("SELECT * FROM products WHERE id=?",(id,)).fetchone()
    if not p: con.close(); flash("محصول پیدا نشد.","error"); return redirect(url_for("products"))
    new_stock=p["stock"]+change
    if new_stock<0:
        con.close(); flash("موجودی نمی‌تواند منفی شود.","error"); return redirect(url_for("products"))
    con.execute("UPDATE products SET stock=? WHERE id=?",(new_stock,id))
    con.execute("INSERT INTO stock_movements(product_id,change,reason,user_id,created_at) VALUES(?,?,?,?,?)",(id,change,reason,session.get("user_id"),datetime.now().isoformat()))
    con.commit(); con.close(); log_action(f"موجودی «{p['name']}» به میزان {change:g} تغییر کرد"); flash("موجودی با موفقیت اصلاح شد.","success"); return redirect(url_for("products"))

@app.route("/stock-history")
@login_required
def stock_history():
    con=db(); rows=con.execute("""SELECT m.*,p.name product,COALESCE(u.first_name||' '||u.last_name,'سیستم') user_name
        FROM stock_movements m JOIN products p ON p.id=m.product_id LEFT JOIN users u ON u.id=m.user_id
        ORDER BY m.id DESC LIMIT 200""").fetchall(); con.close()
    return render_template("stock_history.html", rows=rows)

@app.route("/notifications/read-all", methods=["POST"])
@login_required
def notifications_read_all():
    con=db(); con.execute("UPDATE notifications SET is_read=1 WHERE is_read=0"); con.commit(); con.close()
    return redirect(url_for("notifications"))

@app.route("/api/search")
@login_required
def global_search():
    q=request.args.get("q","").strip()
    if len(q)<2: return jsonify([])
    like=f"%{q}%"; con=db(); out=[]
    for r in con.execute("SELECT id,name FROM customers WHERE name LIKE ? OR phone LIKE ? ORDER BY id DESC LIMIT 5",(like,like)):
        out.append({"type":"مشتری","name":r["name"],"url":url_for("customers",q=q)})
    for r in con.execute("SELECT id,name FROM products WHERE name LIKE ? OR code LIKE ? OR barcode LIKE ? ORDER BY id DESC LIMIT 5",(like,like,like)):
        out.append({"type":"محصول","name":r["name"],"url":url_for("products",q=q)})
    con.close(); return jsonify(out[:10])

@app.route("/export/<entity>")
@login_required
@pro_required
def export_csv(entity):
    con=db()
    configs={
      "customers":("مشتریان", "SELECT id,name,phone,address,notes,created_at FROM customers", ["شناسه","نام","تلفن","آدرس","یادداشت","تاریخ"]),
      "products":("محصولات", "SELECT id,name,code,barcode,category,brand,buy_price,sell_price,stock,min_stock,location FROM products", ["شناسه","نام","کد","بارکد","دسته","برند","خرید","فروش","موجودی","حداقل","محل"]),
      "sales":("فروش", "SELECT s.id,COALESCE(c.name,'مشتری متفرقه'),s.total,s.discount,s.paid,s.payment_method,s.status,s.created_at FROM sales s LEFT JOIN customers c ON c.id=s.customer_id", ["شناسه","مشتری","مبلغ","تخفیف","پرداخت","روش پرداخت","وضعیت","تاریخ"]),
      "finance":("تراکنش مالی", "SELECT id,kind,category,title,amount,note,created_at FROM transactions", ["شناسه","نوع","دسته","عنوان","مبلغ","یادداشت","تاریخ"]),
    }
    if entity not in configs:
        con.close(); return "نوع خروجی نامعتبر است",400
    title,query,headers=configs[entity]; rows=con.execute(query).fetchall(); con.close()
    buf=io.StringIO(); w=csv.writer(buf); w.writerow(headers)
    for r in rows: w.writerow(list(r))
    data='\ufeff'+buf.getvalue()
    return Response(data,mimetype="text/csv; charset=utf-8",headers={"Content-Disposition":f"attachment; filename=dijo_{entity}.csv"})

@app.route("/restore", methods=["POST"])
@login_required
@admin_required
@pro_required
def restore():
    upload=request.files.get('backup_file')
    if not upload or not upload.filename.lower().endswith('.db'):
        flash('فقط فایل دیتابیس با پسوند .db قابل بازیابی است.','error'); return redirect(url_for('settings'))
    temp=os.path.join(BASE,'restore_candidate.db'); upload.save(temp)
    try:
        test=sqlite3.connect(temp); ok=test.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='users'").fetchone(); test.close()
        if not ok: raise ValueError('ساختار دیتابیس دیجو معتبر نیست')
        stamp=datetime.now().strftime('%Y%m%d_%H%M%S'); shutil.copy2(DB,os.path.join(BASE,f'dijo_before_restore_{stamp}.db'))
        shutil.move(temp,DB); init_db(); log_action('بازیابی دیتابیس انجام شد'); flash('دیتابیس با موفقیت بازیابی شد.','success')
    except Exception as e:
        if os.path.exists(temp): os.remove(temp)
        flash('بازیابی ناموفق بود: '+str(e),'error')
    return redirect(url_for('settings'))

@app.route("/backup")
@login_required
@admin_required
@pro_required
def backup():
    if not os.path.exists(DB): init_db()
    stamp=datetime.now().strftime("%Y%m%d_%H%M%S")
    target=os.path.join(BASE,f"dijo_backup_{stamp}.db")
    shutil.copy2(DB,target); log_action("پشتیبان از دیتابیس ساخته شد")
    return send_file(target,as_attachment=True,download_name=f"dijo_backup_{stamp}.db")


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), debug=False)
