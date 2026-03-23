"""
Store Manager — Inventory Management System
GS1-compliant barcode/QR support, POS, Invoicing, PO/SO, Universal Scanning
Multi-tenant SaaS with user auth, API keys, and store integrations.
"""

import sqlite3, os, io, json, uuid, re, threading, time, hashlib, secrets
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from urllib.parse import urlparse

import requests
from flask import (
    Flask, render_template, request, redirect, url_for,
    send_file, jsonify, flash, session
)
from barcode import generate as bc_generate
from barcode.writer import ImageWriter
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
import qrcode

# ─── App Setup ────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'store-manager-' + str(uuid.uuid4()))
app.config['DATABASE'] = os.path.join(os.path.dirname(__file__), 'tpims.db')
app.config['BARCODES'] = os.path.join(os.path.dirname(__file__), 'static', 'barcodes')
app.config['EXPORTS']  = os.path.join(os.path.dirname(__file__), 'exports')
os.makedirs(app.config['BARCODES'], exist_ok=True)
os.makedirs(app.config['EXPORTS'], exist_ok=True)

# ─── Auth Setup ───────────────────────────────────────────────────────────────

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

@login_manager.user_loader
def load_user(user_id):
    conn = get_db()
    user = conn.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()
    conn.close()
    if user:
        return dict(user)
    return None

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def verify_password(password, pw_hash):
    return hashlib.sha256(password.encode()).hexdigest() == pw_hash

def generate_api_key():
    return 'sk_live_' + secrets.token_hex(24)

def hash_api_key(key):
    return hashlib.sha256(key.encode()).hexdigest()

def validate_api_key(api_key):
    key_hash = hash_api_key(api_key)
    conn = get_db()
    row = conn.execute('SELECT user_id FROM api_keys WHERE key_hash=?', (key_hash,)).fetchone()
    if row:
        conn.execute('UPDATE api_keys SET last_used=datetime("now") WHERE key_hash=?', (key_hash,))
        conn.commit()
        conn.close()
        return row['user_id']
    conn.close()
    return None

def get_current_user_id():
    """Return current user ID or None. Works for both session auth and API key auth."""
    if current_user.is_authenticated:
        return current_user.id
    return None

def update_setting_for_user(user_id, key, value):
    conn = get_db()
    conn.execute('INSERT OR REPLACE INTO settings (user_id, key, value) VALUES (?, ?, ?)',
        (user_id, key, value))
    conn.commit()
    conn.close()

def get_setting_for_user(user_id, key, default=None):
    conn = get_db()
    row = conn.execute('SELECT value FROM settings WHERE user_id=? AND key=?', (user_id, key)).fetchone()
    conn.close()
    return row['value'] if row else default

def log_user_audit(user_id, action, details=None, ip_address=None):
    conn = get_db()
    conn.execute('''
        INSERT INTO user_audit_log (user_id, action, details, ip_address, created_at)
        VALUES (?, ?, ?, ?, datetime('now'))
    ''', (user_id, action, json.dumps(details) if details else None, ip_address))
    conn.commit()
    conn.close()

# ─── Ollama AI Setup ─────────────────────────────────────────────────────────
OLLAMA_BASE_URL = os.environ.get('OLLAMA_BASE_URL', 'http://localhost:11434')
OLLAMA_MODEL = os.environ.get('OLLAMA_MODEL', 'llama3')
_ollama_initialized = False

def _ensure_ollama():
    """Lazy init — only connects when actually needed."""
    global _ollama_initialized
    _ollama_initialized = True

def ollama_chat(prompt, system_prompt=None, model=None):
    """Send a chat request to Ollama. Returns text response or None on failure."""
    try:
        import urllib.request, json
        url = f"{OLLAMA_BASE_URL}/api/chat"
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        data = json.dumps({
            "model": model or OLLAMA_MODEL,
            "messages": messages,
            "stream": False
        }).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read().decode())
            return result.get("message", {}).get("content", "").strip()
    except Exception as e:
        print(f"[Ollama] Error: {e}")
        return None

# ─── DB Helpers ────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(app.config['DATABASE'])
    conn.row_factory = sqlite3.Row
    return conn

def dict_from_row(r):
    return dict(r) if r else None

def init_db():
    conn = get_db()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS settings (
            user_id INTEGER DEFAULT 0,
            key TEXT NOT NULL,
            value TEXT,
            PRIMARY KEY (user_id, key)
        );

        CREATE TABLE IF NOT EXISTS warehouses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            code TEXT UNIQUE NOT NULL,
            address TEXT,
            contact_person TEXT,
            phone TEXT,
            email TEXT,
            is_active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS suppliers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            code TEXT UNIQUE,
            contact_name TEXT,
            phone TEXT,
            email TEXT,
            address TEXT,
            gstin TEXT,
            tax_no TEXT,
            payment_terms TEXT,
            notes TEXT,
            is_active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            parent_id INTEGER REFERENCES categories(id),
            description TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS units (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            code TEXT UNIQUE NOT NULL,
            is_decimal INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sku TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            description TEXT,
            category_id INTEGER REFERENCES categories(id),
            unit_id INTEGER REFERENCES units(id),
            hsn_code TEXT,
            barcode_format TEXT DEFAULT 'GS1-128',
            custom_barcode TEXT,
            gtin TEXT,
            cost_price REAL DEFAULT 0,
            selling_price REAL DEFAULT 0,
            mrp REAL DEFAULT 0,
            tax_rate REAL DEFAULT 0,
            reorder_point INTEGER DEFAULT 5,
            initial_stock INTEGER DEFAULT 0,
            weight REAL,
            dimensions TEXT,
            image_url TEXT,
            is_active INTEGER DEFAULT 1,
            has_serial_tracking INTEGER DEFAULT 0,
            has_batch_tracking INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS product_barcodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER REFERENCES products(id),
            barcode TEXT NOT NULL,
            barcode_format TEXT,
            is_primary INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(product_id, barcode)
        );

        CREATE TABLE IF NOT EXISTS inventory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER REFERENCES products(id),
            warehouse_id INTEGER REFERENCES warehouses(id),
            available_qty REAL DEFAULT 0,
            reserved_qty REAL DEFAULT 0,
            ordered_qty REAL DEFAULT 0,
            reorder_point INTEGER DEFAULT 5,
            updated_at TEXT DEFAULT (datetime('now')),
            UNIQUE(product_id, warehouse_id)
        );

        CREATE TABLE IF NOT EXISTS serial_numbers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER REFERENCES products(id),
            serial_number TEXT UNIQUE NOT NULL,
            batch_number TEXT,
            warehouse_id INTEGER REFERENCES warehouses(id),
            purchase_order_id INTEGER,
            invoice_id INTEGER,
            status TEXT DEFAULT 'available',
            warranty_expiry TEXT,
            notes TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER REFERENCES products(id),
            batch_number TEXT NOT NULL,
            expiry_date TEXT,
            warehouse_id INTEGER REFERENCES warehouses(id),
            quantity_initial REAL DEFAULT 0,
            quantity_available REAL DEFAULT 0,
            cost_price REAL,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS stock_moves (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER REFERENCES products(id),
            warehouse_id INTEGER REFERENCES warehouses(id),
            ref_type TEXT,
            ref_id INTEGER,
            move_type TEXT NOT NULL,
            qty_change REAL NOT NULL,
            balance_after REAL,
            serial_number TEXT,
            batch_number TEXT,
            notes TEXT,
            created_by TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS purchase_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            po_number TEXT UNIQUE NOT NULL,
            supplier_id INTEGER REFERENCES suppliers(id),
            warehouse_id INTEGER REFERENCES warehouses(id),
            status TEXT DEFAULT 'draft',
            order_date TEXT,
            expected_date TEXT,
            subtotal REAL DEFAULT 0,
            tax_amount REAL DEFAULT 0,
            discount_amount REAL DEFAULT 0,
            total_amount REAL DEFAULT 0,
            notes TEXT,
            terms TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS purchase_order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            po_id INTEGER REFERENCES purchase_orders(id),
            product_id INTEGER REFERENCES products(id),
            description TEXT,
            unit_id INTEGER REFERENCES units(id),
            ordered_qty REAL DEFAULT 0,
            received_qty REAL DEFAULT 0,
            rejected_qty REAL DEFAULT 0,
            unit_price REAL DEFAULT 0,
            tax_rate REAL DEFAULT 0,
            discount_pct REAL DEFAULT 0,
            total REAL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS sales_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            so_number TEXT UNIQUE NOT NULL,
            customer_name TEXT,
            customer_phone TEXT,
            customer_email TEXT,
            customer_address TEXT,
            warehouse_id INTEGER REFERENCES warehouses(id),
            status TEXT DEFAULT 'pending',
            order_date TEXT,
            subtotal REAL DEFAULT 0,
            tax_amount REAL DEFAULT 0,
            discount_amount REAL DEFAULT 0,
            shipping_amount REAL DEFAULT 0,
            total_amount REAL DEFAULT 0,
            notes TEXT,
            source TEXT DEFAULT 'manual',
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS sales_order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            so_id INTEGER REFERENCES sales_orders(id),
            product_id INTEGER REFERENCES products(id),
            description TEXT,
            unit_id INTEGER REFERENCES units(id),
            ordered_qty REAL DEFAULT 0,
            fulfilled_qty REAL DEFAULT 0,
            unit_price REAL DEFAULT 0,
            tax_rate REAL DEFAULT 0,
            discount_pct REAL DEFAULT 0,
            total REAL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_number TEXT UNIQUE NOT NULL,
            so_id INTEGER REFERENCES sales_orders(id),
            customer_name TEXT,
            customer_phone TEXT,
            customer_email TEXT,
            customer_address TEXT,
            customer_gstin TEXT,
            warehouse_id INTEGER REFERENCES warehouses(id),
            invoice_date TEXT,
            due_date TEXT,
            subtotal REAL DEFAULT 0,
            tax_amount REAL DEFAULT 0,
            discount_amount REAL DEFAULT 0,
            shipping_amount REAL DEFAULT 0,
            total_amount REAL DEFAULT 0,
            amount_paid REAL DEFAULT 0,
            payment_status TEXT DEFAULT 'unpaid',
            payment_method TEXT,
            notes TEXT,
            terms TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS invoice_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_id INTEGER REFERENCES invoices(id),
            product_id INTEGER REFERENCES products(id),
            description TEXT,
            hsn_code TEXT,
            unit_id INTEGER REFERENCES units(id),
            qty REAL DEFAULT 0,
            unit_price REAL DEFAULT 0,
            tax_rate REAL DEFAULT 0,
            discount_pct REAL DEFAULT 0,
            total REAL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS pos_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_number TEXT UNIQUE NOT NULL,
            warehouse_id INTEGER REFERENCES warehouses(id),
            status TEXT DEFAULT 'open',
            subtotal REAL DEFAULT 0,
            tax_amount REAL DEFAULT 0,
            discount_amount REAL DEFAULT 0,
            total_amount REAL DEFAULT 0,
            amount_paid REAL DEFAULT 0,
            change_amount REAL DEFAULT 0,
            payment_method TEXT,
            cashier_name TEXT,
            opened_at TEXT DEFAULT (datetime('now')),
            closed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS pos_session_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER REFERENCES pos_sessions(id),
            product_id INTEGER REFERENCES products(id),
            qty REAL DEFAULT 0,
            unit_price REAL DEFAULT 0,
            tax_rate REAL DEFAULT 0,
            discount_pct REAL DEFAULT 0,
            total REAL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS stock_adjustments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            adjustment_number TEXT UNIQUE NOT NULL,
            warehouse_id INTEGER REFERENCES warehouses(id),
            reason TEXT NOT NULL,
            notes TEXT,
            approved_by TEXT,
            status TEXT DEFAULT 'approved',
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS stock_adjustment_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            adjustment_id INTEGER REFERENCES stock_adjustments(id),
            product_id INTEGER REFERENCES products(id),
            qty_before REAL DEFAULT 0,
            qty_after REAL DEFAULT 0,
            qty_change REAL,
            serial_number TEXT,
            batch_number TEXT
        );

        CREATE TABLE IF NOT EXISTS stock_counts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            warehouse_id INTEGER REFERENCES warehouses(id),
            count_number TEXT UNIQUE NOT NULL,
            status TEXT DEFAULT 'in_progress',
            counted_by TEXT,
            notes TEXT,
            scheduled_date TEXT,
            completed_at TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS stock_count_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            count_id INTEGER REFERENCES stock_counts(id),
            product_id INTEGER REFERENCES products(id),
            system_qty REAL DEFAULT 0,
            counted_qty REAL DEFAULT 0,
            variance REAL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS transfer_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            transfer_number TEXT UNIQUE NOT NULL,
            from_warehouse_id INTEGER REFERENCES warehouses(id),
            to_warehouse_id INTEGER REFERENCES warehouses(id),
            status TEXT DEFAULT 'draft',
            sent_date TEXT,
            received_date TEXT,
            notes TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS transfer_order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            transfer_id INTEGER REFERENCES transfer_orders(id),
            product_id INTEGER REFERENCES products(id),
            qty_sent REAL DEFAULT 0,
            qty_received REAL DEFAULT 0,
            qty_short REAL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_type TEXT NOT NULL,
            entity_id INTEGER,
            action TEXT NOT NULL,
            changes TEXT,
            user_id TEXT,
            ip_address TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            company_name TEXT DEFAULT 'My Store',
            is_active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS api_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            key_hash TEXT NOT NULL,
            last_used TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS store_integrations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            platform TEXT NOT NULL,
            store_url TEXT,
            api_key TEXT,
            api_secret TEXT,
            access_token TEXT,
            is_active INTEGER DEFAULT 1,
            last_sync TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS user_audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            action TEXT,
            details TEXT,
            ip_address TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
    ''')

    # Default settings
    defaults = {
        'company_name': 'Store Manager',
        'gs1_prefix': '890',
        'currency': 'INR',
        'currency_symbol': 'Rs.',
        'decimal_places': '2',
        'tax_number': '',
        'address': '',
        'phone': '',
        'email': '',
        'barcode_format_default': 'GS1-128',
        'low_stock_email_enabled': '0',
        # Email provider: brevo | gmail | smtp
        'email_provider': 'brevo',
        'smtp_host': '',
        'smtp_port': '587',
        'smtp_username': '',
        'smtp_password': '',
        'smtp_from_email': '',
        'smtp_to_email': '',
        'email_brevo_key': '',
        'email_use_tls': '1',
    }
    for k, v in defaults.items():
        conn.execute('INSERT OR IGNORE INTO settings (user_id, key, value) VALUES (0, ?, ?)', (k, v))

    # Default warehouse
    cur = conn.execute('SELECT COUNT(*) FROM warehouses')
    if cur.fetchone()[0] == 0:
        conn.execute('''
            INSERT INTO warehouses (name, code, address, contact_person)
            VALUES ('Main Warehouse', 'MAIN', '', 'Admin')
        ''')

    # Default units
    cur = conn.execute('SELECT COUNT(*) FROM units')
    if cur.fetchone()[0] == 0:
        conn.execute("INSERT INTO units (name, code, is_decimal) VALUES ('Pieces', 'PCS', 0), ('Kilograms', 'KG', 1), ('Grams', 'G', 1), ('Liters', 'L', 1), ('Meters', 'M', 1)")

    conn.commit()
    conn.close()


# ─── Settings Helpers ─────────────────────────────────────────────────────────

def uid():
    """Return current user ID or 0 for unauthenticated/global context."""
    try:
        if current_user.is_authenticated:
            return current_user.id
    except RuntimeError:
        # No request context (e.g. during init)
        pass
    return 0

def get_setting(key, default=None, user_id=None):
    if user_id is None:
        user_id = uid()
    conn = get_db()
    row = conn.execute('SELECT value FROM settings WHERE user_id=? AND key=?', (user_id, key)).fetchone()
    conn.close()
    return row['value'] if row else default

def update_setting(key, value, user_id=None):
    if user_id is None:
        user_id = uid()
    conn = get_db()
    conn.execute('INSERT OR REPLACE INTO settings (user_id, key, value) VALUES (?, ?, ?)', (user_id, key, value))
    conn.commit()
    conn.close()

def get_settings_dict(user_id=None):
    if user_id is None:
        user_id = uid()
    conn = get_db()
    rows = conn.execute('SELECT * FROM settings WHERE user_id=?', (user_id,)).fetchall()
    conn.close()
    return {r['key']: r['value'] for r in rows}

def log_audit(entity_type, entity_id, action, changes=None):
    conn = get_db()
    conn.execute('''
        INSERT INTO audit_log (entity_type, entity_id, action, changes, created_at)
        VALUES (?, ?, ?, ?, datetime('now'))
    ''', (entity_type, entity_id, action, json.dumps(changes) if changes else None))
    conn.commit()
    conn.close()


# ─── Multi-tenant Data Helpers ─────────────────────────────────────────────────

def user_warehouse_id(user_id=None):
    """Return the warehouse ID for the given user (or current user).
    Creates a default warehouse per user if needed."""
    if user_id is None:
        user_id = uid()
    if user_id == 0:
        return 1  # default shared warehouse
    conn = get_db()
    # Check if user has a designated warehouse setting
    wh_id = conn.execute('SELECT value FROM settings WHERE user_id=? AND key=?', (user_id, 'default_warehouse_id')).fetchone()
    if wh_id:
        conn.close()
        return int(wh_id[0])
    # Find first warehouse belonging to user, or first global one
    wh = conn.execute('SELECT id FROM warehouses WHERE user_id=? LIMIT 1', (user_id,)).fetchone()
    if not wh:
        wh = conn.execute('SELECT id FROM warehouses WHERE user_id=0 LIMIT 1').fetchone()
    if wh:
        wid = wh[0]
        conn.close()
        return wid
    # Create a default warehouse for this user
    conn.execute('INSERT INTO warehouses (name, code, user_id) VALUES (?, ?, ?)',
        ('My Warehouse', f'WH-{user_id}', user_id))
    wid = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
    conn.commit()
    conn.close()
    return wid

def get_user_warehouses(user_id=None):
    """Return warehouses visible to a user (owned or global)."""
    if user_id is None:
        user_id = uid()
    conn = get_db()
    if user_id == 0:
        rows = conn.execute('SELECT * FROM warehouses WHERE is_active=1 ORDER BY name').fetchall()
    else:
        rows = conn.execute('SELECT * FROM warehouses WHERE is_active=1 AND (user_id=0 OR user_id=?) ORDER BY name', (user_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_products(filters=None, user_id=None):
    """Get products, scoped to user_id if authenticated."""
    if user_id is None:
        user_id = uid()
    conn = get_db()
    sql = '''
        SELECT p.*, c.name as category_name, u.name as unit_name, u.code as unit_code,
               COALESCE(SUM(i.available_qty), 0) as total_stock
        FROM products p
        LEFT JOIN categories c ON c.id = p.category_id
        LEFT JOIN units u ON u.id = p.unit_id
        LEFT JOIN inventory i ON i.product_id = p.id
        WHERE p.is_active=1
    '''
    params = []
    if user_id != 0:
        sql += ' AND (p.user_id = 0 OR p.user_id = ?)'
        params.append(user_id)
    if filters:
        if filters.get('sku'):
            sql += ' AND p.sku LIKE ?'
            params.append(f"%{filters['sku']}%")
        if filters.get('name'):
            sql += ' AND p.name LIKE ?'
            params.append(f"%{filters['name']}%")
        if filters.get('category_id'):
            sql += ' AND p.category_id = ?'
            params.append(filters['category_id'])
        if filters.get('warehouse_id'):
            sql += ' AND i.warehouse_id = ?'
            params.append(filters['warehouse_id'])
        if filters.get('low_stock'):
            sql += ' AND COALESCE(SUM(i.available_qty), 0) <= p.reorder_point'
    sql += ' GROUP BY p.id ORDER BY p.sku'
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_inventory_summary(product_id):
    conn = get_db()
    rows = conn.execute('''
        SELECT i.*, w.name as warehouse_name, w.code as warehouse_code
        FROM inventory i
        JOIN warehouses w ON w.id = i.warehouse_id
        WHERE i.product_id = ?
    ''', (product_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_low_stock_products():
    conn = get_db()
    rows = conn.execute('''
        SELECT p.*, c.name as category_name,
               COALESCE(SUM(i.available_qty), 0) as total_stock,
               p.reorder_point
        FROM products p
        LEFT JOIN categories c ON c.id = p.category_id
        LEFT JOIN inventory i ON i.product_id = p.id
        WHERE p.is_active=1
        GROUP BY p.id
        HAVING total_stock <= p.reorder_point
        ORDER BY total_stock ASC
    ''').fetchall()
    conn.close()
    return [dict(r) for r in rows]

def update_inventory(product_id, warehouse_id, qty_change, move_type, ref_type=None, ref_id=None, notes=None, serial_number=None, batch_number=None):
    conn = get_db()
    inv = conn.execute(
        'SELECT * FROM inventory WHERE product_id=? AND warehouse_id=?',
        (product_id, warehouse_id)
    ).fetchone()

    if inv:
        new_qty = inv['available_qty'] + qty_change
        conn.execute(
            'UPDATE inventory SET available_qty=?, updated_at=datetime("now") WHERE id=?',
            (max(0, new_qty), inv['id'])
        )
        balance = new_qty
    else:
        conn.execute(
            'INSERT INTO inventory (product_id, warehouse_id, available_qty) VALUES (?, ?, ?)',
            (product_id, warehouse_id, max(0, qty_change))
        )
        balance = max(0, qty_change)

    conn.execute('''
        INSERT INTO stock_moves (product_id, warehouse_id, ref_type, ref_id, move_type, qty_change, balance_after, serial_number, batch_number, notes, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
    ''', (product_id, warehouse_id, ref_type, ref_id, move_type, qty_change, balance, serial_number, batch_number, notes))
    conn.commit()
    conn.close()


# ─── GS1 QR / Barcode Generation ──────────────────────────────────────────────

BARCODE_FORMATS = {
    'GS1-128':   'code128',
    'EAN-13':    'ean13',
    'EAN-8':     'ean8',
    'UPC-A':     'upc',
    'CODE-128':  'code128',
    'CODE-39':   'code39',
    'QR':        'qr',
    'DATA-MATRIX': 'qr',
}

def generate_gtin(gs1_prefix, product_id):
    """Build a GTIN-13 from GS1 prefix + product reference (9 digits)."""
    prefix = str(gs1_prefix)[:3]
    # Remaining digits after prefix: 13 - 3 - 1(check) = 9 for product reference
    product_ref = str(product_id).zfill(9)[:9]
    base = f"{prefix}{product_ref}"  # 12 digits
    # Calculate GTIN-13 check digit (Mod10)
    digits = [int(c) for c in base]
    sum_odd = sum(digits[-1::-2])   # positions 1,3,5... from right (0-indexed odd)
    sum_even = sum(digits[-2::-2]) * 3  # positions 2,4,6... from right
    check = (10 - (sum_odd + sum_even) % 10) % 10
    return base + str(check)  # 13 digits GTIN-13

def build_gs1_qr_data(product, serial=None, gs1_prefix=None):
    """Build GS1-compliant barcode string for QR."""
    if gs1_prefix is None:
        gs1_prefix = get_setting('gs1_prefix', '890')
    gtin = generate_gtin(gs1_prefix, product['id'])
    # AI (01) = GTIN, AI (21) = serial number
    data = f"]C1{gtin}"  # ]C1 = GS1-128 / GS1 QR mode
    if serial:
        data += f"]C2{serial}"
    return data

def generate_qr_gs1(product, serial=None):
    """Generate GS1-compliant QR code as PNG bytes."""
    gs1_prefix = get_setting('gs1_prefix', '890')
    data = build_gs1_qr_data(product, serial, gs1_prefix)
    qr = qrcode.QRCode(version=2, error_correction=qrcode.constants.ERROR_CORRECT_M,
                       box_size=10, border=2)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color='black', back_color='white')
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return buf.getvalue(), data

def generate_barcode_image(product, fmt=None):
    """Generate barcode image for a product."""
    gs1_prefix = get_setting('gs1_prefix', '890')
    fmt = fmt or product.get('barcode_format', 'GS1-128')

    if fmt == 'QR':
        return generate_qr_gs1(product)

    # For GS1-128 / CODE-128 / CODE-39 / EAN-13 / EAN-8 / UPC
    if fmt == 'GS1-128':
        gtin = generate_gtin(gs1_prefix, product['id'])
        data = gtin
        bc_type = 'code128'
    elif fmt == 'CODE-39':
        data = product['sku']
        bc_type = 'code39'
    elif fmt == 'EAN-13':
        # Pad to 12 digits
        data = str(product['id']).zfill(12)[:12]
        bc_type = 'ean13'
    elif fmt == 'EAN-8':
        data = str(product['id']).zfill(7)[:7]
        bc_type = 'ean8'
    elif fmt == 'UPC-A':
        data = str(product['id']).zfill(11)[:11]
        bc_type = 'upc'
    else:  # CODE-128 fallback
        data = product['sku']
        bc_type = 'code128'

    try:
        buf = io.BytesIO()
        bc_generate(bc_type, data, writer=ImageWriter(), output=buf)
        buf.seek(0)
        return buf.getvalue(), data, fmt
    except Exception as e:
        # Ultimate fallback to code128 with SKU
        buf = io.BytesIO()
        bc_generate('code128', product['sku'], writer=ImageWriter(), output=buf)
        buf.seek(0)
        return buf.getvalue(), product['sku'], 'CODE-128'

def save_barcode_png(product, fmt=None):
    """Generate and save barcode PNG to static folder."""
    data, code_str, actual_fmt = generate_barcode_image(product, fmt)
    fname = f"{product['sku']}_{actual_fmt.lower().replace('-','_')}.png"
    fpath = os.path.join(app.config['BARCODES'], fname)
    with open(fpath, 'wb') as f:
        f.write(data)
    return fname, code_str, actual_fmt


# ─── Product URL Fetch ────────────────────────────────────────────────────────

def fetch_product_from_url(url):
    """Fetch and parse product info from a URL."""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-IN,en;q=0.9',
    }
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        return {'error': str(e)}

    from bs4 import BeautifulSoup
    soup = BeautifulSoup(resp.text, 'html.parser')

    name = None
    price = None
    description = None
    image_url = None

    # Try Open Graph
    og_name = soup.find('meta', property='og:title')
    if og_name:
        name = og_name.get('content', '').strip()
    if not name:
        title = soup.find('title')
        if title:
            name = title.string or ''

    og_price = soup.find('meta', property='product:price:amount')
    if og_price:
        price = float(og_price.get('content', 0))
    og_currency = soup.find('meta', property='product:price:currency')
    currency_meta = og_currency.get('content', '') if og_currency else ''

    # Try JSON-LD schema
    json_ld = soup.find('script', type='application/ld+json')
    if json_ld:
        try:
            data = json.loads(json_ld.string)
            items = data if isinstance(data, list) else [data]
            for item in items:
                if item.get('@type') == 'Product' or 'Product' in str(item.get('@type', '')):
                    name = item.get('name', name)
                    desc = item.get('description', '')
                    if not description:
                        description = desc
                    offers = item.get('offers', {})
                    if isinstance(offers, list):
                        offers = offers[0] if offers else {}
                    if not price and offers.get('price'):
                        try:
                            price = float(offers['price'])
                        except:
                            pass
                    img = item.get('image')
                    if img:
                        image_url = img if isinstance(img, str) else (img[0] if isinstance(img, list) else None)
                    break
        except:
            pass

    # Fallback: find price on page
    if not price:
        price_patterns = [
            'class="price"', 'class="product-price"', 'id="price"',
            'class="sale-price"', 'class="current-price"'
        ]
        for pattern in price_patterns:
            el = soup.find(class_=lambda c: c and 'price' in c.lower()) if 'class' in pattern else None
            if el:
                txt = el.get_text()
                nums = re.findall(r'[\d,]+\.?\d*', txt)
                if nums:
                    try:
                        price = float(nums[0].replace(',', ''))
                        break
                    except:
                        pass

    # Fallback name from h1
    if not name:
        h1 = soup.find('h1')
        if h1:
            name = h1.get_text(strip=True)

    # Image fallback
    if not image_url:
        img_tag = soup.find('meta', property='og:image')
        if img_tag:
            image_url = img_tag.get('content', '')

    return {
        'name': name or '',
        'price': price or 0,
        'description': description or '',
        'image_url': image_url or '',
        'url': url,
    }


# ─── Email System (Brevo / Gmail / SMTP) ─────────────────────────────────────

def _build_email_message(to_email, subject, html_body, text_body=None):
    """Build a MIMEMultipart email."""
    if text_body is None:
        text_body = re.sub(r'<[^>]+>', '', html_body)
    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['To'] = to_email
    msg.attach(MIMEText(text_body, 'plain', 'utf-8'))
    msg.attach(MIMEText(html_body, 'html', 'utf-8'))
    return msg


def _send_email_async(to_email, subject, html_body, text_body=None):
    """Send email in a background thread so it never blocks the Flask response."""
    def _send():
        try:
            _do_send_email(to_email, subject, html_body, text_body)
        except Exception as e:
            print(f"[Email] Failed to send to {to_email}: {e}")
    t = threading.Thread(target=_send, daemon=True)
    t.start()


def _do_send_email(to_email, subject, html_body, text_body=None):
    """Actually send the email via Brevo, Gmail, or generic SMTP."""
    provider = get_setting('email_provider', 'brevo')
    company = get_setting('company_name', 'Store Manager')
    from_email = get_setting('email_from', '')
    to_email_addr = to_email or get_setting('smtp_to_email', '')

    if provider == 'brevo':
        _send_via_brevo(from_email, to_email_addr, subject, html_body, text_body)
    elif provider == 'gmail':
        _send_via_gmail(from_email, to_email_addr, subject, html_body, text_body)
    else:
        _send_via_smtp(from_email, to_email_addr, subject, html_body, text_body)


def _send_via_brevo(from_email, to_email, subject, html_body, text_body=None):
    """Send via Brevo (Sendinblue) REST API — no SMTP needed."""
    api_key = get_setting('email_brevo_key', '')
    if not api_key:
        raise ValueError("Brevo API key not configured")

    if text_body is None:
        text_body = re.sub(r'<[^>]+>', '', html_body)

    url = "https://api.brevo.com/v3/smtp/email"
    payload = {
        "sender": {"name": get_setting('company_name', 'Store Manager'), "email": from_email or "noreply@brevo.com"},
        "to": [{"email": to_email}],
        "subject": subject,
        "htmlContent": html_body,
        "textContent": text_body,
    }
    if from_email:
        payload["sender"] = {"name": get_setting('company_name', 'Store Manager'), "email": from_email}

    resp = requests.post(url, json=payload, headers={
        "api-key": api_key,
        "Content-Type": "application/json",
    }, timeout=15)
    if resp.status_code >= 400:
        raise Exception(f"Brevo error {resp.status_code}: {resp.text}")


def _send_via_gmail(from_email, to_email, subject, html_body, text_body=None):
    """Send via Gmail SMTP."""
    import smtplib
    msg = _build_email_message(to_email, subject, html_body, text_body)
    msg['From'] = from_email
    host = 'smtp.gmail.com'
    port = 587
    user = get_setting('smtp_username', '')
    password = get_setting('smtp_password', '')
    with smtplib.SMTP(host, port, timeout=20) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(user, password)
        server.send_message(msg)


def _send_via_smtp(from_email, to_email, subject, html_body, text_body=None):
    """Send via generic SMTP."""
    import smtplib
    msg = _build_email_message(to_email, subject, html_body, text_body)
    msg['From'] = from_email
    host = get_setting('smtp_host', '')
    port = int(get_setting('smtp_port', '587') or 587)
    user = get_setting('smtp_username', '')
    password = get_setting('smtp_password', '')
    use_tls = get_setting('email_use_tls', '1') == '1'
    with smtplib.SMTP(host, port, timeout=20) as server:
        if use_tls:
            server.ehlo()
            server.starttls()
            server.ehlo()
        if user and password:
            server.login(user, password)
        server.send_message(msg)


# ─── Email Templates ──────────────────────────────────────────────────────────

def _email_base(company_name, body_html):
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;background:#f1f5f9;margin:0;padding:0}}
.wrap{{max-width:600px;margin:30px auto;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,.08)}}
.header{{background:#1e293b;padding:1.5rem;text-align:center;color:#fff}}
.header h1{{margin:0;font-size:1.2rem;color:#60a5fa;letter-spacing:.5px}}
.content{{padding:1.5rem}}
.footer{{padding:.75rem 1.5rem;background:#f8fafc;text-align:center;font-size:.75rem;color:#94a3b8;border-top:1px solid #e2e8f0}}
table{{width:100%;border-collapse:collapse;font-size:.85rem}}
th{{text-align:left;padding:.5rem .75rem;background:#f1f5f9;border-bottom:2px solid #e2e8f0;text-transform:uppercase;font-size:.72rem;letter-spacing:.5px;color:#64748b}}
td{{padding:.5rem .75rem;border-bottom:1px solid #e2e8f0}}
.status-ok{{color:#16a34a}} .status-warn{{color:#d97706}} .status-bad{{color:#dc2626}}
</style></head><body>
<div class="wrap">
  <div class="header"><h1>{company_name}</h1></div>
  <div class="content">{body_html}</div>
  <div class="footer">{company_name} — Inventory Management System</div>
</div></body></html>"""


def _low_stock_email_html(products, company_name, currency):
    rows = ""
    for p in products:
        rows += f"""<tr>
          <td><b>{p['sku']}</b></td>
          <td>{p['name']}</td>
          <td style="color:#dc2626;font-weight:700;text-align:center">{int(p['total_stock'])}</td>
          <td style="text-align:center">{p['reorder_point']}</td>
          <td style="text-align:right">{currency}{p['cost_price']:.2f}</td>
        </tr>"""
    body = f"""
<h2 style="margin-top:0;color:#dc2626;">Low Stock Alert</h2>
<p style="color:#64748b;font-size:.9rem;">
  {len(products)} product(s) are at or below their reorder point and need restocking.
</p>
<table>
  <thead><tr><th>SKU</th><th>Product</th><th style="text-align:center">Stock</th><th style="text-align:center">Reorder At</th><th style="text-align:right">Cost</th></tr></thead>
  <tbody>{rows}</tbody>
</table>
<p style="margin-top:1rem;font-size:.85rem;color:#64748b;">
  Please review these items and create purchase orders to replenish stock.
</p>"""
    return _email_base(company_name, body)


def _invoice_email_html(invoice, items, company_name, currency):
    rows = ""
    for i, item in enumerate(items, 1):
        rows += f"""<tr>
          <td>{i}</td>
          <td>{item.get('description','—')}</td>
          <td style="text-align:right">{item['qty']}</td>
          <td style="text-align:right">{currency}{item['unit_price']:.2f}</td>
          <td style="text-align:right">{currency}{item['total']:.2f}</td>
        </tr>"""
    body = f"""
<h2 style="margin-top:0;">Invoice {invoice['invoice_number']}</h2>
<p style="color:#64748b;font-size:.9rem;">Thank you for your business!</p>
<div style="background:#f1f5f9;border-radius:6px;padding:1rem;margin:.75rem 0;font-size:.85rem;">
  <b>Invoice #:</b> {invoice['invoice_number']}<br>
  <b>Date:</b> {invoice.get('invoice_date', invoice.get('created_at','')[:10])}<br>
  <b>Amount:</b> <b style="color:#2563eb;font-size:1.1rem;">{currency}{invoice['total_amount']:.2f}</b>
</div>
<table>
  <thead><tr><th>#</th><th>Description</th><th style="text-align:right">Qty</th><th style="text-align:right">Unit Price</th><th style="text-align:right">Total</th></tr></thead>
  <tbody>{rows}</tbody>
</table>
<p style="text-align:right;margin-top:.75rem;font-size:.9rem;">
  <b>Total: {currency}{invoice['total_amount']:.2f}</b>
</p>"""
    return _email_base(company_name, body)


def _order_confirmation_email(so_or_po, items, order_type, company_name, currency):
    num = so_or_po.get('so_number') or so_or_po.get('po_number')
    rows = ""
    for i, item in enumerate(items, 1):
        rows += f"""<tr>
          <td>{i}</td>
          <td>{item.get('description','')}</td>
          <td style="text-align:right">{item.get('ordered_qty','')}</td>
          <td style="text-align:right">{currency}{item.get('unit_price',0):.2f}</td>
          <td style="text-align:right">{currency}{item.get('total',0):.2f}</td>
        </tr>"""
    body = f"""
<h2 style="margin-top:0;">{order_type} Confirmation — {num}</h2>
<p style="color:#64748b;font-size:.9rem;">Your order has been created successfully.</p>
<div style="background:#f1f5f9;border-radius:6px;padding:1rem;margin:.75rem 0;font-size:.85rem;">
  <b>Order #:</b> {num}<br>
  <b>Date:</b> {so_or_po.get('order_date','') or so_or_po.get('created_at','')[:10]}<br>
  <b>Total:</b> <b>{currency}{(so_or_po.get('total_amount') or so_or_po.get('total') or 0):.2f}</b>
</div>
<table>
  <thead><tr><th>#</th><th>Item</th><th style="text-align:right">Qty</th><th style="text-align:right">Price</th><th style="text-align:right">Total</th></tr></thead>
  <tbody>{rows}</tbody>
</table>"""
    return _email_base(company_name, body)


# ─── Public Email API ─────────────────────────────────────────────────────────

def send_low_stock_email():
    """Queue a low stock alert email in the background."""
    if get_setting('low_stock_email_enabled') != '1':
        return
    products = get_low_stock_products()
    if not products:
        return
    company = get_setting('company_name', 'Store Manager')
    currency = get_setting('currency_symbol', 'Rs.')
    to_email = get_setting('smtp_to_email', '')
    if not to_email:
        return
    subject = f"[{company}] Low Stock Alert — {len(products)} item(s)"
    html = _low_stock_email_html(products, company, currency)
    _send_email_async(to_email, subject, html)


def send_test_email(to_email=None):
    """Send a test email. Returns (success, message)."""
    company = get_setting('company_name', 'Store Manager')
    to_addr = to_email or get_setting('smtp_to_email', '')
    if not to_addr:
        return False, "No recipient configured"
    subject = f"[{company}] Test Email — Configuration OK"
    html = _email_base(company, f"""
<h2 style="margin-top:0;color:#16a34a;">Email Configuration Working!</h2>
<p>This test email was sent successfully from <b>{company}</b>.</p>
<p style="color:#64748b;font-size:.9rem;">Provider: {get_setting('email_provider','brevo').upper()}</p>
<p style="color:#64748b;font-size:.85rem;">If you received this, your email settings are correct.</p>
""")
    try:
        _do_send_email(to_addr, subject, html)
        return True, f"Test email sent to {to_addr}"
    except Exception as e:
        return False, str(e)


def send_invoice_email(invoice_id, to_email_override=None):
    """Queue invoice email in the background."""
    conn = get_db()
    invoice = conn.execute('SELECT * FROM invoices WHERE id=?', (invoice_id,)).fetchone()
    items = conn.execute('SELECT * FROM invoice_items WHERE invoice_id=?', (invoice_id,)).fetchall()
    conn.close()
    if not invoice:
        return
    company = get_setting('company_name', 'Store Manager')
    currency = get_setting('currency_symbol', 'Rs.')
    to_addr = to_email_override or (invoice['customer_email'] if invoice['customer_email'] else get_setting('smtp_to_email', ''))
    if not to_addr:
        return
    subject = f"Invoice {invoice['invoice_number']} from {company}"
    html = _invoice_email_html(dict(invoice), [dict(i) for i in items], company, currency)
    _send_email_async(to_addr, subject, html)


def send_order_email(order_type, order_id):
    """Queue SO/PO confirmation email in the background."""
    conn = get_db()
    table = 'sales_orders' if order_type == 'SO' else 'purchase_orders'
    items_table = 'sales_order_items' if order_type == 'SO' else 'purchase_order_items'
    order = conn.execute(f'SELECT * FROM {table} WHERE id=?', (order_id,)).fetchone()
    items = conn.execute(f'SELECT * FROM {items_table} WHERE {order_type.lower()}_id=?', (order_id,)).fetchall()
    conn.close()
    if not order:
        return
    company = get_setting('company_name', 'Store Manager')
    currency = get_setting('currency_symbol', 'Rs.')
    to_addr = get_setting('smtp_to_email', '')
    if not to_addr:
        return
    subject = f"{order_type} {order.get('so_number') or order.get('po_number')} Created"
    html = _order_confirmation_email(dict(order), [dict(i) for i in items], order_type, company, currency)
    _send_email_async(to_addr, subject, html)


# ─── Context Processors ───────────────────────────────────────────────────────

@app.context_processor
def inject_globals():
    current_uid = uid()
    warehouses = get_user_warehouses(current_uid) if current_uid != 0 else [dict(w) for w in get_db().execute('SELECT * FROM warehouses WHERE is_active=1 ORDER BY name').fetchall()]
    conn = get_db()
    categories = conn.execute('SELECT * FROM categories ORDER BY name').fetchall()
    suppliers = conn.execute('SELECT * FROM suppliers WHERE is_active=1 ORDER BY name').fetchall()
    conn.close()
    settings = get_settings_dict()
    low_stock = get_low_stock_products()
    return dict(
        warehouses=warehouses,
        categories=[dict(c) for c in categories],
        suppliers=[dict(s) for s in suppliers],
        company_name=settings.get('company_name', 'Store Manager'),
        gs1_prefix=settings.get('gs1_prefix', '890'),
        currency=settings.get('currency_symbol', 'Rs.'),
        page=request.path,
        low_stock_alert=len(low_stock) > 0,
        low_stock_count=len(low_stock),
    )


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route('/')
def dashboard():
    # Guest mode: if no users exist yet, show dashboard without login
    conn = get_db()
    user_count = conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]
    conn.close()

    if user_count > 0 and not current_user.is_authenticated:
        return redirect(url_for('login'))

    conn = get_db()
    uid_val = uid()
    # Build stats scoped to user
    if uid_val == 0:
        products_filter = 'is_active=1'
        inventory_filter = ''
    else:
        products_filter = 'is_active=1 AND (user_id=0 OR user_id=?)'
        inventory_filter = ' AND (p.user_id=0 OR p.user_id=?)'

    params = [uid_val] if uid_val != 0 else []
    stats = {
        'total_products': conn.execute(f'SELECT COUNT(*) FROM products WHERE {products_filter}', params).fetchone()[0],
        'total_suppliers': conn.execute(f'SELECT COUNT(*) FROM suppliers WHERE is_active=1 {inventory_filter.replace("user_id=0 OR user_id=?","(suppliers.user_id=0 OR suppliers.user_id=?)" if uid_val != 0 else "1=1")}', params if uid_val != 0 else []).fetchone()[0],
        'total_warehouses': conn.execute('SELECT COUNT(*) FROM warehouses WHERE is_active=1').fetchone()[0],
        'total_stock_value': conn.execute(f'''
            SELECT COALESCE(SUM(i.available_qty * p.cost_price), 0)
            FROM inventory i JOIN products p ON p.id=i.product_id WHERE p.is_active=1 {inventory_filter}
        ''', params + params if uid_val != 0 else []).fetchone()[0],
        'low_stock_count': len(get_low_stock_products()),
        'pending_orders': conn.execute("SELECT COUNT(*) FROM sales_orders WHERE status='pending'").fetchone()[0],
        'pending_pos': conn.execute("SELECT COUNT(*) FROM purchase_orders WHERE status IN ('draft','ordered','partial')").fetchone()[0],
        'total_invoices': conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0],
        'unpaid_invoices': conn.execute("SELECT COUNT(*) FROM invoices WHERE payment_status='unpaid'").fetchone()[0],
    }

    low_stock = get_low_stock_products()[:10]
    recent_moves = conn.execute('''
        SELECT sm.*, p.name as product_name, p.sku, w.name as warehouse_name
        FROM stock_moves sm
        JOIN products p ON p.id=sm.product_id
        JOIN warehouses w ON w.id=sm.warehouse_id
        ORDER BY sm.created_at DESC LIMIT 10
    ''').fetchall()
    recent_orders = conn.execute('''
        SELECT so.*, COUNT(soi.id) as item_count
        FROM sales_orders so
        LEFT JOIN sales_order_items soi ON soi.so_id=so.id
        GROUP BY so.id ORDER BY so.created_at DESC LIMIT 8
    ''').fetchall()
    conn.close()

    send_low_stock_email()

    # AI-powered quick insight
    ai_insight = None
    if low_stock:
        try:
            names = [p['name'] for p in low_stock[:5]]
            prompt = f"These products are low on stock: {', '.join(names)}. Give a one-line actionable recommendation."
            ai_insight = ollama_chat(prompt)
        except:
            pass

    return render_template('dashboard.html',
        stats=stats, low_stock=low_stock,
        recent_moves=[dict(m) for m in recent_moves],
        recent_orders=[dict(o) for o in recent_orders],
        ai_insight=ai_insight)


# ─── Auth Routes ───────────────────────────────────────────────────────────────

@app.route('/auth/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        email = request.form['email'].strip().lower()
        password = request.form['password']
        confirm = request.form.get('confirm_password', '')
        company_name = request.form.get('company_name', 'My Store').strip()

        if len(password) < 6:
            flash('Password must be at least 6 characters', 'error')
            return redirect(url_for('signup'))
        if password != confirm:
            flash('Passwords do not match', 'error')
            return redirect(url_for('signup'))

        pw_hash = hash_password(password)
        conn = get_db()
        try:
            conn.execute('INSERT INTO users (email, password_hash, company_name) VALUES (?, ?, ?)',
                (email, pw_hash, company_name))
            conn.commit()
            user = conn.execute('SELECT * FROM users WHERE email=?', (email,)).fetchone()
            conn.close()
            login_user(dict(user))
            update_setting_for_user(user['id'], 'company_name', company_name)
            update_setting_for_user(user['id'], 'gs1_prefix', '890')
            update_setting_for_user(user['id'], 'currency', 'INR')
            update_setting_for_user(user['id'], 'currency_symbol', 'Rs.')
            flash('Account created! Welcome to Store Manager.', 'success')
            return redirect(url_for('dashboard'))
        except Exception as e:
            conn.close()
            flash('Email already registered', 'error')
            return redirect(url_for('signup'))
    return render_template('auth/signup.html')

@app.route('/auth/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        email = request.form['email'].strip().lower()
        password = request.form['password']
        conn = get_db()
        user = conn.execute('SELECT * FROM users WHERE email=? AND is_active=1', (email,)).fetchone()
        conn.close()
        if user and verify_password(password, user['password_hash']):
            login_user(dict(user))
            log_user_audit(user['id'], 'login')
            next_page = request.args.get('next')
            return redirect(next_page if next_page else url_for('dashboard'))
        flash('Invalid email or password', 'error')
    return render_template('auth/login.html')

@app.route('/auth/logout')
@login_required
def logout():
    log_user_audit(current_user.id, 'logout')
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('login'))

# ── Categories ────────────────────────────────────────────────────────────────

@app.route('/categories')
@login_required
def categories():
    conn = get_db()
    rows = conn.execute('SELECT c.*, COUNT(p.id) as product_count FROM categories c LEFT JOIN products p ON p.category_id=c.id GROUP BY c.id ORDER BY c.name').fetchall()
    conn.close()
    return render_template('categories/list.html', categories=[dict(r) for r in rows])

@app.route('/categories/add', methods=['POST'])
def category_create():
    conn = get_db()
    conn.execute('INSERT INTO categories (name, description) VALUES (?, ?)',
        (request.form['name'].strip(), request.form.get('description', '')))
    conn.commit()
    conn.close()
    flash('Category created!', 'success')
    return redirect(url_for('categories'))


# ── Products ──────────────────────────────────────────────────────────────────

@app.route('/products')
@login_required
def products():
    filters = {
        'sku': request.args.get('sku'),
        'name': request.args.get('name'),
        'category_id': request.args.get('category_id'),
    }
    product_list = get_products(filters)
    conn = get_db()
    all_categories = conn.execute('SELECT * FROM categories ORDER BY name').fetchall()
    conn.close()
    return render_template('products/list.html',
        products=product_list,
        categories=[dict(c) for c in all_categories],
        filters=filters)

@app.route('/products/fetch-from-url', methods=['GET', 'POST'])
def product_fetch_from_url():
    """Fetch product info from URL and show preview."""
    preview_data = None
    error = None

    if request.method == 'POST':
        url = request.form.get('url', '').strip()
        if not url:
            error = 'Please provide a URL'
        else:
            # Validate URL
            try:
                parsed = urlparse(url)
                if not parsed.scheme:
                    url = 'https://' + url
                    parsed = urlparse(url)
                if not parsed.netloc:
                    error = 'Invalid URL'
            except:
                error = 'Invalid URL format'

            if not error:
                preview_data = fetch_product_from_url(url)
                if 'error' in preview_data:
                    error = preview_data['error']
                    preview_data = None

    conn = get_db()
    categories = conn.execute('SELECT * FROM categories ORDER BY name').fetchall()
    units = conn.execute('SELECT * FROM units ORDER BY name').fetchall()
    warehouses = conn.execute('SELECT * FROM warehouses WHERE is_active=1 ORDER BY name').fetchall()
    conn.close()
    return render_template('products/fetch_from_url.html',
        preview_data=preview_data,
        error=error,
        categories=[dict(c) for c in categories],
        units=[dict(u) for u in units],
        warehouses=[dict(w) for w in warehouses])

@app.route('/products/create-from-preview', methods=['POST'])
def product_create_from_preview():
    """Create a product from URL-fetch preview data."""
    sku = request.form['sku'].strip().upper()
    name = request.form['name'].strip()
    description = request.form.get('description', '')
    category_id = request.form.get('category_id') or None
    unit_id = request.form.get('unit_id') or None
    hsn_code = request.form.get('hsn_code', '')
    cost_price = float(request.form.get('cost_price') or 0)
    selling_price = float(request.form.get('selling_price') or 0)
    mrp = float(request.form.get('mrp') or 0)
    tax_rate = float(request.form.get('tax_rate') or 0)
    reorder_point = int(request.form.get('reorder_point') or 5)
    initial_stock = int(request.form.get('initial_stock') or 0)
    barcode_format = request.form.get('barcode_format', 'GS1-128')
    warehouse_id = request.form.get('warehouse_id')
    image_url = request.form.get('image_url', '')

    conn = get_db()
    try:
        conn.execute('''
            INSERT INTO products (sku, name, description, category_id, unit_id, hsn_code,
                cost_price, selling_price, mrp, tax_rate, reorder_point, initial_stock,
                barcode_format, image_url)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (sku, name, description, category_id, unit_id, hsn_code,
              cost_price, selling_price, mrp, tax_rate, reorder_point, initial_stock, barcode_format, image_url))
        product_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]

        conn.execute('INSERT INTO product_barcodes (product_id, barcode, barcode_format, is_primary) VALUES (?, ?, ?, 1)',
            (product_id, sku, barcode_format))

        if initial_stock > 0 and warehouse_id:
            wh_id = int(warehouse_id)
            inv = conn.execute('SELECT id FROM inventory WHERE product_id=? AND warehouse_id=?', (product_id, wh_id)).fetchone()
            if inv:
                conn.execute('UPDATE inventory SET available_qty=available_qty+? WHERE id=?', (initial_stock, inv['id']))
            else:
                conn.execute('INSERT INTO inventory (product_id, warehouse_id, available_qty) VALUES (?, ?, ?)', (product_id, wh_id, initial_stock))
            conn.execute('''
                INSERT INTO stock_moves (product_id, warehouse_id, move_type, qty_change, balance_after, notes, created_at)
                VALUES (?, ?, 'initial', ?, ?, 'Opening stock', datetime('now'))
            ''', (product_id, wh_id, initial_stock, initial_stock))

        conn.commit()
        conn.close()
        log_audit('product', product_id, 'created', {'sku': sku, 'name': name, 'source': 'url_fetch'})
        flash(f'Product {sku} created from URL!', 'success')
        return redirect(url_for('product_view', product_id=product_id))
    except sqlite3.IntegrityError:
        conn.close()
        flash(f'SKU "{sku}" already exists!', 'error')
        return redirect(url_for('product_fetch_from_url'))

@app.route('/products/add', methods=['GET', 'POST'])
def product_add():
    if request.method == 'POST':
        conn = get_db()
        sku = request.form['sku'].strip().upper()
        name = request.form['name'].strip()
        description = request.form.get('description', '')
        category_id = request.form.get('category_id') or None
        unit_id = request.form.get('unit_id') or None
        hsn_code = request.form.get('hsn_code', '')
        cost_price = float(request.form.get('cost_price') or 0)
        selling_price = float(request.form.get('selling_price') or 0)
        mrp = float(request.form.get('mrp') or 0)
        tax_rate = float(request.form.get('tax_rate') or 0)
        reorder_point = int(request.form.get('reorder_point') or 5)
        initial_stock = int(request.form.get('initial_stock') or 0)
        barcode_format = request.form.get('barcode_format', 'GS1-128')
        warehouse_id = request.form.get('warehouse_id')
        image_url = request.form.get('image_url', '')

        try:
            conn.execute('''
                INSERT INTO products (sku, name, description, category_id, unit_id, hsn_code,
                    cost_price, selling_price, mrp, tax_rate, reorder_point, initial_stock, barcode_format, image_url)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (sku, name, description, category_id, unit_id, hsn_code,
                  cost_price, selling_price, mrp, tax_rate, reorder_point, initial_stock, barcode_format, image_url))
            product_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            conn.execute('INSERT INTO product_barcodes (product_id, barcode, barcode_format, is_primary) VALUES (?, ?, ?, 1)',
                (product_id, sku, barcode_format))

            if initial_stock > 0 and warehouse_id:
                wh_id = int(warehouse_id)
                inv = conn.execute('SELECT id FROM inventory WHERE product_id=? AND warehouse_id=?', (product_id, wh_id)).fetchone()
                if inv:
                    conn.execute('UPDATE inventory SET available_qty=available_qty+? WHERE id=?', (initial_stock, inv['id']))
                else:
                    conn.execute('INSERT INTO inventory (product_id, warehouse_id, available_qty) VALUES (?, ?, ?)', (product_id, wh_id, initial_stock))
                conn.execute('''
                    INSERT INTO stock_moves (product_id, warehouse_id, move_type, qty_change, balance_after, notes, created_at)
                    VALUES (?, ?, 'initial', ?, ?, 'Opening stock', datetime('now'))
                ''', (product_id, wh_id, initial_stock, initial_stock))

            conn.commit()
            conn.close()
            log_audit('product', product_id, 'created', {'sku': sku, 'name': name})
            flash(f'Product {sku} created!', 'success')
            return redirect(url_for('product_view', product_id=product_id))
        except sqlite3.IntegrityError:
            conn.close()
            flash(f'SKU "{sku}" already exists!', 'error')
            return redirect(url_for('product_add'))

    conn = get_db()
    categories = conn.execute('SELECT * FROM categories ORDER BY name').fetchall()
    units = conn.execute('SELECT * FROM units ORDER BY name').fetchall()
    warehouses = conn.execute('SELECT * FROM warehouses WHERE is_active=1 ORDER BY name').fetchall()
    conn.close()
    return render_template('products/add.html',
        categories=[dict(c) for c in categories],
        units=[dict(u) for u in units],
        barcode_formats=list(BARCODE_FORMATS.keys()),
        warehouses=[dict(w) for w in warehouses])

@app.route('/products/<int:product_id>')
def product_view(product_id):
    conn = get_db()
    product = conn.execute('SELECT * FROM products WHERE id=?', (product_id,)).fetchone()
    if not product:
        conn.close()
        return "Product not found", 404
    product = dict(product)
    barcodes = conn.execute('SELECT * FROM product_barcodes WHERE product_id=?', (product_id,)).fetchall()
    inventory = get_inventory_summary(product_id)
    moves = conn.execute('''
        SELECT sm.*, w.name as warehouse_name
        FROM stock_moves sm
        JOIN warehouses w ON w.id=sm.warehouse_id
        WHERE sm.product_id=? ORDER BY sm.created_at DESC LIMIT 50
    ''', (product_id,)).fetchall()
    serials = conn.execute('SELECT * FROM serial_numbers WHERE product_id=? ORDER BY created_at DESC LIMIT 20', (product_id,)).fetchall()
    batches = conn.execute('SELECT * FROM batches WHERE product_id=? ORDER BY created_at DESC', (product_id,)).fetchall()
    conn.close()
    return render_template('products/view.html',
        product=product, barcodes=[dict(b) for b in barcodes],
        inventory=inventory, moves=[dict(m) for m in moves],
        serials=[dict(s) for s in serials], batches=[dict(b) for b in batches])

@app.route('/products/<int:product_id>/edit', methods=['GET', 'POST'])
def product_edit(product_id):
    conn = get_db()
    product = conn.execute('SELECT * FROM products WHERE id=?', (product_id,)).fetchone()
    if not product:
        conn.close()
        return "Product not found", 404
    if request.method == 'POST':
        conn.execute('''
            UPDATE products SET
                name=?, description=?, category_id=?, unit_id=?, hsn_code=?,
                cost_price=?, selling_price=?, mrp=?, tax_rate=?,
                reorder_point=?, barcode_format=?, image_url=?,
                updated_at=datetime('now')
            WHERE id=?
        ''', (
            request.form['name'].strip(),
            request.form.get('description', ''),
            request.form.get('category_id') or None,
            request.form.get('unit_id') or None,
            request.form.get('hsn_code', ''),
            float(request.form.get('cost_price') or 0),
            float(request.form.get('selling_price') or 0),
            float(request.form.get('mrp') or 0),
            float(request.form.get('tax_rate') or 0),
            int(request.form.get('reorder_point') or 5),
            request.form.get('barcode_format', 'GS1-128'),
            request.form.get('image_url', ''),
            product_id
        ))
        conn.commit()
        conn.close()
        log_audit('product', product_id, 'updated')
        flash('Product updated!', 'success')
        return redirect(url_for('product_view', product_id=product_id))
    categories = conn.execute('SELECT * FROM categories ORDER BY name').fetchall()
    units = conn.execute('SELECT * FROM units ORDER BY name').fetchall()
    conn.close()
    return render_template('products/edit.html',
        product=dict(product),
        categories=[dict(c) for c in categories],
        units=[dict(u) for u in units],
        barcode_formats=list(BARCODE_FORMATS.keys()))

@app.route('/products/<int:product_id>/delete', methods=['POST'])
def product_delete(product_id):
    conn = get_db()
    conn.execute('UPDATE products SET is_active=0 WHERE id=?', (product_id,))
    conn.commit()
    conn.close()
    log_audit('product', product_id, 'deactivated')
    flash('Product deactivated.', 'warning')
    return redirect(url_for('products'))

@app.route('/products/<int:product_id>/barcode-download')
def product_barcode_download(product_id):
    conn = get_db()
    product = conn.execute('SELECT * FROM products WHERE id=?', (product_id,)).fetchone()
    conn.close()
    if not product:
        return "Product not found", 404
    fname, code_str, fmt = save_barcode_png(dict(product))
    return send_file(
        os.path.join(app.config['BARCODES'], fname),
        mimetype='image/png',
        as_attachment=True,
        download_name=fname
    )

@app.route('/products/barcodes/batch-print', methods=['GET', 'POST'])
def batch_print_barcodes():
    if request.method == 'POST':
        sku_list = [s.strip() for s in request.form.get('skus', '').split('\n') if s.strip()]
        if not sku_list:
            flash('No SKUs provided', 'error')
            return redirect(url_for('batch_print_barcodes'))
        conn = get_db()
        products = []
        for sku in sku_list:
            p = conn.execute('SELECT * FROM products WHERE sku=? AND is_active=1', (sku.upper(),)).fetchone()
            if p:
                fname, code_str, fmt = save_barcode_png(dict(p))
                products.append({'sku': p['sku'], 'name': p['name'], 'barcode_file': fname, 'code': code_str, 'format': fmt})
        conn.close()
        return render_template('products/batch_print.html', products=products)
    return render_template('products/batch_print_form.html')

@app.route('/products/import', methods=['GET', 'POST'])
def import_products():
    if request.method == 'POST':
        file = request.files.get('file')
        if not file:
            flash('No file uploaded', 'error')
            return redirect(url_for('import_products'))
        conn = get_db()
        created = 0
        errors = []
        for line in file.read().decode('utf-8').split('\n'):
            if not line.strip():
                continue
            parts = [p.strip() for p in line.split(',')]
            if len(parts) < 2:
                errors.append(f"Skipping line (too few cols): {line[:50]}")
                continue
            try:
                sku = parts[0].upper()
                name = parts[1]
                cost = float(parts[2]) if len(parts) > 2 and parts[2] else 0
                sell = float(parts[3]) if len(parts) > 3 and parts[3] else 0
                cat = parts[4] if len(parts) > 4 else None
                if cat:
                    cat_row = conn.execute('SELECT id FROM categories WHERE name=?', (cat,)).fetchone()
                    if not cat_row:
                        conn.execute('INSERT INTO categories (name) VALUES (?)', (cat,))
                        cat_row = conn.execute('SELECT last_insert_rowid()').fetchone()
                    cat_id = cat_row[0]
                else:
                    cat_id = None
                conn.execute('''
                    INSERT OR IGNORE INTO products (sku, name, cost_price, selling_price, category_id)
                    VALUES (?, ?, ?, ?, ?)
                ''', (sku, name, cost, sell, cat_id))
                created += 1
            except Exception as e:
                errors.append(f"Error on {parts[0]}: {str(e)}")
        conn.commit()
        conn.close()
        flash(f'{created} products imported!', 'success')
        if errors:
            flash(f'{len(errors)} errors: {"; ".join(errors[:5])}', 'warning')
        return redirect(url_for('products'))
    return render_template('products/import.html')

@app.route('/products/export')
def export_products():
    products = get_products()
    import csv, io as sio
    output = sio.StringIO()
    w = csv.writer(output)
    w.writerow(['SKU', 'Name', 'Category', 'Cost Price', 'Selling Price', 'MRP', 'HSN', 'Tax Rate', 'Total Stock', 'Reorder Point', 'Barcode Format'])
    for p in products:
        w.writerow([p['sku'], p['name'], p.get('category_name', ''), p['cost_price'],
                    p['selling_price'], p['mrp'], p.get('hsn_code', ''), p['tax_rate'],
                    p['total_stock'], p['reorder_point'], p['barcode_format']])
    output.seek(0)
    return send_file(
        sio.BytesIO(output.getvalue().encode('utf-8')),
        mimetype='text/csv',
        as_attachment=True,
        download_name='products_export.csv'
    )

# ── SKU Auto-generate API ──────────────────────────────────────────────────────

@app.route('/api/sku/generate')
def api_sku_generate():
    """Generate a SKU in format SKU-YYYYMMDD-XXXX."""
    import random, string
    date_part = datetime.now().strftime('%Y%m%d')
    rand_part = ''.join(random.choices(string.ascii_uppercase + string.digits, k=4))
    sku = f"SKU-{date_part}-{rand_part}"
    return jsonify({'sku': sku})


# ── Barcode Hub ───────────────────────────────────────────────────────────────

@app.route('/barcodes')
@login_required
def barcode_hub():
    conn = get_db()
    products = conn.execute('''
        SELECT p.id, p.sku, p.name, p.barcode_format, p.selling_price, c.name as category_name
        FROM products p
        LEFT JOIN categories c ON c.id=p.category_id
        WHERE p.is_active=1 ORDER BY p.sku
    ''').fetchall()
    conn.close()
    return render_template('barcodes/hub.html', products=[dict(p) for p in products])

@app.route('/barcodes/generate/<int:product_id>', methods=['GET', 'POST'])
def barcode_generate(product_id):
    conn = get_db()
    product = conn.execute('SELECT * FROM products WHERE id=?', (product_id,)).fetchone()
    conn.close()
    if not product:
        return "Product not found", 404
    product = dict(product)
    fmt = request.args.get('format') or product['barcode_format']
    fname, code_str, actual_fmt = save_barcode_png(product, fmt)
    return render_template('barcodes/generate.html',
        product=product, barcode_file=fname, code_str=code_str,
        actual_format=actual_fmt, barcode_formats=list(BARCODE_FORMATS.keys()))

@app.route('/barcodes/generate/<int:product_id>/qr')
def barcode_generate_qr(product_id):
    conn = get_db()
    product = conn.execute('SELECT * FROM products WHERE id=?', (product_id,)).fetchone()
    conn.close()
    if not product:
        return "Product not found", 404
    product = dict(product)
    data, gs1_str = build_gs1_qr_data(product)
    qr_bytes, _ = generate_qr_gs1(product)
    return send_file(
        io.BytesIO(qr_bytes),
        mimetype='image/png',
        as_attachment=True,
        download_name=f"{product['sku']}_gs1_qr.png"
    )

@app.route('/barcodes/print-label/<int:product_id>', methods=['GET', 'POST'])
def print_label(product_id):
    conn = get_db()
    product = conn.execute('SELECT * FROM products WHERE id=?', (product_id,)).fetchone()
    conn.close()
    if not product:
        return "Product not found", 404
    product = dict(product)
    if request.method == 'POST':
        qty = int(request.form.get('qty') or 1)
        fmt = request.form.get('format') or product['barcode_format']
        conn = get_db()
        existing = conn.execute(
            'SELECT * FROM product_barcodes WHERE product_id=? AND barcode_format=?',
            (product_id, fmt)
        ).fetchone()
        if not existing:
            fname, code_str, actual_fmt = save_barcode_png(product, fmt)
            conn.execute(
                'INSERT INTO product_barcodes (product_id, barcode, barcode_format, is_primary) VALUES (?, ?, ?, 0)',
                (product_id, code_str, fmt)
            )
            conn.commit()
        else:
            fname = f"{product['sku']}_{fmt.lower().replace('-','_')}.png"
        conn.close()
        return render_template('barcodes/label_print.html',
            product=product, qty=qty, barcode_file=fname,
            code_str=existing['barcode'] if existing else code_str,
            format=fmt)
    return render_template('barcodes/label_form.html',
        product=product, barcode_formats=list(BARCODE_FORMATS.keys()))


# ── Warehouses ────────────────────────────────────────────────────────────────

@app.route('/warehouses')
@login_required
def warehouses():
    conn = get_db()
    warehouses_list = conn.execute('''
        SELECT w.*,
               COALESCE(SUM(i.available_qty), 0) as total_items,
               COUNT(DISTINCT i.product_id) as product_count
        FROM warehouses w
        LEFT JOIN inventory i ON i.warehouse_id=w.id
        GROUP BY w.id ORDER BY w.name
    ''').fetchall()
    conn.close()
    return render_template('warehouses/list.html', warehouses=[dict(w) for w in warehouses_list])

@app.route('/warehouses/add', methods=['GET', 'POST'])
def warehouse_add():
    if request.method == 'POST':
        conn = get_db()
        conn.execute('''
            INSERT INTO warehouses (name, code, address, contact_person, phone, email)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (
            request.form['name'].strip(),
            request.form['code'].strip().upper(),
            request.form.get('address', ''),
            request.form.get('contact_person', ''),
            request.form.get('phone', ''),
            request.form.get('email', '')
        ))
        conn.commit()
        conn.close()
        flash('Warehouse created!', 'success')
        return redirect(url_for('warehouses'))
    return render_template('warehouses/add.html')

@app.route('/warehouses/<int:warehouse_id>/view')
def warehouse_view(warehouse_id):
    conn = get_db()
    wh = conn.execute('SELECT * FROM warehouses WHERE id=?', (warehouse_id,)).fetchone()
    if not wh:
        conn.close()
        return "Warehouse not found", 404
    inventory = conn.execute('''
        SELECT i.*, p.sku, p.name as product_name, p.selling_price, p.reorder_point
        FROM inventory i
        JOIN products p ON p.id=i.product_id
        WHERE i.warehouse_id=? AND p.is_active=1
        ORDER BY p.sku
    ''', (warehouse_id,)).fetchall()
    conn.close()
    return render_template('warehouses/view.html',
        warehouse=dict(wh), inventory=[dict(i) for i in inventory])


# ── Suppliers ──────────────────────────────────────────────────────────────────

@app.route('/suppliers')
@login_required
def suppliers():
    conn = get_db()
    suppliers_list = conn.execute('''
        SELECT s.*, COUNT(po.id) as po_count,
               COALESCE(SUM(po.total_amount), 0) as total_spent
        FROM suppliers s
        LEFT JOIN purchase_orders po ON po.supplier_id=s.id
        GROUP BY s.id ORDER BY s.name
    ''').fetchall()
    conn.close()
    return render_template('suppliers/list.html', suppliers=[dict(s) for s in suppliers_list])

@app.route('/suppliers/add', methods=['GET', 'POST'])
def supplier_add():
    if request.method == 'POST':
        conn = get_db()
        code = request.form.get('code', '').strip().upper() or None
        conn.execute('''
            INSERT INTO suppliers (name, code, contact_name, phone, email, address, gstin, tax_no, payment_terms, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            request.form['name'].strip(),
            code,
            request.form.get('contact_name', ''),
            request.form.get('phone', ''),
            request.form.get('email', ''),
            request.form.get('address', ''),
            request.form.get('gstin', ''),
            request.form.get('tax_no', ''),
            request.form.get('payment_terms', ''),
            request.form.get('notes', '')
        ))
        conn.commit()
        conn.close()
        flash('Supplier added!', 'success')
        return redirect(url_for('suppliers'))
    return render_template('suppliers/add.html')

@app.route('/suppliers/<int:supplier_id>/view')
def supplier_view(supplier_id):
    conn = get_db()
    supplier = conn.execute('SELECT * FROM suppliers WHERE id=?', (supplier_id,)).fetchone()
    pos = conn.execute('''
        SELECT po.*, COUNT(poi.id) as item_count
        FROM purchase_orders po
        LEFT JOIN purchase_order_items poi ON poi.po_id=po.id
        WHERE po.supplier_id=?
        GROUP BY po.id ORDER BY po.created_at DESC
    ''', (supplier_id,)).fetchall()
    conn.close()
    if not supplier:
        return "Supplier not found", 404
    return render_template('suppliers/view.html',
        supplier=dict(supplier), purchase_orders=[dict(po) for po in pos])


# ── Purchase Orders ───────────────────────────────────────────────────────────

@app.route('/purchase-orders')
@login_required
def purchase_orders():
    conn = get_db()
    pos = conn.execute('''
        SELECT po.*, s.name as supplier_name, w.name as warehouse_name,
               COUNT(poi.id) as item_count
        FROM purchase_orders po
        LEFT JOIN suppliers s ON s.id=po.supplier_id
        LEFT JOIN warehouses w ON w.id=po.warehouse_id
        LEFT JOIN purchase_order_items poi ON poi.po_id=po.id
        GROUP BY po.id ORDER BY po.created_at DESC
    ''').fetchall()
    conn.close()
    return render_template('purchase_orders/list.html', purchase_orders=[dict(po) for po in pos])

@app.route('/purchase-orders/add', methods=['GET', 'POST'])
def po_add():
    if request.method == 'POST':
        conn = get_db()
        today = datetime.now().strftime('%Y-%m-%d')
        po_number = f"PO-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:4].upper()}"
        conn.execute('''
            INSERT INTO purchase_orders (po_number, supplier_id, warehouse_id, status, order_date, expected_date, notes)
            VALUES (?, ?, ?, 'draft', ?, ?, ?)
        ''', (
            po_number,
            request.form.get('supplier_id'),
            request.form.get('warehouse_id'),
            today,
            request.form.get('expected_date', ''),
            request.form.get('notes', '')
        ))
        po_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        conn.commit()
        conn.close()
        return redirect(url_for('po_edit', po_id=po_id))

    conn = get_db()
    suppliers_list = conn.execute('SELECT * FROM suppliers WHERE is_active=1 ORDER BY name').fetchall()
    warehouses_list = conn.execute('SELECT * FROM warehouses WHERE is_active=1 ORDER BY name').fetchall()
    conn.close()
    return render_template('purchase_orders/add.html',
        suppliers=[dict(s) for s in suppliers_list],
        warehouses=[dict(w) for w in warehouses_list],
        today=datetime.now().strftime('%Y-%m-%d'))

@app.route('/purchase-orders/<int:po_id>/edit', methods=['GET', 'POST'])
def po_edit(po_id):
    conn = get_db()
    po = conn.execute('SELECT * FROM purchase_orders WHERE id=?', (po_id,)).fetchone()
    if not po:
        conn.close()
        return "PO not found", 404
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'update_items':
            item_ids = request.form.getlist('item_id')
            for i, item_id in enumerate(item_ids):
                conn.execute('''
                    UPDATE purchase_order_items SET
                        description=?, ordered_qty=?, unit_price=?, tax_rate=?, discount_pct=?,
                        total=ordered_qty*unit_price*(1+tax_rate/100)*(1-discount_pct/100)
                    WHERE id=?
                ''', (
                    request.form.getlist('description')[i],
                    float(request.form.getlist('ordered_qty')[i]),
                    float(request.form.getlist('unit_price')[i]),
                    float(request.form.getlist('tax_rate')[i]),
                    float(request.form.getlist('discount_pct')[i]),
                    item_id
                ))
            rows = conn.execute('SELECT SUM(total) as subtotal FROM purchase_order_items WHERE po_id=?', (po_id,)).fetchone()
            subtotal = rows['subtotal'] or 0
            tax_amount = conn.execute('SELECT SUM(ordered_qty*unit_price*tax_rate/100) FROM purchase_order_items WHERE po_id=?', (po_id,)).fetchone()[0] or 0
            conn.execute('UPDATE purchase_orders SET subtotal=?, tax_amount=?, total_amount=? WHERE id=?',
                (subtotal, tax_amount, subtotal + tax_amount, po_id))
            conn.commit()
            flash('PO updated!', 'success')
        elif action == 'add_item':
            product_id = request.form.get('product_id')
            if product_id:
                product = conn.execute('SELECT * FROM products WHERE id=?', (product_id,)).fetchone()
                conn.execute('''
                    INSERT INTO purchase_order_items (po_id, product_id, description, unit_id, ordered_qty, unit_price, tax_rate, total)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', (po_id, product_id, product['name'], product['unit_id'], 1, product['cost_price'], product['tax_rate'], product['cost_price']))
                conn.commit()
                flash('Item added!', 'success')
        elif action == 'set_status':
            new_status = request.form.get('status')
            conn.execute('UPDATE purchase_orders SET status=? WHERE id=?', (new_status, po_id))
            conn.commit()
            flash(f'PO status -> {new_status}', 'success')
        conn.close()
        return redirect(url_for('po_edit', po_id=po_id))

    items = conn.execute('''
        SELECT poi.*, p.sku, p.name as product_name, u.name as unit_name
        FROM purchase_order_items poi
        LEFT JOIN products p ON p.id=poi.product_id
        LEFT JOIN units u ON u.id=poi.unit_id
        WHERE poi.po_id=?
    ''', (po_id,)).fetchall()
    suppliers_list = conn.execute('SELECT * FROM suppliers WHERE is_active=1 ORDER BY name').fetchall()
    warehouses_list = conn.execute('SELECT * FROM warehouses WHERE is_active=1 ORDER BY name').fetchall()
    products = conn.execute('SELECT id, sku, name, cost_price, tax_rate FROM products WHERE is_active=1 ORDER BY sku').fetchall()
    conn.close()
    return render_template('purchase_orders/edit.html',
        po=dict(po), items=[dict(i) for i in items],
        suppliers=[dict(s) for s in suppliers_list],
        products=[dict(p) for p in products],
        warehouses=[dict(w) for w in warehouses_list])

@app.route('/purchase-orders/<int:po_id>/receive', methods=['POST'])
def po_receive(po_id):
    conn = get_db()
    po = conn.execute('SELECT * FROM purchase_orders WHERE id=?', (po_id,)).fetchone()
    if not po or po['status'] not in ('draft', 'ordered', 'partial'):
        conn.close()
        flash('PO cannot receive stock in current status', 'error')
        return redirect(url_for('purchase_orders'))

    items = conn.execute('SELECT * FROM purchase_order_items WHERE po_id=?', (po_id,)).fetchall()
    warehouse_id = po['warehouse_id']

    for item in items:
        received = float(request.form.get(f'received_{item["id"]}') or 0)
        if received > 0:
            new_received = item['received_qty'] + received
            conn.execute('UPDATE purchase_order_items SET received_qty=? WHERE id=?', (new_received, item['id']))
            update_inventory(item['product_id'], warehouse_id, received, 'purchase_receive',
                ref_type='po', ref_id=po_id)
            log_audit('po_receive', po_id, 'received', {'product_id': item['product_id'], 'qty': received})

    all_received = all(
        conn.execute('SELECT received_qty >= ordered_qty FROM purchase_order_items WHERE id=?', (item['id'],)).fetchone()[0]
        for item in items
    )
    new_status = 'received' if all_received else 'partial'
    conn.execute('UPDATE purchase_orders SET status=? WHERE id=?', (new_status, po_id))
    conn.commit()
    conn.close()
    flash(f'Received stock. PO status: {new_status}', 'success')
    return redirect(url_for('po_edit', po_id=po_id))


# ── Sales Orders ──────────────────────────────────────────────────────────────

@app.route('/sales-orders')
@login_required
def sales_orders():
    conn = get_db()
    orders = conn.execute('''
        SELECT so.*, COUNT(soi.id) as item_count, w.name as warehouse_name
        FROM sales_orders so
        LEFT JOIN sales_order_items soi ON soi.so_id=so.id
        LEFT JOIN warehouses w ON w.id=so.warehouse_id
        GROUP BY so.id ORDER BY so.created_at DESC
    ''').fetchall()
    conn.close()
    return render_template('sales_orders/list.html', orders=[dict(o) for o in orders])

@app.route('/sales-orders/add', methods=['GET', 'POST'])
def so_add():
    if request.method == 'POST':
        conn = get_db()
        today = datetime.now().strftime('%Y-%m-%d')
        so_number = f"SO-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:4].upper()}"
        warehouse_id = request.form.get('warehouse_id')
        conn.execute('''
            INSERT INTO sales_orders (so_number, customer_name, customer_phone, customer_email,
                customer_address, warehouse_id, status, order_date, notes)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
        ''', (
            so_number,
            request.form.get('customer_name', ''),
            request.form.get('customer_phone', ''),
            request.form.get('customer_email', ''),
            request.form.get('customer_address', ''),
            warehouse_id,
            today,
            request.form.get('notes', '')
        ))
        so_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        conn.commit()
        conn.close()
        return redirect(url_for('so_edit', so_id=so_id))

    conn = get_db()
    products = conn.execute('SELECT id, sku, name, selling_price, tax_rate FROM products WHERE is_active=1 ORDER BY sku').fetchall()
    warehouses = conn.execute('SELECT * FROM warehouses WHERE is_active=1 ORDER BY name').fetchall()
    conn.close()
    return render_template('sales_orders/add.html', products=[dict(p) for p in products], warehouses=[dict(w) for w in warehouses], today=datetime.now().strftime('%Y-%m-%d'))

@app.route('/sales-orders/<int:so_id>/edit', methods=['GET', 'POST'])
def so_edit(so_id):
    conn = get_db()
    so = conn.execute('SELECT * FROM sales_orders WHERE id=?', (so_id,)).fetchone()
    if not so:
        conn.close()
        return "SO not found", 404
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'update_items':
            item_ids = request.form.getlist('item_id')
            for i, item_id in enumerate(item_ids):
                conn.execute('''
                    UPDATE sales_order_items SET
                        description=?, ordered_qty=?, unit_price=?, tax_rate=?, discount_pct=?,
                        total=ordered_qty*unit_price*(1+tax_rate/100)*(1-discount_pct/100)
                    WHERE id=?
                ''', (
                    request.form.getlist('description')[i],
                    float(request.form.getlist('ordered_qty')[i]),
                    float(request.form.getlist('unit_price')[i]),
                    float(request.form.getlist('tax_rate')[i]),
                    float(request.form.getlist('discount_pct')[i]),
                    item_id
                ))
            rows = conn.execute('SELECT SUM(total) as subtotal FROM sales_order_items WHERE so_id=?', (so_id,)).fetchone()
            subtotal = rows['subtotal'] or 0
            tax_amount = conn.execute('SELECT SUM(ordered_qty*unit_price*tax_rate/100) FROM sales_order_items WHERE so_id=?', (so_id,)).fetchone()[0] or 0
            conn.execute('UPDATE sales_orders SET subtotal=?, tax_amount=?, total_amount=? WHERE id=?',
                (subtotal, tax_amount, subtotal + tax_amount, so_id))
            conn.commit()
            flash('Order updated!', 'success')
        elif action == 'add_item':
            product_id = request.form.get('product_id')
            if product_id:
                product = conn.execute('SELECT * FROM products WHERE id=?', (product_id,)).fetchone()
                conn.execute('''
                    INSERT INTO sales_order_items (so_id, product_id, description, unit_id, ordered_qty, unit_price, tax_rate, total)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', (so_id, product_id, product['name'], product['unit_id'], 1, product['selling_price'], product['tax_rate'], product['selling_price']))
                conn.commit()
                flash('Item added!', 'success')
        elif action == 'set_status':
            new_status = request.form.get('status')
            conn.execute('UPDATE sales_orders SET status=? WHERE id=?', (new_status, so_id))
            conn.commit()
            flash(f'Order status -> {new_status}', 'success')
        conn.close()
        return redirect(url_for('so_edit', so_id=so_id))

    items = conn.execute('''
        SELECT soi.*, p.sku, p.name as product_name
        FROM sales_order_items soi
        LEFT JOIN products p ON p.id=soi.product_id
        WHERE soi.so_id=?
    ''', (so_id,)).fetchall()
    products = conn.execute('SELECT id, sku, name, selling_price, tax_rate FROM products WHERE is_active=1 ORDER BY sku').fetchall()
    warehouses = conn.execute('SELECT * FROM warehouses WHERE is_active=1 ORDER BY name').fetchall()
    conn.close()
    return render_template('sales_orders/edit.html',
        so=dict(so), items=[dict(i) for i in items],
        products=[dict(p) for p in products],
        warehouses=[dict(w) for w in warehouses])

@app.route('/sales-orders/<int:so_id>/fulfill', methods=['POST'])
def so_fulfill(so_id):
    conn = get_db()
    so = conn.execute('SELECT * FROM sales_orders WHERE id=?', (so_id,)).fetchone()
    if not so or so['status'] not in ('pending', 'partial'):
        conn.close()
        flash('Order cannot be fulfilled in current status', 'error')
        return redirect(url_for('sales_orders'))

    items = conn.execute('SELECT * FROM sales_order_items WHERE so_id=?', (so_id,)).fetchall()
    warehouse_id = so['warehouse_id']
    all_fulfilled = True

    for item in items:
        fulfill_qty = float(request.form.get(f'fulfill_{item["id"]}') or 0)
        if fulfill_qty > 0:
            new_fulfilled = item['fulfilled_qty'] + fulfill_qty
            conn.execute('UPDATE sales_order_items SET fulfilled_qty=? WHERE id=?', (new_fulfilled, item['id']))
            update_inventory(item['product_id'], warehouse_id, -fulfill_qty, 'sale',
                ref_type='so', ref_id=so_id, notes=f'SO: {so["so_number"]}')
            log_audit('so_fulfill', so_id, 'fulfilled', {'product_id': item['product_id'], 'qty': fulfill_qty})
        if item['fulfilled_qty'] < item['ordered_qty']:
            all_fulfilled = False

    new_status = 'fulfilled' if all_fulfilled else 'partial'
    conn.execute('UPDATE sales_orders SET status=? WHERE id=?', (new_status, so_id))
    conn.commit()
    conn.close()
    flash(f'Order {new_status}!', 'success')
    return redirect(url_for('so_edit', so_id=so_id))


# ── Invoices ──────────────────────────────────────────────────────────────────

@app.route('/invoices')
@login_required
def invoices():
    conn = get_db()
    invoices_list = conn.execute('''
        SELECT i.*, w.name as warehouse_name,
               (SELECT SUM(ii.total) FROM invoice_items ii WHERE ii.invoice_id=i.id) as subtotal_check
        FROM invoices i
        LEFT JOIN warehouses w ON w.id=i.warehouse_id
        ORDER BY i.created_at DESC
    ''').fetchall()
    conn.close()
    return render_template('invoices/list.html', invoices=[dict(i) for i in invoices_list])

@app.route('/invoices/create-from-so/<int:so_id>', methods=['GET', 'POST'])
def invoice_create_from_so(so_id):
    """Create an invoice from a sales order."""
    conn = get_db()
    so = conn.execute('SELECT * FROM sales_orders WHERE id=?', (so_id,)).fetchone()
    if not so:
        conn.close()
        return "SO not found", 404
    so = dict(so)

    if request.method == 'POST':
        invoice_number = f"INV-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:4].upper()}"
        subtotal = float(request.form.get('subtotal') or so.get('subtotal') or 0)
        tax_amount = float(request.form.get('tax_amount') or so.get('tax_amount') or 0)
        discount_amount = float(request.form.get('discount_amount') or 0)
        shipping_amount = float(request.form.get('shipping_amount') or 0)
        total_amount = float(request.form.get('total_amount') or so.get('total_amount') or 0)
        amount_paid = float(request.form.get('amount_paid') or 0)
        payment_status = 'paid' if amount_paid >= total_amount else ('partial' if amount_paid > 0 else 'unpaid')
        payment_method = request.form.get('payment_method', '')
        notes = request.form.get('notes', '')
        invoice_date = request.form.get('invoice_date') or datetime.now().strftime('%Y-%m-%d')
        due_date = request.form.get('due_date', '')

        conn.execute('''
            INSERT INTO invoices (invoice_number, so_id, customer_name, customer_phone, customer_email,
                customer_address, customer_gstin, warehouse_id, invoice_date, due_date,
                subtotal, tax_amount, discount_amount, shipping_amount, total_amount,
                amount_paid, payment_status, payment_method, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            invoice_number, so_id,
            request.form.get('customer_name', so.get('customer_name', '')),
            request.form.get('customer_phone', so.get('customer_phone', '')),
            request.form.get('customer_email', so.get('customer_email', '')),
            request.form.get('customer_address', so.get('customer_address', '')),
            request.form.get('customer_gstin', ''),
            so.get('warehouse_id'),
            invoice_date, due_date,
            subtotal, tax_amount, discount_amount, shipping_amount, total_amount,
            amount_paid, payment_status, payment_method, notes
        ))
        invoice_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]

        # Copy line items from SO
        so_items = conn.execute('SELECT * FROM sales_order_items WHERE so_id=?', (so_id,)).fetchall()
        for item in so_items:
            conn.execute('''
                INSERT INTO invoice_items (invoice_id, product_id, description, hsn_code, unit_id, qty, unit_price, tax_rate, discount_pct, total)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (invoice_id, item['product_id'], item['description'], '', item['unit_id'],
                  item['ordered_qty'], item['unit_price'], item['tax_rate'], item['discount_pct'], item['total']))

        # If paid, decrement stock
        if amount_paid > 0:
            for item in so_items:
                update_inventory(item['product_id'], so['warehouse_id'], -item['ordered_qty'], 'sale',
                    ref_type='invoice', ref_id=invoice_id, notes=f'Invoice: {invoice_number}')

        conn.commit()
        conn.close()
        flash(f'Invoice {invoice_number} created!', 'success')
        return redirect(url_for('invoice_view', invoice_id=invoice_id))

    so_items = conn.execute('SELECT * FROM sales_order_items WHERE so_id=?', (so_id,)).fetchall()
    settings = get_settings_dict()
    conn.close()
    return render_template('invoices/create_from_so.html',
        so=so, so_items=[dict(i) for i in so_items],
        settings=settings)

@app.route('/invoices/create', methods=['GET', 'POST'])
def invoice_create():
    """Direct invoice creation."""
    if request.method == 'POST':
        conn = get_db()
        invoice_number = f"INV-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:4].upper()}"
        invoice_date = request.form.get('invoice_date') or datetime.now().strftime('%Y-%m-%d')
        due_date = request.form.get('due_date', '')
        warehouse_id = request.form.get('warehouse_id')

        conn.execute('''
            INSERT INTO invoices (invoice_number, customer_name, customer_phone, customer_email,
                customer_address, customer_gstin, warehouse_id, invoice_date, due_date,
                subtotal, tax_amount, discount_amount, shipping_amount, total_amount,
                amount_paid, payment_status, payment_method, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            invoice_number,
            request.form.get('customer_name', ''),
            request.form.get('customer_phone', ''),
            request.form.get('customer_email', ''),
            request.form.get('customer_address', ''),
            request.form.get('customer_gstin', ''),
            warehouse_id,
            invoice_date, due_date,
            float(request.form.get('subtotal') or 0),
            float(request.form.get('tax_amount') or 0),
            float(request.form.get('discount_amount') or 0),
            float(request.form.get('shipping_amount') or 0),
            float(request.form.get('total_amount') or 0),
            float(request.form.get('amount_paid') or 0),
            'unpaid', '',
            request.form.get('notes', '')
        ))
        invoice_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        conn.commit()
        conn.close()
        return redirect(url_for('invoice_view', invoice_id=invoice_id))

    conn = get_db()
    warehouses = conn.execute('SELECT * FROM warehouses WHERE is_active=1 ORDER BY name').fetchall()
    products = conn.execute('SELECT id, sku, name, selling_price, tax_rate FROM products WHERE is_active=1 ORDER BY sku').fetchall()
    conn.close()
    return render_template('invoices/create.html',
        warehouses=[dict(w) for w in warehouses],
        products=[dict(p) for p in products])

@app.route('/invoices/<int:invoice_id>')
def invoice_view(invoice_id):
    conn = get_db()
    invoice = conn.execute('SELECT * FROM invoices WHERE id=?', (invoice_id,)).fetchone()
    if not invoice:
        conn.close()
        return "Invoice not found", 404
    invoice = dict(invoice)
    items = conn.execute('SELECT ii.*, p.sku FROM invoice_items ii LEFT JOIN products p ON p.id=ii.product_id WHERE ii.invoice_id=?', (invoice_id,)).fetchall()
    settings = get_settings_dict()
    conn.close()
    return render_template('invoices/view.html',
        invoice=invoice, items=[dict(i) for i in items], settings=settings)

@app.route('/invoices/<int:invoice_id>/record-payment', methods=['POST'])
def invoice_record_payment(invoice_id):
    conn = get_db()
    invoice = conn.execute('SELECT * FROM invoices WHERE id=?', (invoice_id,)).fetchone()
    if not invoice:
        conn.close()
        return "Invoice not found", 404
    amount_paid = float(request.form.get('amount_paid') or 0)
    payment_method = request.form.get('payment_method', '')
    current_paid = invoice['amount_paid'] + amount_paid
    total = invoice['total_amount']
    status = 'paid' if current_paid >= total else ('partial' if current_paid > 0 else 'unpaid')
    conn.execute('UPDATE invoices SET amount_paid=?, payment_status=?, payment_method=? WHERE id=?',
        (current_paid, status, payment_method, invoice_id))
    if amount_paid > 0 and invoice['so_id']:
        so = conn.execute('SELECT * FROM sales_orders WHERE id=?', (invoice['so_id'],)).fetchone()
        if so:
            so_items = conn.execute('SELECT * FROM sales_order_items WHERE so_id=?', (so['id'],)).fetchall()
            for item in so_items:
                update_inventory(item['product_id'], invoice['warehouse_id'], -item['ordered_qty'], 'sale',
                    ref_type='invoice', ref_id=invoice_id, notes=f'Invoice payment: {invoice["invoice_number"]}')
    conn.commit()
    conn.close()
    flash(f'Payment recorded. Status: {status}', 'success')
    return redirect(url_for('invoice_view', invoice_id=invoice_id))


# ── POS ───────────────────────────────────────────────────────────────────────

@app.route('/pos')
@login_required
def pos():
    conn = get_db()
    products = conn.execute('''
        SELECT p.id, p.sku, p.name, p.selling_price, p.tax_rate, p.cost_price,
               COALESCE(SUM(i.available_qty), 0) as stock
        FROM products p
        LEFT JOIN inventory i ON i.product_id=p.id
        WHERE p.is_active=1
        GROUP BY p.id ORDER BY p.name
    ''').fetchall()
    warehouses = conn.execute('SELECT * FROM warehouses WHERE is_active=1 ORDER BY name').fetchall()
    conn.close()
    return render_template('pos.html',
        products=[dict(p) for p in products],
        warehouses=[dict(w) for w in warehouses])

@app.route('/pos/start-session', methods=['POST'])
def pos_start_session():
    conn = get_db()
    session_number = f"POS-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:4].upper()}"
    warehouse_id = request.form.get('warehouse_id')
    cashier = request.form.get('cashier_name', '')
    conn.execute('''
        INSERT INTO pos_sessions (session_number, warehouse_id, status, cashier_name)
        VALUES (?, ?, 'open', ?)
    ''', (session_number, warehouse_id, cashier))
    session_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
    conn.commit()
    conn.close()
    return jsonify({'session_id': session_id, 'session_number': session_number})

@app.route('/pos/<int:pos_session_id>/add-item', methods=['POST'])
def pos_add_item(pos_session_id):
    product_id = int(request.form.get('product_id'))
    qty = float(request.form.get('qty') or 1)
    conn = get_db()
    product = conn.execute('SELECT * FROM products WHERE id=?', (product_id,)).fetchone()
    if not product:
        conn.close()
        return jsonify({'error': 'Product not found'}), 404
    total = qty * product['selling_price'] * (1 + product['tax_rate'] / 100)
    conn.execute('''
        INSERT INTO pos_session_items (session_id, product_id, qty, unit_price, tax_rate, total)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (pos_session_id, product_id, qty, product['selling_price'], product['tax_rate'], total))
    # Update session totals
    rows = conn.execute('SELECT SUM(total) as subtotal, SUM(total * tax_rate/100) as tax FROM pos_session_items WHERE session_id=?', (pos_session_id,)).fetchone()
    conn.execute('UPDATE pos_sessions SET subtotal=?, tax_amount=? WHERE id=?',
        (rows['subtotal'] or 0, rows['tax'] or 0, pos_session_id))
    conn.commit()
    items = conn.execute('''
        SELECT psi.*, p.sku, p.name as product_name
        FROM pos_session_items psi
        JOIN products p ON p.id=psi.product_id
        WHERE psi.session_id=?
    ''', (pos_session_id,)).fetchall()
    session = conn.execute('SELECT * FROM pos_sessions WHERE id=?', (pos_session_id,)).fetchone()
    conn.close()
    return jsonify({
        'items': [dict(i) for i in items],
        'session': dict(session)
    })

@app.route('/pos/<int:pos_session_id>/remove-item/<int:item_id>', methods=['POST'])
def pos_remove_item(pos_session_id, item_id):
    conn = get_db()
    conn.execute('DELETE FROM pos_session_items WHERE id=? AND session_id=?', (item_id, pos_session_id))
    rows = conn.execute('SELECT SUM(total) as subtotal, SUM(total * tax_rate/100) as tax FROM pos_session_items WHERE session_id=?', (pos_session_id,)).fetchone()
    conn.execute('UPDATE pos_sessions SET subtotal=?, tax_amount=? WHERE id=?',
        (rows['subtotal'] or 0, rows['tax'] or 0, pos_session_id))
    conn.commit()
    items = conn.execute('''
        SELECT psi.*, p.sku, p.name as product_name
        FROM pos_session_items psi
        JOIN products p ON p.id=psi.product_id
        WHERE psi.session_id=?
    ''', (pos_session_id,)).fetchall()
    session = conn.execute('SELECT * FROM pos_sessions WHERE id=?', (pos_session_id,)).fetchone()
    conn.close()
    return jsonify({'items': [dict(i) for i in items], 'session': dict(session)})

@app.route('/pos/<int:pos_session_id>/checkout', methods=['POST'])
def pos_checkout(pos_session_id):
    conn = get_db()
    session = conn.execute('SELECT * FROM pos_sessions WHERE id=?', (pos_session_id,)).fetchone()
    if not session:
        conn.close()
        return jsonify({'error': 'Session not found'}), 404

    amount_paid = float(request.form.get('amount_paid') or 0)
    discount_amount = float(request.form.get('discount_amount') or 0)
    payment_method = request.form.get('payment_method', 'cash')
    subtotal = session['subtotal']
    tax_amount = session['tax_amount']
    total_amount = subtotal + tax_amount - discount_amount
    change_amount = max(0, amount_paid - total_amount)

    conn.execute('''
        UPDATE pos_sessions SET
            discount_amount=?, total_amount=?, amount_paid=?,
            change_amount=?, payment_method=?, status='closed',
            closed_at=datetime('now')
        WHERE id=?
    ''', (discount_amount, total_amount, amount_paid, change_amount, payment_method, pos_session_id))

    # Decrement inventory
    items = conn.execute('SELECT * FROM pos_session_items WHERE session_id=?', (pos_session_id,)).fetchall()
    for item in items:
        update_inventory(item['product_id'], session['warehouse_id'], -item['qty'], 'pos_sale',
            ref_type='pos_session', ref_id=pos_session_id,
            notes=f'POS: {session["session_number"]}')

    # Create invoice
    invoice_number = f"INV-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:4].upper()}"
    conn.execute('''
        INSERT INTO invoices (invoice_number, customer_name, warehouse_id, invoice_date,
            subtotal, tax_amount, discount_amount, total_amount, amount_paid,
            payment_status, payment_method, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        invoice_number, 'POS Customer', session['warehouse_id'],
        datetime.now().strftime('%Y-%m-%d'),
        subtotal, tax_amount, discount_amount, total_amount, amount_paid,
        'paid', payment_method, f'POS Session: {session["session_number"]}'
    ))
    invoice_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
    for item in items:
        conn.execute('''
            INSERT INTO invoice_items (invoice_id, product_id, description, qty, unit_price, tax_rate, total)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (invoice_id, item['product_id'], f"{item['product_id']}", item['qty'], item['unit_price'], item['tax_rate'], item['total']))

    conn.commit()
    conn.close()
    return jsonify({
        'success': True,
        'invoice_number': invoice_number,
        'invoice_id': invoice_id,
        'total_amount': total_amount,
        'amount_paid': amount_paid,
        'change_amount': change_amount
    })


# ── Scan Interface ─────────────────────────────────────────────────────────────

@app.route('/scan', methods=['GET', 'POST'])
@login_required
def scan():
    product = None
    error = None
    action_result = None

    if request.method == 'POST':
        identifier = request.form['identifier'].strip()
        action = request.form['action']
        qty = float(request.form.get('qty') or 1)
        warehouse_id = int(request.form.get('warehouse_id') or 1)
        notes = request.form.get('notes', '')
        serial = request.form.get('serial_number', '').strip()
        batch = request.form.get('batch_number', '').strip()
        supplier_id = request.form.get('supplier_id')

        conn = get_db()

        # Try SKU lookup
        product = conn.execute('SELECT * FROM products WHERE sku=? AND is_active=1', (identifier,)).fetchone()
        # Try barcode lookup
        if not product:
            bc = conn.execute('SELECT product_id FROM product_barcodes WHERE barcode=?', (identifier,)).fetchone()
            if bc:
                product = conn.execute('SELECT * FROM products WHERE id=? AND is_active=1', (bc['product_id'],)).fetchone()
        # Try GS1 GTIN extraction
        if not product and identifier.startswith(']C1'):
            gs1_prefix = get_setting('gs1_prefix', '890')
            try:
                gtin_data = identifier[3:17]  # after ]C1, 14 digits
                if len(gtin_data) >= 14:
                    product_ref = gtin_data[len(gs1_prefix):13]
                    product_id = int(product_ref.lstrip('0') or 0)
                    if product_id:
                        product = conn.execute('SELECT * FROM products WHERE id=? AND is_active=1', (product_id,)).fetchone()
            except:
                pass

        if not product:
            conn.close()
            return render_template('scan.html', error=f'Unknown: {identifier}', action=action)

        product = dict(product)

        if action == 'buy':
            # Create purchase order
            po_number = f"PO-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:4].upper()}"
            conn.execute('''
                INSERT INTO purchase_orders (po_number, supplier_id, warehouse_id, status, order_date, subtotal, total_amount)
                VALUES (?, ?, ?, 'draft', ?, 0, 0)
            ''', (supplier_id, warehouse_id, datetime.now().strftime('%Y-%m-%d')))
            po_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            conn.execute('''
                INSERT INTO purchase_order_items (po_id, product_id, description, unit_id, ordered_qty, unit_price, tax_rate, total)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (po_id, product['id'], product['name'], product.get('unit_id'), qty, product['cost_price'], product['tax_rate'], qty * product['cost_price']))
            conn.commit()
            conn.close()
            action_result = {'type': 'buy', 'po_id': po_id, 'po_number': po_number, 'qty': qty, 'product': product}

        elif action == 'sell':
            # Create/fetch sales order and add item
            so_id = request.form.get('so_id')
            if so_id:
                so_id = int(so_id)
            else:
                so_number = f"SO-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:4].upper()}"
                conn.execute('''
                    INSERT INTO sales_orders (so_number, warehouse_id, status, order_date, customer_name)
                    VALUES (?, ?, 'pending', ?, 'Walk-in Customer')
                ''', (so_number, warehouse_id, datetime.now().strftime('%Y-%m-%d')))
                so_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]

            total = qty * product['selling_price'] * (1 + product['tax_rate'] / 100)
            conn.execute('''
                INSERT INTO sales_order_items (so_id, product_id, description, ordered_qty, unit_price, tax_rate, total)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (so_id, product['id'], product['name'], qty, product['selling_price'], product['tax_rate'], total))
            conn.commit()
            conn.close()
            action_result = {'type': 'sell', 'so_id': so_id, 'qty': qty, 'product': product}

        elif action == 'restock':
            update_inventory(product['id'], warehouse_id, qty, 'restock', notes=notes)
            if serial:
                for i in range(int(qty)):
                    try:
                        conn.execute('''
                            INSERT INTO serial_numbers (product_id, serial_number, batch_number, warehouse_id, status)
                            VALUES (?, ?, ?, ?, 'available')
                        ''', (product['id'], f"{serial}-{uuid.uuid4().hex[:4].upper()}", batch or None, warehouse_id))
                    except:
                        pass
                conn.commit()
            conn.close()
            action_result = {'type': 'restock', 'qty': qty, 'product': product}

        elif action == 'adjustment':
            inv = conn.execute('SELECT available_qty FROM inventory WHERE product_id=? AND warehouse_id=?', (product['id'], warehouse_id)).fetchone()
            current = inv['available_qty'] if inv else 0
            change = qty - current
            update_inventory(product['id'], warehouse_id, change, 'manual_adjust', notes=notes)
            conn.close()
            action_result = {'type': 'adjustment', 'qty_before': current, 'qty_after': qty, 'change': change, 'product': product}

        else:
            conn.close()

    return render_template('scan.html', error=error, action_result=action_result)


# ── Stock Transfers ───────────────────────────────────────────────────────────

@app.route('/transfers')
@login_required
def transfers():
    conn = get_db()
    transfers_list = conn.execute('''
        SELECT t.*, fw.name as from_warehouse, tw.name as to_warehouse, COUNT(ti.id) as item_count
        FROM transfer_orders t
        JOIN warehouses fw ON fw.id=t.from_warehouse_id
        JOIN warehouses tw ON tw.id=t.to_warehouse_id
        LEFT JOIN transfer_order_items ti ON ti.transfer_id=t.id
        GROUP BY t.id ORDER BY t.created_at DESC
    ''').fetchall()
    conn.close()
    return render_template('transfers/list.html', transfers=[dict(t) for t in transfers_list])

@app.route('/transfers/add', methods=['GET', 'POST'])
def transfer_add():
    if request.method == 'POST':
        conn = get_db()
        today = datetime.now().strftime('%Y-%m-%d')
        transfer_number = f"TR-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:4].upper()}"
        from_wh = int(request.form['from_warehouse_id'])
        to_wh = int(request.form['to_warehouse_id'])
        if from_wh == to_wh:
            conn.close()
            flash('Source and destination must differ', 'error')
            return redirect(url_for('transfer_add'))

        conn.execute('''
            INSERT INTO transfer_orders (transfer_number, from_warehouse_id, to_warehouse_id, status, sent_date, notes)
            VALUES (?, ?, ?, 'transit', ?, ?)
        ''', (transfer_number, from_wh, to_wh, today, request.form.get('notes', '')))
        transfer_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]

        product_ids = request.form.getlist('product_id')
        qtys = request.form.getlist('qty')
        for pid, qty in zip(product_ids, qtys):
            if pid and float(qty or 0) > 0:
                conn.execute('INSERT INTO transfer_order_items (transfer_id, product_id, qty_sent) VALUES (?, ?, ?)',
                    (transfer_id, int(pid), float(qty)))
                update_inventory(int(pid), from_wh, -float(qty), 'transfer_out', ref_type='transfer', ref_id=transfer_id)
                update_inventory(int(pid), to_wh, float(qty), 'transfer_in', ref_type='transfer', ref_id=transfer_id)
        conn.commit()
        conn.close()
        flash(f'Transfer {transfer_number} created!', 'success')
        return redirect(url_for('transfers'))

    conn = get_db()
    products = conn.execute('SELECT id, sku, name FROM products WHERE is_active=1 ORDER BY sku').fetchall()
    warehouses = conn.execute('SELECT * FROM warehouses WHERE is_active=1 ORDER BY name').fetchall()
    conn.close()
    return render_template('transfers/add.html',
        products=[dict(p) for p in products],
        warehouses=[dict(w) for w in warehouses],
        today=datetime.now().strftime('%Y-%m-%d'))


# ── Stock Adjustments ─────────────────────────────────────────────────────────

@app.route('/adjustments')
@login_required
def adjustments():
    conn = get_db()
    adj = conn.execute('''
        SELECT sa.*, w.name as warehouse_name, COUNT(sai.id) as item_count
        FROM stock_adjustments sa
        JOIN warehouses w ON w.id=sa.warehouse_id
        LEFT JOIN stock_adjustment_items sai ON sai.adjustment_id=sa.id
        GROUP BY sa.id ORDER BY sa.created_at DESC
    ''').fetchall()
    conn.close()
    return render_template('adjustments/list.html', adjustments=[dict(a) for a in adj])

@app.route('/adjustments/add', methods=['GET', 'POST'])
def adjustment_add():
    if request.method == 'POST':
        conn = get_db()
        adj_number = f"SA-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:4].upper()}"
        warehouse_id = int(request.form['warehouse_id'])
        reason = request.form['reason'].strip()
        notes = request.form.get('notes', '')

        conn.execute('''
            INSERT INTO stock_adjustments (adjustment_number, warehouse_id, reason, notes, status, created_at)
            VALUES (?, ?, ?, ?, 'approved', datetime('now'))
        ''', (adj_number, warehouse_id, reason, notes))
        adj_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]

        product_ids = request.form.getlist('product_id')
        qtys_before = request.form.getlist('qty_before')
        qtys_after = request.form.getlist('qty_after')
        for pid, qb, qa in zip(product_ids, qtys_before, qtys_after):
            if not pid:
                continue
            qb = float(qb or 0)
            qa = float(qa or 0)
            change = qa - qb
            if change == 0:
                continue
            conn.execute('INSERT INTO stock_adjustment_items (adjustment_id, product_id, qty_before, qty_after, qty_change) VALUES (?, ?, ?, ?, ?)',
                (adj_id, int(pid), qb, qa, change))
            update_inventory(int(pid), warehouse_id, change, 'adjustment', ref_type='adjustment', ref_id=adj_id, notes=f"{reason}: {notes}")
        conn.commit()
        conn.close()
        flash(f'Adjustment {adj_number} saved!', 'success')
        return redirect(url_for('adjustments'))

    conn = get_db()
    products = conn.execute('SELECT id, sku, name FROM products WHERE is_active=1 ORDER BY sku').fetchall()
    warehouses = conn.execute('SELECT * FROM warehouses WHERE is_active=1 ORDER BY name').fetchall()
    conn.close()
    return render_template('adjustments/add.html',
        products=[dict(p) for p in products],
        warehouses=[dict(w) for w in warehouses])


# ── Cycle Counts ──────────────────────────────────────────────────────────────

@app.route('/cycle-counts')
@login_required
def cycle_counts():
    conn = get_db()
    counts = conn.execute('''
        SELECT sc.*, w.name as warehouse_name, COUNT(sci.id) as item_count
        FROM stock_counts sc
        JOIN warehouses w ON w.id=sc.warehouse_id
        LEFT JOIN stock_count_items sci ON sci.count_id=sc.id
        GROUP BY sc.id ORDER BY sc.created_at DESC
    ''').fetchall()
    conn.close()
    return render_template('cycle_counts/list.html', counts=[dict(c) for c in counts])

@app.route('/cycle-counts/add', methods=['GET', 'POST'])
def cycle_count_add():
    if request.method == 'POST':
        conn = get_db()
        count_number = f"CC-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:4].upper()}"
        warehouse_id = int(request.form['warehouse_id'])
        today = datetime.now().strftime('%Y-%m-%d')
        conn.execute('''
            INSERT INTO stock_counts (warehouse_id, count_number, status, scheduled_date, counted_by, notes)
            VALUES (?, ?, 'in_progress', ?, ?, ?)
        ''', (warehouse_id, count_number, today, request.form.get('counted_by', ''), request.form.get('notes', '')))
        count_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        products = conn.execute('SELECT p.id, COALESCE(i.available_qty, 0) as system_qty FROM products p LEFT JOIN inventory i ON i.product_id=p.id AND i.warehouse_id=? WHERE p.is_active=1', (warehouse_id,)).fetchall()
        for p in products:
            conn.execute('INSERT INTO stock_count_items (count_id, product_id, system_qty, counted_qty, variance) VALUES (?, ?, ?, 0, 0)',
                (count_id, p['id'], p['system_qty']))
        conn.commit()
        conn.close()
        flash(f'Cycle count {count_number} started', 'success')
        return redirect(url_for('cycle_count_view', count_id=count_id))

    conn = get_db()
    warehouses = conn.execute('SELECT * FROM warehouses WHERE is_active=1 ORDER BY name').fetchall()
    conn.close()
    return render_template('cycle_counts/add.html', warehouses=[dict(w) for w in warehouses])

@app.route('/cycle-counts/<int:count_id>/view', methods=['GET', 'POST'])
def cycle_count_view(count_id):
    conn = get_db()
    count = conn.execute('SELECT * FROM stock_counts WHERE id=?', (count_id,)).fetchone()
    if not count:
        conn.close()
        return "Count not found", 404
    if request.method == 'POST':
        counted_vals = request.form.getlist('counted_qty')
        item_ids = request.form.getlist('item_id')
        for item_id, counted in zip(item_ids, counted_vals):
            if not item_id:
                continue
            item = conn.execute('SELECT system_qty, product_id FROM stock_count_items WHERE id=?', (item_id,)).fetchone()
            if item:
                variance = float(counted or 0) - item['system_qty']
                conn.execute('UPDATE stock_count_items SET counted_qty=?, variance=? WHERE id=?',
                    (float(counted or 0), variance, item_id))
                if variance != 0:
                    update_inventory(item['product_id'], count['warehouse_id'], variance, 'cycle_count',
                        ref_type='cycle_count', ref_id=count_id, notes=f'Cycle count {count["count_number"]}')
        conn.execute('UPDATE stock_counts SET status=?, completed_at=datetime("now") WHERE id=?',
            ('completed', count_id))
        conn.commit()
        flash('Cycle count completed!', 'success')
        conn.close()
        return redirect(url_for('cycle_counts'))

    items = conn.execute('SELECT sci.*, p.sku, p.name as product_name FROM stock_count_items sci JOIN products p ON p.id=sci.product_id WHERE sci.count_id=?', (count_id,)).fetchall()
    conn.close()
    return render_template('cycle_counts/view.html', count=dict(count), items=[dict(i) for i in items])


# ── Reports ───────────────────────────────────────────────────────────────────

@app.route('/reports')
@login_required
def reports():
    return render_template('reports/index.html')

@app.route('/reports/inventory-summary')
def report_inventory_summary():
    conn = get_db()
    rows = conn.execute('''
        SELECT p.sku, p.name, p.cost_price, p.selling_price, p.reorder_point,
               COALESCE(SUM(i.available_qty), 0) as available_qty,
               w.name as warehouse_name
        FROM products p
        LEFT JOIN inventory i ON i.product_id=p.id
        LEFT JOIN warehouses w ON w.id=i.warehouse_id
        WHERE p.is_active=1
        GROUP BY p.id, w.id ORDER BY p.sku, w.name
    ''').fetchall()
    conn.close()
    return render_template('reports/inventory_summary.html', rows=[dict(r) for r in rows])

@app.route('/reports/low-stock')
def report_low_stock():
    rows = get_low_stock_products()
    return render_template('reports/low_stock.html', products=rows)

@app.route('/reports/stock-movement')
def report_stock_movement():
    conn = get_db()
    from_date = request.args.get('from_date', (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d'))
    to_date = request.args.get('to_date', datetime.now().strftime('%Y-%m-%d'))
    rows = conn.execute('''
        SELECT sm.*, p.sku, p.name as product_name, w.name as warehouse_name
        FROM stock_moves sm
        JOIN products p ON p.id=sm.product_id
        JOIN warehouses w ON w.id=sm.warehouse_id
        WHERE date(sm.created_at) BETWEEN ? AND ?
        ORDER BY sm.created_at DESC
    ''', (from_date, to_date)).fetchall()
    conn.close()
    return render_template('reports/stock_movement.html', rows=[dict(r) for r in rows], from_date=from_date, to_date=to_date)

@app.route('/reports/valuation')
def report_valuation():
    conn = get_db()
    rows = conn.execute('''
        SELECT p.sku, p.name, c.name as category_name,
               COALESCE(SUM(i.available_qty), 0) as qty,
               p.cost_price,
               (COALESCE(SUM(i.available_qty), 0) * p.cost_price) as total_value,
               p.selling_price,
               (COALESCE(SUM(i.available_qty), 0) * p.selling_price) as total_retail_value
        FROM products p
        LEFT JOIN inventory i ON i.product_id=p.id
        LEFT JOIN categories c ON c.id=p.category_id
        WHERE p.is_active=1
        GROUP BY p.id ORDER BY total_value DESC
    ''').fetchall()
    total_value = sum(r['total_value'] or 0 for r in rows)
    total_retail = sum(r['total_retail_value'] or 0 for r in rows)
    conn.close()
    return render_template('reports/valuation.html', rows=[dict(r) for r in rows], total_value=total_value, total_retail=total_retail)

@app.route('/reports/supplier-summary')
def report_supplier_summary():
    conn = get_db()
    rows = conn.execute('''
        SELECT s.name, s.code, s.gstin, s.email,
               COUNT(DISTINCT po.id) as po_count,
               COALESCE(SUM(po.total_amount), 0) as total_spent,
               COALESCE(AVG(po.total_amount), 0) as avg_order
        FROM suppliers s
        LEFT JOIN purchase_orders po ON po.supplier_id=s.id
        GROUP BY s.id ORDER BY total_spent DESC
    ''').fetchall()
    conn.close()
    return render_template('reports/supplier_summary.html', suppliers=[dict(r) for r in rows])

@app.route('/reports/sales-summary')
def report_sales_summary():
    conn = get_db()
    from_date = request.args.get('from_date', (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d'))
    to_date = request.args.get('to_date', datetime.now().strftime('%Y-%m-%d'))
    rows = conn.execute('''
        SELECT so.so_number, so.customer_name, so.order_date, so.status,
               so.subtotal, so.tax_amount, so.total_amount,
               w.name as warehouse_name, COUNT(soi.id) as item_count
        FROM sales_orders so
        LEFT JOIN sales_order_items soi ON soi.so_id=so.id
        LEFT JOIN warehouses w ON w.id=so.warehouse_id
        WHERE date(so.order_date) BETWEEN ? AND ?
        GROUP BY so.id ORDER BY so.order_date DESC
    ''', (from_date, to_date)).fetchall()
    totals = conn.execute('''
        SELECT COUNT(*) as order_count, SUM(subtotal) as revenue, SUM(tax_amount) as tax
        FROM sales_orders WHERE date(order_date) BETWEEN ? AND ?
    ''', (from_date, to_date)).fetchone()
    conn.close()
    return render_template('reports/sales_summary.html',
        orders=[dict(r) for r in rows], from_date=from_date, to_date=to_date,
        totals=dict(totals) if totals else {})


# ── Settings ──────────────────────────────────────────────────────────────────

@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    if request.method == 'POST':
        for key in ['company_name', 'gs1_prefix', 'currency', 'currency_symbol', 'tax_number', 'address', 'phone', 'email',
                    'barcode_format_default', 'low_stock_email_enabled',
                    'email_provider', 'smtp_host', 'smtp_port',
                    'smtp_username', 'smtp_password',
                    'smtp_from_email', 'smtp_to_email', 'email_brevo_key', 'email_use_tls']:
            val = request.form.get(key, '').strip()
            if key == 'email_smtp_port':
                val = str(val)
            update_setting(key, val)
        flash('Settings saved!', 'success')
        return redirect(url_for('settings'))
    settings_dict = get_settings_dict()
    conn = get_db()
    units = conn.execute('SELECT * FROM units ORDER BY name').fetchall()
    categories = conn.execute('SELECT * FROM categories ORDER BY name').fetchall()
    conn.close()
    return render_template('settings/index.html',
        settings=settings_dict, units=[dict(u) for u in units], categories=[dict(c) for c in categories])

@app.route('/api/email/test', methods=['POST'])
def api_email_test():
    """Send a test email. Accepts optional recipient in JSON body."""
    data = request.get_json(force=True) or {}
    to_email = data.get('to')
    success, message = send_test_email(to_email)
    if success:
        return jsonify({'status': 'ok', 'message': message})
    return jsonify({'status': 'error', 'message': message}), 400

@app.route('/settings/units/add', methods=['POST'])
def unit_add():
    conn = get_db()
    conn.execute('INSERT INTO units (name, code, is_decimal) VALUES (?, ?, ?)',
        (request.form['name'].strip(), request.form['code'].strip().upper(),
         1 if request.form.get('is_decimal') else 0))
    conn.commit()
    conn.close()
    flash('Unit added!', 'success')
    return redirect(url_for('settings'))

@app.route('/settings/categories/add', methods=['POST'])
def category_add():
    conn = get_db()
    conn.execute('INSERT INTO categories (name, description) VALUES (?, ?)',
        (request.form['name'].strip(), request.form.get('description', '')))
    conn.commit()
    conn.close()
    flash('Category added!', 'success')
    return redirect(url_for('settings'))

@app.route('/settings/ollama', methods=['GET', 'POST'])
@login_required
def settings_ollama():
    uid_val = uid()
    if request.method == 'POST':
        model = request.form.get('model', 'llama3').strip()
        base_url = request.form.get('base_url', 'http://localhost:11434').strip()
        update_setting('ollama_model', model, uid_val)
        update_setting('ollama_base_url', base_url, uid_val)
        flash('Ollama settings saved!', 'success')
        return redirect(url_for('settings'))

    current_model = get_setting('ollama_model', 'llama3', uid_val)
    current_url = get_setting('ollama_base_url', 'http://localhost:11434', uid_val)

    # Test connection
    status = 'unknown'
    try:
        import urllib.request
        req = urllib.request.Request(f"{current_url}/api/tags")
        with urllib.request.urlopen(req, timeout=5) as r:
            status = 'connected' if r.status == 200 else f'error({r.status})'
    except:
        status = 'disconnected'

    return render_template('settings/ollama.html',
                           ollama_status=status,
                           current_model=current_model,
                           current_url=current_url)


# ── API ───────────────────────────────────────────────────────────────────────

# ── AI Routes ────────────────────────────────────────────────────────────────

@app.route('/api/ai/generate-description', methods=['POST'])
@login_required
def ai_generate_description():
    """Use Ollama to generate a product description from name/category."""
    name = request.form.get('name', '')
    category = request.form.get('category', '')
    if not name:
        return jsonify({'error': 'Product name required'}), 400

    prompt = f"Write a compelling, concise product description for: {name} (Category: {category}). Keep it under 150 characters, e-commerce friendly."
    desc = ollama_chat(prompt, system_prompt="You are a helpful e-commerce copywriter.")
    return jsonify({'description': desc or 'Description unavailable.'})

@app.route('/api/ai/stock-report', methods=['GET'])
@login_required
def ai_stock_report():
    """Generate an AI-powered stock insights report."""
    conn = get_db()
    low_stock_q = conn.execute("""
        SELECT p.sku, p.name, p.reorder_point, COALESCE(SUM(i.available_qty), 0) as qty
        FROM products p LEFT JOIN inventory i ON i.product_id=p.id
        WHERE p.is_active=1 GROUP BY p.id HAVING qty <= p.reorder_point
    """).fetchall()
    out_of_stock = [dict(r) for r in low_stock_q if r['qty'] == 0]
    low = [dict(r) for r in low_stock_q if r['qty'] > 0]

    prompt = f"""
You are a retail inventory analyst. Based on this data:
- Out of Stock ({len(out_of_stock)} items): {[r['name'] for r in out_of_stock[:10]]}
- Low Stock ({len(low)} items): {[f"{r['name']}({r['qty']}/{r['reorder_point']})" for r in low[:10]]}

Provide: 1) Top 3 restocking priorities, 2) Quick insights, 3) Suggested actions.
Keep it concise and actionable.
"""
    insights = ollama_chat(prompt, system_prompt="You are a retail inventory expert assistant.")
    return jsonify({'insights': insights or 'AI insights unavailable. Ensure Ollama is running.',
                    'out_of_stock': out_of_stock, 'low_stock': low})

@app.route('/api/ai/search', methods=['GET'])
@login_required
def ai_smart_search():
    """Semantic product search using Ollama."""
    q = request.args.get('q', '').strip()
    if not q or len(q) < 2:
        return jsonify({'results': []})

    conn = get_db()
    exact = conn.execute("""
        SELECT p.*, COALESCE(SUM(i.available_qty), 0) as total_stock
        FROM products p LEFT JOIN inventory i ON i.product_id=p.id
        WHERE p.is_active=1 AND (p.name LIKE ? OR p.sku LIKE ?)
        GROUP BY p.id LIMIT 20
    """, (f'%{q}%', f'%{q}%')).fetchall()

    if exact:
        return jsonify({'results': [dict(r) for r in exact], 'type': 'exact'})

    # Fall back to Ollama semantic search
    products = conn.execute("SELECT id, name, sku, category_id FROM products WHERE is_active=1").fetchall()
    product_list = "\n".join([f"- ID {r['id']}: {r['name']} (SKU: {r['sku']})" for r in products])

    prompt = f"From this product list, find the best matches for: '{q}'\n{product_list[:2000]}\n\nReturn ONLY a JSON array of matching product IDs, e.g. [1,2,3]. No explanation."

    try:
        import re
        raw = ollama_chat(prompt)
        ids = re.findall(r'\d+', raw or '')
        if ids:
            placeholders = ','.join('?' * len(ids))
            sem_results = conn.execute(f"SELECT p.*, COALESCE(SUM(i.available_qty), 0) as total_stock FROM products p LEFT JOIN inventory i ON i.product_id=p.id WHERE p.id IN ({placeholders}) GROUP BY p.id", ids).fetchall()
            return jsonify({'results': [dict(r) for r in sem_results], 'type': 'ai'})
    except:
        pass

    return jsonify({'results': [], 'type': 'none'})

@app.route('/api/products')
def api_products():
    conn = get_db()
    rows = conn.execute('''
        SELECT p.id, p.sku, p.name, p.selling_price, p.cost_price,
               COALESCE(SUM(i.available_qty), 0) as stock
        FROM products p
        LEFT JOIN inventory i ON i.product_id=p.id
        WHERE p.is_active=1
        GROUP BY p.id ORDER BY p.sku
    ''').fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/barcode/<path:barcode>')
def api_barcode_lookup(barcode):
    conn = get_db()
    product = conn.execute('SELECT * FROM products WHERE sku=? AND is_active=1', (barcode,)).fetchone()
    if not product:
        bc = conn.execute('SELECT product_id FROM product_barcodes WHERE barcode=?', (barcode,)).fetchone()
        if bc:
            product = conn.execute('SELECT * FROM products WHERE id=? AND is_active=1', (bc['product_id'],)).fetchone()
    # GS1 QR decode
    if not product and barcode.startswith(']C1'):
        gs1_prefix = get_setting('gs1_prefix', '890')
        try:
            gtin_data = barcode[3:17]
            if len(gtin_data) >= 14:
                product_ref = gtin_data[len(gs1_prefix):13]
                product_id = int(product_ref.lstrip('0') or 0)
                if product_id:
                    product = conn.execute('SELECT * FROM products WHERE id=? AND is_active=1', (product_id,)).fetchone()
        except:
            pass
    conn.close()
    if product:
        return jsonify({'found': True, 'product': dict(product)})
    return jsonify({'found': False, 'barcode': barcode}), 404

@app.route('/api/check-low-stock-email')
def api_check_low_stock_email():
    """External endpoint for cron-based low stock email check. Sends synchronously."""
    from_email = get_setting('smtp_from_email', '')
    if not from_email or get_setting('low_stock_email_enabled') != '1':
        return jsonify({'status': 'skipped', 'message': 'Email alerts disabled or no recipient'})

    products = get_low_stock_products()
    if not products:
        return jsonify({'status': 'ok', 'message': 'No low stock items'})

    company = get_setting('company_name', 'Store Manager')
    currency = get_setting('currency_symbol', 'Rs.')
    subject = f"[{company}] Low Stock Alert — {len(products)} item(s)"
    html = _low_stock_email_html(products, company, currency)
    try:
        _do_send_email(from_email, subject, html)
        return jsonify({'status': 'ok', 'message': f'Low stock email sent to {from_email}'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/lookup')
def api_lookup():
    """Universal product lookup by SKU, barcode, or GS1 data."""
    identifier = request.args.get('q', '').strip()
    if not identifier:
        return jsonify({'found': False, 'error': 'No identifier provided'}), 400

    conn = get_db()
    product = conn.execute('SELECT * FROM products WHERE sku=? AND is_active=1', (identifier,)).fetchone()
    if not product:
        bc = conn.execute('SELECT product_id FROM product_barcodes WHERE barcode=?', (identifier,)).fetchone()
        if bc:
            product = conn.execute('SELECT * FROM products WHERE id=? AND is_active=1', (bc['product_id'],)).fetchone()
    if not product and identifier.startswith(']C1'):
        gs1_prefix = get_setting('gs1_prefix', '890')
        try:
            gtin_data = identifier[3:17]
            if len(gtin_data) >= 14:
                product_ref = gtin_data[len(gs1_prefix):13]
                product_id = int(product_ref.lstrip('0') or 0)
                if product_id:
                    product = conn.execute('SELECT * FROM products WHERE id=? AND is_active=1', (product_id,)).fetchone()
        except:
            pass
    conn.close()

    if product:
        p = dict(product)
        inv = get_inventory_summary(p['id'])
        p['inventory'] = inv
        return jsonify({'found': True, 'product': p})
    return jsonify({'found': False, 'identifier': identifier}), 404


# ─── Integration Hub ──────────────────────────────────────────────────────────

@app.route('/integrations')
@login_required
def integrations():
    conn = get_db()
    ints = conn.execute('SELECT * FROM store_integrations WHERE user_id=?', (current_user.id,)).fetchall()
    api_keys = conn.execute('SELECT * FROM api_keys WHERE user_id=?', (current_user.id,)).fetchall()
    conn.close()
    return render_template('integrations/index.html',
        integrations=[dict(i) for i in ints],
        api_keys=[dict(k) for k in api_keys])

@app.route('/integrations/generate-api-key', methods=['POST'])
@login_required
def generate_api_key_route():
    name = request.form.get('name', 'Default Key').strip()
    key = generate_api_key()
    key_hash = hash_api_key(key)
    conn = get_db()
    conn.execute('INSERT INTO api_keys (user_id, name, key_hash) VALUES (?, ?, ?)',
        (current_user.id, name, key_hash))
    conn.commit()
    conn.close()
    flash(f'API Key created: {key} -- Copy this key now, it will not be shown again.', 'success')
    return redirect(url_for('integrations'))

@app.route('/integrations/revoke-api-key/<int:key_id>', methods=['POST'])
@login_required
def revoke_api_key(key_id):
    conn = get_db()
    conn.execute('DELETE FROM api_keys WHERE id=? AND user_id=?', (key_id, current_user.id))
    conn.commit()
    conn.close()
    flash('API key revoked.', 'success')
    return redirect(url_for('integrations'))

@app.route('/integrations/shopify', methods=['GET', 'POST'])
@login_required
def integration_shopify():
    if request.method == 'POST':
        store_url = request.form['store_url'].strip().rstrip('/')
        api_key = request.form['api_key'].strip()
        access_token = request.form['access_token'].strip()
        if not store_url or not access_token:
            flash('Store URL and Access Token are required.', 'error')
            return redirect(url_for('integration_shopify'))
        conn = get_db()
        conn.execute('''
            INSERT OR REPLACE INTO store_integrations
            (user_id, platform, store_url, api_key, access_token, is_active)
            VALUES (?, ?, ?, ?, ?, 1)
        ''', (current_user.id, 'shopify', store_url, api_key, access_token))
        conn.execute('UPDATE store_integrations SET last_sync=datetime("now") WHERE user_id=? AND platform=?',
            (current_user.id, 'shopify'))
        conn.commit()
        conn.close()
        flash('Shopify store connected successfully!', 'success')
        return redirect(url_for('integrations'))
    return render_template('integrations/shopify.html')

@app.route('/integrations/woocommerce', methods=['GET', 'POST'])
@login_required
def integration_woocommerce():
    if request.method == 'POST':
        store_url = request.form['store_url'].strip().rstrip('/')
        consumer_key = request.form['consumer_key'].strip()
        consumer_secret = request.form['consumer_secret'].strip()
        if not store_url or not consumer_key:
            flash('Store URL and Consumer Key are required.', 'error')
            return redirect(url_for('integration_woocommerce'))
        conn = get_db()
        conn.execute('''
            INSERT OR REPLACE INTO store_integrations
            (user_id, platform, store_url, api_key, api_secret, is_active)
            VALUES (?, ?, ?, ?, ?, 1)
        ''', (current_user.id, 'woocommerce', store_url, consumer_key, consumer_secret))
        conn.execute('UPDATE store_integrations SET last_sync=datetime("now") WHERE user_id=? AND platform=?',
            (current_user.id, 'woocommerce'))
        conn.commit()
        conn.close()
        flash('WooCommerce store connected successfully!', 'success')
        return redirect(url_for('integrations'))
    return render_template('integrations/woocommerce.html')

@app.route('/integrations/import-products', methods=['POST'])
@login_required
def integration_import_products():
    conn = get_db()
    store = conn.execute('SELECT * FROM store_integrations WHERE user_id=? AND is_active=1',
        (current_user.id,)).fetchone()
    conn.close()
    if not store:
        return jsonify({'error': 'No active store integration found.'}), 400

    count = 0
    try:
        if store['platform'] == 'shopify':
            count = _import_shopify_products(dict(store))
        elif store['platform'] == 'woocommerce':
            count = _import_woocommerce_products(dict(store))
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    return jsonify({'success': True, 'imported': count})

@app.route('/integrations/disconnect/<int:integration_id>', methods=['POST'])
@login_required
def integration_disconnect(integration_id):
    conn = get_db()
    conn.execute('DELETE FROM store_integrations WHERE id=? AND user_id=?', (integration_id, current_user.id))
    conn.commit()
    conn.close()
    flash('Integration disconnected.', 'success')
    return redirect(url_for('integrations'))

def _import_shopify_products(store):
    """Import products from a Shopify store via Storefront API."""
    importbase = store['store_url']
    if not importbase.startswith('http'):
        importbase = 'https://' + importbase
    token = store.get('access_token', '')
    url = f"{importbase}/admin/api/2024-01/products.json"
    headers = {'X-Shopify-Access-Token': token} if token else {}
    resp = requests.get(url, headers=headers, timeout=15)
    if resp.status_code != 200:
        raise Exception(f"Shopify API error: {resp.status_code} {resp.text[:200]}")
    data = resp.json()
    conn = get_db()
    count = 0
    for item in data.get('products', []):
        for variant in item.get('variants', []):
            sku = variant.get('sku') or f"SHP-{variant['id']}"
            name = item.get('title', 'Unnamed')
            desc = item.get('body_html', '') or ''
            cost = float(variant.get('price', 0) or 0)
            sell = float(variant.get('price', 0) or 0)
            existing = conn.execute('SELECT id FROM products WHERE sku=? AND user_id=?', (sku, current_user.id)).fetchone()
            if not existing:
                conn.execute('''
                    INSERT INTO products (user_id, sku, name, description, cost_price, selling_price, is_active)
                    VALUES (?, ?, ?, ?, ?, ?, 1)
                ''', (current_user.id, sku, name, desc, cost, sell))
                count += 1
    conn.commit()
    conn.close()
    return count

def _import_woocommerce_products(store):
    """Import products from a WooCommerce store via REST API."""
    base = store['store_url']
    if not base.startswith('http'):
        base = 'https://' + base
    ck = store.get('api_key', '')
    cs = store.get('api_secret', '')
    url = f"{base}/wp-json/wc/v3/products"
    resp = requests.get(url, params={'consumer_key': ck, 'consumer_secret': cs}, timeout=15)
    if resp.status_code != 200:
        raise Exception(f"WooCommerce API error: {resp.status_code} {resp.text[:200]}")
    data = resp.json()
    conn = get_db()
    count = 0
    for item in data:
        sku = item.get('sku') or f"WC-{item['id']}"
        name = item.get('name', 'Unnamed')
        desc = item.get('description', '') or item.get('short_description', '') or ''
        price = float(item.get('price', 0) or 0)
        existing = conn.execute('SELECT id FROM products WHERE sku=? AND user_id=?', (sku, current_user.id)).fetchone()
        if not existing:
            conn.execute('''
                INSERT INTO products (user_id, sku, name, description, cost_price, selling_price, is_active)
                VALUES (?, ?, ?, ?, ?, ?, 1)
            ''', (current_user.id, sku, name, desc, price, price))
            count += 1
    conn.commit()
    conn.close()
    return count


# ─── API v1 (Open API with Bearer Token) ───────────────────────────────────────

def _api_require_key():
    """Validate API key from Authorization: Bearer header. Returns (user_id, error_response)."""
    api_key = request.headers.get('Authorization', '').replace('Bearer ', '')
    if not api_key:
        return None, (jsonify({'error': 'API key required', 'hint': 'Authorization: Bearer <your-api-key>'}), 401)
    user_id = validate_api_key(api_key)
    if not user_id:
        return None, (jsonify({'error': 'Invalid API key'}), 401)
    return user_id, None

@app.route('/api/v1/products', methods=['GET'])
def api_v1_products():
    user_id, err = _api_require_key()
    if err:
        return err
    conn = get_db()
    products = conn.execute('''
        SELECT p.*, COALESCE(SUM(i.available_qty), 0) as stock
        FROM products p
        LEFT JOIN inventory i ON i.product_id=p.id
        WHERE p.user_id=? AND p.is_active=1
        GROUP BY p.id ORDER BY p.sku
    ''', (user_id,)).fetchall()
    conn.close()
    return jsonify([dict(p) for p in products])

@app.route('/api/v1/products', methods=['POST'])
def api_v1_create_product():
    user_id, err = _api_require_key()
    if err:
        return err
    data = request.get_json() or {}
    sku = data.get('sku')
    if not sku:
        return jsonify({'error': 'sku is required'}), 400
    conn = get_db()
    existing = conn.execute('SELECT id FROM products WHERE sku=? AND user_id=?', (sku, user_id)).fetchone()
    if existing:
        conn.close()
        return jsonify({'error': 'Product with this SKU already exists'}), 409
    conn.execute('''
        INSERT INTO products (user_id, sku, name, description, cost_price, selling_price, reorder_point, is_active)
        VALUES (?, ?, ?, ?, ?, ?, ?, 1)
    ''', (
        user_id,
        sku,
        data.get('name', ''),
        data.get('description', ''),
        float(data.get('cost_price', 0) or 0),
        float(data.get('selling_price', 0) or 0),
        float(data.get('reorder_point', 0) or 0),
    ))
    new_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'product_id': new_id}), 201

@app.route('/api/v1/products/<int:product_id>', methods=['GET'])
def api_v1_get_product(product_id):
    user_id, err = _api_require_key()
    if err:
        return err
    conn = get_db()
    product = conn.execute('SELECT * FROM products WHERE id=? AND user_id=?', (product_id, user_id)).fetchone()
    if not product:
        conn.close()
        return jsonify({'error': 'Product not found'}), 404
    inv = conn.execute('SELECT * FROM inventory WHERE product_id=?', (product_id,)).fetchall()
    conn.close()
    result = dict(product)
    result['inventory'] = [dict(i) for i in inv]
    return jsonify(result)

@app.route('/api/v1/inventory/<int:product_id>', methods=['GET'])
def api_v1_inventory(product_id):
    user_id, err = _api_require_key()
    if err:
        return err
    conn = get_db()
    product = conn.execute('SELECT id FROM products WHERE id=? AND user_id=?', (product_id, user_id)).fetchone()
    if not product:
        conn.close()
        return jsonify({'error': 'Product not found'}), 404
    rows = conn.execute('''
        SELECT i.*, w.name as warehouse_name
        FROM inventory i
        JOIN warehouses w ON w.id=i.warehouse_id
        WHERE i.product_id=?
    ''', (product_id,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/v1/inventory/<int:product_id>/adjust', methods=['POST'])
def api_v1_adjust_inventory(product_id):
    user_id, err = _api_require_key()
    if err:
        return err
    data = request.get_json() or {}
    qty_change = float(data.get('qty_change', 0))
    move_type = data.get('move_type', 'api_adjust')
    notes = data.get('notes', '')
    warehouse_id = int(data.get('warehouse_id', 1))
    conn = get_db()
    product = conn.execute('SELECT id FROM products WHERE id=? AND user_id=?', (product_id, user_id)).fetchone()
    if not product:
        conn.close()
        return jsonify({'error': 'Product not found'}), 404
    update_inventory(product_id, warehouse_id, qty_change, move_type, notes=notes)
    conn.close()
    return jsonify({'success': True, 'product_id': product_id, 'adjustment': qty_change})

@app.route('/api/v1/orders', methods=['GET', 'POST'])
def api_v1_orders():
    user_id, err = _api_require_key()
    if err:
        return err
    conn = get_db()
    if request.method == 'GET':
        rows = conn.execute('''
            SELECT so.*, COUNT(soi.id) as item_count
            FROM sales_orders so
            LEFT JOIN sales_order_items soi ON soi.so_id=so.id
            WHERE so.user_id=?
            GROUP BY so.id ORDER BY so.created_at DESC
        ''', (user_id,)).fetchall()
        conn.close()
        return jsonify([dict(r) for r in rows])
    # POST: create order
    data = request.get_json() or {}
    so_number = f"SO-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:4].upper()}"
    warehouse_id = int(data.get('warehouse_id', 1))
    customer = data.get('customer_name', 'API Customer')
    conn.execute('''
        INSERT INTO sales_orders (user_id, so_number, warehouse_id, status, order_date, customer_name)
        VALUES (?, ?, ?, 'pending', ?, ?)
    ''', (user_id, so_number, warehouse_id, datetime.now().strftime('%Y-%m-%d'), customer))
    so_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
    items = data.get('items', [])
    subtotal = 0
    for item in items:
        pid = int(item.get('product_id', 0))
        qty = float(item.get('qty', 1))
        price = float(item.get('unit_price', 0))
        tax = float(item.get('tax_rate', 0))
        total = qty * price * (1 + tax / 100)
        conn.execute('''
            INSERT INTO sales_order_items (so_id, product_id, ordered_qty, unit_price, tax_rate, total)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (so_id, pid, qty, price, tax, total))
        subtotal += total
        update_inventory(pid, warehouse_id, -qty, 'sold', ref_type='sales_order', ref_id=so_id)
    tax_amount = subtotal * 0.18
    conn.execute('UPDATE sales_orders SET subtotal=?, tax_amount=?, total_amount=? WHERE id=?',
        (subtotal, tax_amount, subtotal + tax_amount, so_id))
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'order_id': so_id, 'so_number': so_number}), 201

@app.route('/api/v1/invoices', methods=['GET', 'POST'])
def api_v1_invoices():
    user_id, err = _api_require_key()
    if err:
        return err
    conn = get_db()
    if request.method == 'GET':
        rows = conn.execute('''
            SELECT i.*, so.so_number
            FROM invoices i
            LEFT JOIN sales_orders so ON so.id=i.so_id
            WHERE i.user_id=?
            ORDER BY i.created_at DESC
        ''', (user_id,)).fetchall()
        conn.close()
        return jsonify([dict(r) for r in rows])
    data = request.get_json() or {}
    inv_number = f"INV-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:4].upper()}"
    so_id = data.get('so_id')
    conn.execute('''
        INSERT INTO invoices (user_id, invoice_number, so_id, invoice_date, due_date, subtotal, tax_amount, total_amount, payment_status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'unpaid')
    ''', (
        user_id, inv_number, so_id or None,
        datetime.now().strftime('%Y-%m-%d'),
        data.get('due_date', (datetime.now() + timedelta(days=30)).strftime('%Y-%m-%d')),
        float(data.get('subtotal', 0)),
        float(data.get('tax_amount', 0)),
        float(data.get('total_amount', 0)),
    ))
    new_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'invoice_id': new_id, 'invoice_number': inv_number}), 201


# ─── API Docs Page ────────────────────────────────────────────────────────────

@app.route('/api/docs')
def api_docs():
    return render_template('api_docs.html')


# ─── Run ──────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    init_db()
    port = int(os.environ.get('PORT', 5050))
    app.run(host='0.0.0.0', port=port, debug=False)
