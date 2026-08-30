# ============================================================
# DIJO BUSINESS DATABASE v2
# SQLite - سازگار با business.db فعلی
# ============================================================

import sqlite3
import os
import shutil
import threading
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Master DB فقط برای حساب‌های کاربری/احراز هویت.
MASTER_DB_PATH = os.path.join(BASE_DIR, "business.db")

# برای جلوگیری از قاطی شدن داده کاربران، هر کاربر DB مستقل خودش را دارد.
# این مدل برای شروع SaaS سبک، روی Pydroid/VPS و بدون وابستگی سنگین مناسب است.
DB_PATH = MASTER_DB_PATH
_tenant = threading.local()


def set_current_user(user_id):
    """DB جاری درخواست را روی فضای اختصاصی کاربر تنظیم می‌کند."""
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        raise ValueError("شناسه کاربر نامعتبر است.")
    if uid <= 0:
        raise ValueError("شناسه کاربر نامعتبر است.")
    _tenant.user_id = uid
    ensure_user_db(uid)


def clear_current_user():
    if hasattr(_tenant, "user_id"):
        delattr(_tenant, "user_id")


def get_db_path(user_id=None):
    uid = user_id if user_id is not None else getattr(_tenant, "user_id", None)
    if uid is None:
        return MASTER_DB_PATH
    return os.path.join(BASE_DIR, f"business_user_{int(uid)}.db")


def ensure_user_db(user_id):
    path = get_db_path(user_id)
    if os.path.exists(path):
        return path

    # سازگاری با دیتای نسخه قدیمی: برای اولین کاربر، داده موجود را
    # به فضای اختصاصی او منتقل/کپی می‌کنیم؛ کاربران بعدی DB خالی می‌گیرند.
    if user_id == 1 and os.path.exists(MASTER_DB_PATH):
        shutil.copy2(MASTER_DB_PATH, path)
    else:
        # فایل خالی ساخته می‌شود و init_schema پایین آن را آماده می‌کند.
        sqlite3.connect(path).close()
    _init_business_schema(path)
    return path


def get_conn():
    db_path = get_db_path()
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def row_to_dict(row):
    return dict(row) if row else None


def rows_to_list(rows):
    return [dict(r) for r in rows]


def _ensure_column(conn, table, column, coltype):
    cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


def init_db():
    # همیشه ابتدا master DB را آماده می‌کنیم تا auth مستقل از tenant باشد.
    _init_business_schema(MASTER_DB_PATH)
    conn = sqlite3.connect(MASTER_DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    c = conn.cursor()

    c.execute("""CREATE TABLE IF NOT EXISTS customers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        phone TEXT,
        address TEXT,
        note TEXT,
        created_at TEXT DEFAULT (datetime('now','localtime'))
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        price REAL NOT NULL DEFAULT 0,
        cost_price REAL NOT NULL DEFAULT 0,
        stock REAL NOT NULL DEFAULT 0,
        note TEXT,
        created_at TEXT DEFAULT (datetime('now','localtime'))
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_id INTEGER,
        total REAL NOT NULL DEFAULT 0,
        status TEXT DEFAULT 'تکمیل‌شده',
        note TEXT,
        created_at TEXT DEFAULT (datetime('now','localtime')),
        FOREIGN KEY(customer_id) REFERENCES customers(id)
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS order_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER NOT NULL,
        product_id INTEGER,
        product_name TEXT,
        qty REAL NOT NULL DEFAULT 1,
        price REAL NOT NULL DEFAULT 0,
        cost_price REAL NOT NULL DEFAULT 0,
        FOREIGN KEY(order_id) REFERENCES orders(id),
        FOREIGN KEY(product_id) REFERENCES products(id)
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER,
        customer_id INTEGER NOT NULL,
        amount REAL NOT NULL,
        method TEXT DEFAULT 'نقدی',
        note TEXT,
        created_at TEXT DEFAULT (datetime('now','localtime')),
        FOREIGN KEY(order_id) REFERENCES orders(id),
        FOREIGN KEY(customer_id) REFERENCES customers(id)
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS customer_ledger (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_id INTEGER NOT NULL,
        kind TEXT NOT NULL,
        amount REAL NOT NULL,
        note TEXT,
        created_at TEXT DEFAULT (datetime('now','localtime')),
        FOREIGN KEY(customer_id) REFERENCES customers(id)
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS stock_movements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id INTEGER,
        product_name TEXT,
        movement_type TEXT NOT NULL,
        quantity REAL NOT NULL,
        reference_type TEXT,
        reference_id INTEGER,
        note TEXT,
        created_at TEXT DEFAULT (datetime('now','localtime')),
        FOREIGN KEY(product_id) REFERENCES products(id)
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        amount REAL NOT NULL,
        kind TEXT NOT NULL,
        category TEXT,
        created_at TEXT DEFAULT (datetime('now','localtime'))
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        due_date TEXT,
        done INTEGER NOT NULL DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now','localtime'))
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS suppliers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        phone TEXT,
        note TEXT,
        created_at TEXT DEFAULT (datetime('now','localtime'))
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS purchases (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        supplier_id INTEGER,
        total REAL NOT NULL DEFAULT 0,
        note TEXT,
        created_at TEXT DEFAULT (datetime('now','localtime')),
        FOREIGN KEY(supplier_id) REFERENCES suppliers(id)
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS purchase_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        purchase_id INTEGER NOT NULL,
        product_id INTEGER,
        product_name TEXT,
        qty REAL NOT NULL DEFAULT 1,
        cost_price REAL NOT NULL DEFAULT 0,
        FOREIGN KEY(purchase_id) REFERENCES purchases(id),
        FOREIGN KEY(product_id) REFERENCES products(id)
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        created_at TEXT DEFAULT (datetime('now','localtime'))
    )""")

    # v2 fields; old business.db remains usable.
    _ensure_column(conn, "products", "cost_price", "REAL NOT NULL DEFAULT 0")
    _ensure_column(conn, "order_items", "cost_price", "REAL NOT NULL DEFAULT 0")

    conn.commit()
    conn.close()


# ------------------------- CUSTOMERS -------------------------

def add_customer(name, phone=None, address=None, note=None):
    name = (name or "").strip()
    if not name:
        raise ValueError("نام مشتری الزامی است.")
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO customers(name,phone,address,note) VALUES(?,?,?,?)",
        (name, phone, address, note)
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id


def list_customers(query=None):
    conn = get_conn()
    base = """SELECT c.*,
        COALESCE((SELECT SUM(CASE WHEN kind='debt' THEN amount ELSE -amount END)
                  FROM customer_ledger WHERE customer_id=c.id),0) AS balance
        FROM customers c"""
    if query:
        q = f"%{query}%"
        rows = conn.execute(base + " WHERE c.name LIKE ? OR c.phone LIKE ? ORDER BY c.id DESC", (q,q)).fetchall()
    else:
        rows = conn.execute(base + " ORDER BY c.id DESC").fetchall()
    conn.close()
    return rows_to_list(rows)


def find_customer_by_name(name):
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM customers WHERE name LIKE ? ORDER BY id DESC LIMIT 1",
        (f"%{(name or '').strip()}%",)
    ).fetchone()
    conn.close()
    return row_to_dict(row)


def update_customer(customer_id, name=None, phone=None, address=None, note=None):
    conn = get_conn()
    old = conn.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
    if not old:
        conn.close()
        return False
    conn.execute("""UPDATE customers SET name=?,phone=?,address=?,note=? WHERE id=?""",
                 (name if name is not None else old["name"],
                  phone if phone is not None else old["phone"],
                  address if address is not None else old["address"],
                  note if note is not None else old["note"], customer_id))
    conn.commit()
    conn.close()
    return True


def delete_customer(customer_id):
    conn = get_conn()
    # Do not silently delete a customer with sales.
    sales = conn.execute("SELECT COUNT(*) c FROM orders WHERE customer_id=?", (customer_id,)).fetchone()["c"]
    if sales:
        conn.close()
        raise ValueError("این مشتری سابقه فروش دارد و برای حفظ سوابق قابل حذف نیست.")
    conn.execute("DELETE FROM customer_ledger WHERE customer_id=?", (customer_id,))
    conn.execute("DELETE FROM payments WHERE customer_id=?", (customer_id,))
    conn.execute("DELETE FROM customers WHERE id=?", (customer_id,))
    conn.commit()
    conn.close()


# ------------------------- CUSTOMER LEDGER -------------------------

def add_customer_debt(customer_id, amount, note=None):
    amount = float(amount)
    if amount <= 0:
        raise ValueError("مبلغ بدهی باید بیشتر از صفر باشد.")
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO customer_ledger(customer_id,kind,amount,note) VALUES(?,'debt',?,?)",
        (customer_id, amount, note))
    conn.commit()
    lid = cur.lastrowid
    conn.close()
    return lid


def add_customer_payment(customer_id, amount, note=None):
    amount = float(amount)
    if amount <= 0:
        raise ValueError("مبلغ پرداخت باید بیشتر از صفر باشد.")
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO customer_ledger(customer_id,kind,amount,note) VALUES(?,'payment',?,?)",
        (customer_id, amount, note))
    conn.commit()
    lid = cur.lastrowid
    conn.close()
    return lid


def get_customer_balance(customer_id):
    conn = get_conn()
    row = conn.execute("""SELECT COALESCE(SUM(
        CASE WHEN kind='debt' THEN amount ELSE -amount END),0) balance
        FROM customer_ledger WHERE customer_id=?""", (customer_id,)).fetchone()
    conn.close()
    return row["balance"]


def customer_ledger_history(customer_id, limit=50):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM customer_ledger WHERE customer_id=? ORDER BY id DESC LIMIT ?",
        (customer_id, limit)).fetchall()
    conn.close()
    return rows_to_list(rows)


def delete_ledger_entry(entry_id):
    conn = get_conn()
    conn.execute("DELETE FROM customer_ledger WHERE id=?", (entry_id,))
    conn.commit()
    conn.close()


def list_customer_balances(min_abs=0):
    conn = get_conn()
    rows = conn.execute("""SELECT c.id customer_id,c.name customer_name,c.phone,
        COALESCE(SUM(CASE WHEN l.kind='debt' THEN l.amount ELSE -l.amount END),0) balance,
        MAX(l.created_at) last_activity
        FROM customers c LEFT JOIN customer_ledger l ON l.customer_id=c.id
        GROUP BY c.id HAVING ABS(balance)>? ORDER BY balance DESC""", (min_abs,)).fetchall()
    conn.close()
    return rows_to_list(rows)


def list_old_debts(days=30):
    cutoff = (datetime.now()-timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_conn()
    rows = conn.execute("""SELECT c.id customer_id,c.name customer_name,c.phone,
        COALESCE(SUM(CASE WHEN l.kind='debt' THEN l.amount ELSE -l.amount END),0) balance,
        MAX(l.created_at) last_activity
        FROM customers c JOIN customer_ledger l ON l.customer_id=c.id
        GROUP BY c.id HAVING balance>0 AND last_activity<=?
        ORDER BY last_activity ASC""", (cutoff,)).fetchall()
    conn.close()
    return rows_to_list(rows)


def total_outstanding_debt():
    return sum(max(0,float(r["balance"])) for r in list_customer_balances(0))


# ------------------------- PRODUCTS -------------------------

def add_product(name, price=0, stock=0, note=None, cost_price=0):
    name = (name or "").strip()
    price = float(price or 0)
    stock = float(stock or 0)
    cost_price = float(cost_price or 0)
    if not name:
        raise ValueError("نام محصول الزامی است.")
    if price < 0 or stock < 0 or cost_price < 0:
        raise ValueError("قیمت و موجودی نمی‌توانند منفی باشند.")
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO products(name,price,stock,note,cost_price) VALUES(?,?,?,?,?)",
        (name,price,stock,note,cost_price))
    pid = cur.lastrowid
    if stock:
        conn.execute("""INSERT INTO stock_movements
            (product_id,product_name,movement_type,quantity,reference_type,reference_id,note)
            VALUES(?,?,?,?,?,?,?)""",
            (pid,name,"initial",stock,"product",pid,"موجودی اولیه"))
    conn.commit()
    conn.close()
    return pid


def list_products(query=None):
    conn = get_conn()
    if query:
        rows = conn.execute("SELECT * FROM products WHERE name LIKE ? ORDER BY id DESC",
                            (f"%{query}%",)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM products ORDER BY id DESC").fetchall()
    conn.close()
    return rows_to_list(rows)


def find_product_by_name(name):
    conn = get_conn()
    row = conn.execute("SELECT * FROM products WHERE name LIKE ? ORDER BY id DESC LIMIT 1",
                       (f"%{(name or '').strip()}%",)).fetchone()
    conn.close()
    return row_to_dict(row)


def update_product(product_id, name=None, price=None, cost_price=None, stock=None, note=None):
    conn = get_conn()
    old = conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    if not old:
        conn.close()
        return False
    old_stock = float(old["stock"] or 0)
    new_stock = old_stock if stock is None else float(stock)
    if new_stock < 0:
        conn.close()
        raise ValueError("موجودی نمی‌تواند منفی باشد.")
    conn.execute("""UPDATE products SET name=?,price=?,cost_price=?,stock=?,note=? WHERE id=?""",
                 (name if name is not None else old["name"],
                  float(price) if price is not None else old["price"],
                  float(cost_price) if cost_price is not None else old["cost_price"],
                  new_stock,
                  note if note is not None else old["note"], product_id))
    if new_stock != old_stock:
        conn.execute("""INSERT INTO stock_movements
            (product_id,product_name,movement_type,quantity,reference_type,reference_id,note)
            VALUES(?,?,?,?,?,?,?)""",
            (product_id, old["name"], "adjustment", new_stock-old_stock,
             "manual", product_id, "اصلاح دستی موجودی"))
    conn.commit()
    conn.close()
    return True


def update_stock(product_id, change, movement_type="adjustment", reference_type=None,
                 reference_id=None, note=None, conn=None):
    own = conn is None
    conn = conn or get_conn()
    change = float(change)
    row = conn.execute("SELECT id,name,stock FROM products WHERE id=?", (product_id,)).fetchone()
    if not row:
        if own: conn.close()
        raise ValueError("محصول پیدا نشد.")
    new_stock = float(row["stock"] or 0) + change
    if new_stock < 0:
        if own: conn.close()
        raise ValueError(f"موجودی «{row['name']}» کافی نیست.")
    conn.execute("UPDATE products SET stock=? WHERE id=?", (new_stock,product_id))
    conn.execute("""INSERT INTO stock_movements
        (product_id,product_name,movement_type,quantity,reference_type,reference_id,note)
        VALUES(?,?,?,?,?,?,?)""",
        (product_id,row["name"],movement_type,change,reference_type,reference_id,note))
    if own:
        conn.commit(); conn.close()
    return new_stock


def delete_product(product_id):
    conn = get_conn()
    used = conn.execute("SELECT COUNT(*) c FROM order_items WHERE product_id=?", (product_id,)).fetchone()["c"]
    if used:
        conn.close()
        raise ValueError("این محصول سابقه فروش دارد و برای حفظ گزارش سود قابل حذف نیست.")
    conn.execute("DELETE FROM stock_movements WHERE product_id=?", (product_id,))
    conn.execute("DELETE FROM products WHERE id=?", (product_id,))
    conn.commit()
    conn.close()


def low_stock_products(threshold=5):
    conn = get_conn()
    rows = conn.execute("SELECT * FROM products WHERE stock<=? ORDER BY stock ASC", (threshold,)).fetchall()
    conn.close()
    return rows_to_list(rows)


def stock_history(product_id, limit=100):
    conn = get_conn()
    rows = conn.execute("SELECT * FROM stock_movements WHERE product_id=? ORDER BY id DESC LIMIT ?",
                        (product_id,limit)).fetchall()
    conn.close()
    return rows_to_list(rows)


# ------------------------- SUPPLIERS / PURCHASES -------------------------

def add_supplier(name, phone=None, note=None):
    name=(name or "").strip()
    if not name: raise ValueError("نام تأمین‌کننده الزامی است.")
    conn=get_conn()
    cur=conn.execute("INSERT INTO suppliers(name,phone,note) VALUES(?,?,?)",(name,phone,note))
    conn.commit(); sid=cur.lastrowid; conn.close()
    return sid


def list_suppliers(query=None):
    conn=get_conn()
    if query:
        rows=conn.execute("SELECT * FROM suppliers WHERE name LIKE ? ORDER BY id DESC",(f"%{query}%",)).fetchall()
    else:
        rows=conn.execute("SELECT * FROM suppliers ORDER BY id DESC").fetchall()
    conn.close(); return rows_to_list(rows)


def find_supplier_by_name(name):
    conn=get_conn()
    row=conn.execute("SELECT * FROM suppliers WHERE name LIKE ? ORDER BY id DESC LIMIT 1",
                     (f"%{(name or '').strip()}%",)).fetchone()
    conn.close(); return row_to_dict(row)


def delete_supplier(supplier_id):
    conn=get_conn()
    used=conn.execute("SELECT COUNT(*) c FROM purchases WHERE supplier_id=?",(supplier_id,)).fetchone()["c"]
    if used:
        conn.close(); raise ValueError("این تأمین‌کننده سابقه خرید دارد و قابل حذف نیست.")
    conn.execute("DELETE FROM suppliers WHERE id=?",(supplier_id,))
    conn.commit(); conn.close()


def add_purchase(supplier_id, items, note=None):
    if not items: raise ValueError("حداقل یک قلم برای خرید لازم است.")
    conn=get_conn()
    try:
        conn.execute("BEGIN")
        total=sum(float(i["qty"])*float(i["cost_price"]) for i in items)
        cur=conn.execute("INSERT INTO purchases(supplier_id,total,note) VALUES(?,?,?)",
                         (supplier_id,total,note))
        purchase_id=cur.lastrowid
        for i in items:
            qty=float(i["qty"]); cost=float(i["cost_price"])
            if qty<=0 or cost<0: raise ValueError("تعداد/قیمت خرید نامعتبر است.")
            pid=i.get("product_id")
            if pid:
                row=conn.execute("SELECT stock,cost_price,name FROM products WHERE id=?",(pid,)).fetchone()
                if not row: raise ValueError("محصول خرید پیدا نشد.")
                old_stock=float(row["stock"] or 0); old_cost=float(row["cost_price"] or 0)
                total_qty=old_stock+qty
                weighted=((old_stock*old_cost)+(qty*cost))/total_qty if total_qty else cost
                conn.execute("UPDATE products SET stock=stock+?,cost_price=? WHERE id=?",
                             (qty,weighted,pid))
                conn.execute("""INSERT INTO stock_movements
                    (product_id,product_name,movement_type,quantity,reference_type,reference_id,note)
                    VALUES(?,?,?,?,?,?,?)""",
                    (pid,row["name"],"purchase",qty,"purchase",purchase_id,note))
            conn.execute("""INSERT INTO purchase_items
                (purchase_id,product_id,product_name,qty,cost_price) VALUES(?,?,?,?,?)""",
                (purchase_id,pid,i.get("product_name"),qty,cost))
        conn.commit()
        return purchase_id,total
    except:
        conn.rollback(); raise
    finally:
        conn.close()


def list_purchases(limit=20):
    conn=get_conn()
    rows=conn.execute("""SELECT p.*,s.name supplier_name FROM purchases p
        LEFT JOIN suppliers s ON s.id=p.supplier_id ORDER BY p.id DESC LIMIT ?""",(limit,)).fetchall()
    conn.close(); return rows_to_list(rows)


def purchase_items_for(purchase_id):
    conn=get_conn()
    rows=conn.execute("SELECT * FROM purchase_items WHERE purchase_id=?",(purchase_id,)).fetchall()
    conn.close(); return rows_to_list(rows)


def delete_purchase(purchase_id):
    raise ValueError("حذف خرید در v2 غیرفعال است؛ برای جلوگیری از خراب شدن موجودی از عملیات برگشت خرید استفاده می‌کنیم.")


# ------------------------- ORDERS / PAYMENTS -------------------------

def add_order(customer_id, items, note=None, status="تکمیل‌شده",
              paid_amount=0, payment_method="نقدی", payment_note=None):
    if not customer_id: raise ValueError("شناسه مشتری مشخص نیست.")
    if not items: raise ValueError("حداقل یک قلم برای فروش لازم است.")
    paid=float(paid_amount or 0)
    if paid < 0: raise ValueError("مبلغ پرداختی نمی‌تواند منفی باشد.")

    conn=get_conn()
    try:
        conn.execute("BEGIN")

        validated=[]
        total=0.0

        # Validate everything before writing the order.
        for item in items:
            pid=item.get("product_id")
            qty=float(item.get("qty",0))
            price=float(item.get("price",0))
            cost=float(item.get("cost_price",0) or 0)
            if qty<=0: raise ValueError("تعداد کالا باید بیشتر از صفر باشد.")
            if price<0 or cost<0: raise ValueError("قیمت نمی‌تواند منفی باشد.")

            name=item.get("product_name") or ""
            if pid:
                row=conn.execute("SELECT id,name,stock,cost_price,price FROM products WHERE id=?",(pid,)).fetchone()
                if not row: raise ValueError(f"محصول با شناسه {pid} پیدا نشد.")
                if float(row["stock"] or 0) < qty:
                    raise ValueError(f"موجودی «{row['name']}» کافی نیست. موجودی: {row['stock']}، درخواست: {qty}")
                name=row["name"]
                if cost<=0: cost=float(row["cost_price"] or 0)
            elif not name:
                raise ValueError("نام محصول مشخص نیست.")

            total += qty*price
            validated.append((pid,name,qty,price,cost))

        if paid > total:
            raise ValueError("مبلغ پرداختی نمی‌تواند از مبلغ کل فروش بیشتر باشد.")

        cur=conn.execute("INSERT INTO orders(customer_id,total,status,note) VALUES(?,?,?,?)",
                         (customer_id,total,status,note))
        order_id=cur.lastrowid

        for pid,name,qty,price,cost in validated:
            conn.execute("""INSERT INTO order_items
                (order_id,product_id,product_name,qty,price,cost_price)
                VALUES(?,?,?,?,?,?)""",(order_id,pid,name,qty,price,cost))
            if pid:
                cur2=conn.execute("""UPDATE products SET stock=stock-?
                    WHERE id=? AND stock>=?""",(qty,pid,qty))
                if cur2.rowcount != 1:
                    raise ValueError(f"موجودی «{name}» در لحظه ثبت کافی نبود.")
                conn.execute("""INSERT INTO stock_movements
                    (product_id,product_name,movement_type,quantity,reference_type,reference_id,note)
                    VALUES(?,?,?,?,?,?,?)""",
                    (pid,name,"sale",-qty,"order",order_id,note))

        remaining=total-paid

        if paid>0:
            conn.execute("""INSERT INTO payments
                (order_id,customer_id,amount,method,note) VALUES(?,?,?,?,?)""",
                (order_id,customer_id,paid,payment_method or "نقدی",payment_note))
            conn.execute("""INSERT INTO customer_ledger
                (customer_id,kind,amount,note) VALUES(?,'payment',?,?)""",
                (customer_id,paid,payment_note or f"پرداخت بابت فروش #{order_id}"))

        if remaining>0:
            conn.execute("""INSERT INTO customer_ledger
                (customer_id,kind,amount,note) VALUES(?,'debt',?,?)""",
                (customer_id,remaining,f"بدهی بابت فروش #{order_id}"))

        conn.commit()
        return {"order_id":order_id,"total":total,"paid_amount":paid,"remaining":remaining}
    except:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_orders(limit=20):
    conn=get_conn()
    rows=conn.execute("""SELECT o.*,c.name customer_name,
        COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.order_id=o.id),0) paid_amount
        FROM orders o LEFT JOIN customers c ON c.id=o.customer_id
        ORDER BY o.id DESC LIMIT ?""",(limit,)).fetchall()
    conn.close()
    result=rows_to_list(rows)
    for r in result:
        r["remaining"]=max(0,float(r["total"] or 0)-float(r["paid_amount"] or 0))
    return result


def order_items_for(order_id):
    conn=get_conn()
    rows=conn.execute("SELECT * FROM order_items WHERE order_id=?",(order_id,)).fetchall()
    conn.close(); return rows_to_list(rows)


def get_order_details(order_id):
    conn=get_conn()
    order=conn.execute("""SELECT o.*,c.name customer_name,c.phone customer_phone
        FROM orders o LEFT JOIN customers c ON c.id=o.customer_id WHERE o.id=?""",(order_id,)).fetchone()
    if not order:
        conn.close(); return None
    items=conn.execute("SELECT * FROM order_items WHERE order_id=? ORDER BY id",(order_id,)).fetchall()
    payments=conn.execute("SELECT * FROM payments WHERE order_id=? ORDER BY id",(order_id,)).fetchall()
    conn.close()
    result=dict(order)
    result["items"]=rows_to_list(items)
    result["payments"]=rows_to_list(payments)
    result["paid_amount"]=sum(float(x["amount"] or 0) for x in result["payments"])
    result["remaining"]=max(0,float(result["total"] or 0)-result["paid_amount"])
    return result


def add_order_payment(order_id, amount, method="نقدی", note=None):
    amount=float(amount)
    if amount<=0: raise ValueError("مبلغ پرداخت باید بیشتر از صفر باشد.")
    conn=get_conn()
    try:
        conn.execute("BEGIN")
        order=conn.execute("SELECT id,customer_id,total,status FROM orders WHERE id=?",(order_id,)).fetchone()
        if not order: raise ValueError("سفارش پیدا نشد.")
        paid=conn.execute("SELECT COALESCE(SUM(amount),0) x FROM payments WHERE order_id=?",(order_id,)).fetchone()["x"]
        remaining=float(order["total"])-float(paid)
        if amount>remaining: raise ValueError("پرداخت بیشتر از مانده سفارش است.")
        conn.execute("INSERT INTO payments(order_id,customer_id,amount,method,note) VALUES(?,?,?,?,?)",
                     (order_id,order["customer_id"],amount,method or "نقدی",note))
        conn.execute("INSERT INTO customer_ledger(customer_id,kind,amount,note) VALUES(?,'payment',?,?)",
                     (order["customer_id"],amount,note or f"پرداخت بابت فروش #{order_id}"))
        conn.commit()
        return {"order_id":order_id,"paid_amount":float(paid)+amount,"remaining":remaining-amount}
    except:
        conn.rollback(); raise
    finally:
        conn.close()


def cancel_order(order_id, reason=None):
    conn=get_conn()
    try:
        conn.execute("BEGIN")
        order=conn.execute("SELECT * FROM orders WHERE id=?",(order_id,)).fetchone()
        if not order: raise ValueError("سفارش پیدا نشد.")
        if order["status"]=="لغوشده": raise ValueError("این سفارش قبلاً لغو شده است.")
        items=conn.execute("SELECT * FROM order_items WHERE order_id=?",(order_id,)).fetchall()

        # Restore stock.
        for item in items:
            if item["product_id"]:
                conn.execute("UPDATE products SET stock=stock+? WHERE id=?",
                             (item["qty"],item["product_id"]))
                conn.execute("""INSERT INTO stock_movements
                    (product_id,product_name,movement_type,quantity,reference_type,reference_id,note)
                    VALUES(?,?,?,?,?,?,?)""",
                    (item["product_id"],item["product_name"],"sale_return",item["qty"],
                     "order_cancel",order_id,reason or "لغو فروش"))

        # Reverse the debt created by this order.
        debt_note=f"بدهی بابت فروش #{order_id}"
        debt_rows=conn.execute("""SELECT id,amount FROM customer_ledger
            WHERE customer_id=? AND kind='debt' AND note=?""",(order["customer_id"],debt_note)).fetchall()
        for row in debt_rows:
            conn.execute("INSERT INTO customer_ledger(customer_id,kind,amount,note) VALUES(?,'payment',?,?)",
                         (order["customer_id"],row["amount"],f"برگشت بدهی فروش #{order_id}"))

        # Record cancellation; existing payments are retained as history.
        conn.execute("UPDATE orders SET status='لغوشده',note=? WHERE id=?",
                     ((order["note"] or "") + (f" | لغو: {reason}" if reason else ""),order_id))
        conn.commit()
        return True
    except:
        conn.rollback(); raise
    finally:
        conn.close()


def delete_order(order_id):
    raise ValueError("حذف مستقیم فروش در v2 غیرفعال است؛ از «لغو فروش» استفاده کنید.")


# ------------------------- TRANSACTIONS -------------------------

def add_transaction(title, amount, kind, category=None):
    if kind not in ("income","expense"): raise ValueError("نوع تراکنش نامعتبر است.")
    amount=float(amount)
    if amount<=0: raise ValueError("مبلغ باید بیشتر از صفر باشد.")
    conn=get_conn()
    cur=conn.execute("INSERT INTO transactions(title,amount,kind,category) VALUES(?,?,?,?)",
                     (title,amount,kind,category))
    conn.commit(); tid=cur.lastrowid; conn.close(); return tid


def list_transactions(limit=50):
    conn=get_conn()
    rows=conn.execute("SELECT * FROM transactions ORDER BY id DESC LIMIT ?",(limit,)).fetchall()
    conn.close(); return rows_to_list(rows)


def delete_transaction(t_id):
    conn=get_conn(); conn.execute("DELETE FROM transactions WHERE id=?",(t_id,))
    conn.commit(); conn.close()


# ------------------------- TASKS -------------------------

def add_task(title,due_date=None):
    if not (title or "").strip(): raise ValueError("عنوان وظیفه الزامی است.")
    conn=get_conn(); cur=conn.execute("INSERT INTO tasks(title,due_date) VALUES(?,?)",(title.strip(),due_date))
    conn.commit(); tid=cur.lastrowid; conn.close(); return tid


def list_tasks(only_open=False):
    conn=get_conn()
    if only_open: rows=conn.execute("SELECT * FROM tasks WHERE done=0 ORDER BY id DESC").fetchall()
    else: rows=conn.execute("SELECT * FROM tasks ORDER BY id DESC").fetchall()
    conn.close(); return rows_to_list(rows)


def set_task_done(task_id,done=True):
    conn=get_conn(); conn.execute("UPDATE tasks SET done=? WHERE id=?",(1 if done else 0,task_id))
    conn.commit(); conn.close()


def delete_task(task_id):
    conn=get_conn(); conn.execute("DELETE FROM tasks WHERE id=?",(task_id,))
    conn.commit(); conn.close()


# ------------------------- REPORTS -------------------------

def _cogs_since(since_iso):
    conn=get_conn()
    row=conn.execute("""SELECT COALESCE(SUM(oi.qty*oi.cost_price),0) total
        FROM order_items oi JOIN orders o ON o.id=oi.order_id
        WHERE o.created_at>=? AND o.status!='لغوشده'""",(since_iso,)).fetchone()
    conn.close(); return float(row["total"] or 0)


def product_profit_report(days=30,limit=10):
    since=(datetime.now()-timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    conn=get_conn()
    rows=conn.execute("""SELECT oi.product_name product_name,SUM(oi.qty) qty_sold,
        SUM(oi.qty*oi.price) revenue,SUM(oi.qty*oi.cost_price) cost,
        SUM(oi.qty*(oi.price-oi.cost_price)) profit
        FROM order_items oi JOIN orders o ON o.id=oi.order_id
        WHERE o.created_at>=? AND o.status!='لغوشده'
        GROUP BY oi.product_name ORDER BY profit DESC LIMIT ?""",(since,limit)).fetchall()
    conn.close(); return rows_to_list(rows)


def get_summary():
    now=datetime.now()
    today=now.strftime("%Y-%m-%d 00:00:00")
    week=(now-timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    month=(now-timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    conn=get_conn()
    def s(sql,args): return float(conn.execute(sql,args).fetchone()["x"] or 0)
    sales_today=s("SELECT COALESCE(SUM(total),0) x FROM orders WHERE created_at>=? AND status!='لغوشده'",(today,))
    sales_week=s("SELECT COALESCE(SUM(total),0) x FROM orders WHERE created_at>=? AND status!='لغوشده'",(week,))
    sales_month=s("SELECT COALESCE(SUM(total),0) x FROM orders WHERE created_at>=? AND status!='لغوشده'",(month,))
    income=s("SELECT COALESCE(SUM(amount),0) x FROM transactions WHERE created_at>=? AND kind='income'",(month,))
    expense=s("SELECT COALESCE(SUM(amount),0) x FROM transactions WHERE created_at>=? AND kind='expense'",(month,))
    customers_count=conn.execute("SELECT COUNT(*) c FROM customers").fetchone()["c"]
    products_count=conn.execute("SELECT COUNT(*) c FROM products").fetchone()["c"]
    orders_count=conn.execute("SELECT COUNT(*) c FROM orders WHERE status!='لغوشده'").fetchone()["c"]
    trend=[]
    for i in range(6,-1,-1):
        d=(now-timedelta(days=i)).strftime("%Y-%m-%d")
        row=conn.execute("SELECT COALESCE(SUM(total),0) x FROM orders WHERE date(created_at)=? AND status!='لغوشده'",(d,)).fetchone()
        trend.append({"date":d,"total":float(row["x"] or 0)})
    conn.close()
    cogs=_cogs_since(month)
    debtors=[r for r in list_customer_balances(0) if r["balance"]>0]
    open_tasks=list_tasks(True)
    old=list_old_debts(30)
    return {
        "sales_today":sales_today,"sales_week":sales_week,"sales_month":sales_month,
        "income_month":income,"expense_month":expense,"cogs_month":cogs,
        "profit_month":sales_month-cogs+income-expense,
        "top_products_month":product_profit_report(30,5),
        "low_stock":low_stock_products(5),"open_tasks_count":len(open_tasks),
        "open_tasks":open_tasks[:5],"customers_count":customers_count,
        "products_count":products_count,"orders_count":orders_count,
        "trend_7d":trend,"total_debt":total_outstanding_debt(),
        "debtors_count":len(debtors),"top_debtors":debtors[:5],
        "old_debts_count":len(old),"old_debts":old[:5]
    }


# ------------------------- USERS -------------------------

def user_count():
    conn=get_conn(); n=conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]; conn.close(); return n


def add_user(username,password_hash):
    conn=get_conn(); cur=conn.execute("INSERT INTO users(username,password_hash) VALUES(?,?)",(username,password_hash))
    conn.commit(); uid=cur.lastrowid; conn.close(); return uid


def get_user_by_username(username):
    conn=get_conn(); row=conn.execute("SELECT * FROM users WHERE username=?",(username,)).fetchone()
    conn.close(); return row_to_dict(row)


init_db()
