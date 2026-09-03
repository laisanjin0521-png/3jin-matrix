import os
import re
import io
import json
import time
import base64
import sqlite3
import datetime
import zipfile
import threading
import mimetypes
import urllib.request
import urllib.error
from flask import Flask, request, jsonify, send_file, render_template_string, Response, make_response

app = Flask(__name__)
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'distributor.db')
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
MATERIALS_DIR = os.path.join(PROJECT_ROOT, 'materials')

# Beijing Time (UTC+8) Configuration
BEIJING_TZ = datetime.timezone(datetime.timedelta(hours=8))

def get_beijing_now():
    return datetime.datetime.now(BEIJING_TZ)

def get_beijing_now_str():
    return get_beijing_now().strftime('%Y-%m-%d %H:%M:%S')

def get_beijing_today_str():
    return get_beijing_now().strftime('%Y-%m-%d')

def parse_beijing_time(time_str):
    if not time_str:
        return None
    try:
        dt = datetime.datetime.strptime(time_str, '%Y-%m-%d %H:%M:%S')
        return dt.replace(tzinfo=BEIJING_TZ)
    except Exception:
        return None

# Turso Cloud Database Configuration (AWS Tokyo)
TURSO_DATABASE_URL = os.environ.get(
    "TURSO_DATABASE_URL",
    "https://thomas-jin-laisanjin0521-png.aws-ap-northeast-1.turso.io/v2/pipeline"
)
TURSO_AUTH_TOKEN = os.environ.get(
    "TURSO_AUTH_TOKEN",
    "eyJhbGciOiJFZERTQSIsInR5cCI6IkpXVCJ9.eyJhIjoicnciLCJpYXQiOjE3ODgzODE2NzgsImlkIjoiMDFhMDYzZDktYjgwMS03MzZhLWFiYzYtYzZlYTA4OGU3Mjc4Iiwia2lkIjoiV2lzLXRta2xiQ1BZX0YwcXBEYTVDbzA5ZTJUWXhUSkFrWUl5b2NaYWdqdyIsInJpZCI6IjQ2NWRkNGYzLWIzNTUtNGNiOS05Yjk5LTIzMzhhYjgzMmMwOCJ9.Clkqlm5HEMhyLNS3r6ygb4KSoeM1VZEZPFzp5Gd-YWjh9GOftx2zDjDL9ZqZFSZM43XTUTTIVL0CGIFmtZZkAQ"
)

class TursoRow(dict):
    def __init__(self, cols, values):
        super().__init__()
        self._cols = cols
        self._values = values
        for c, v in zip(cols, values):
            self[c] = v

    def __getitem__(self, item):
        if isinstance(item, int):
            return self._values[item]
        return super().__getitem__(item)

    def keys(self):
        return self._cols

class TursoCursor:
    def __init__(self, conn):
        self.conn = conn
        self.description = None
        self._rows = []
        self._row_idx = 0
        self.rowcount = 0
        self.lastrowid = None

    def execute(self, sql, params=()):
        args = []
        for p in params:
            if p is None:
                args.append({"type": "null"})
            elif isinstance(p, int):
                args.append({"type": "integer", "value": str(p)})
            elif isinstance(p, float):
                args.append({"type": "float", "value": p})
            elif isinstance(p, bytes):
                args.append({"type": "blob", "base64": base64.b64encode(p).decode()})
            else:
                args.append({"type": "text", "value": str(p)})
        
        stmt = {"sql": sql}
        if args:
            stmt["args"] = args

        payload = {
            "requests": [
                {"type": "execute", "stmt": stmt},
                {"type": "close"}
            ]
        }
        
        req = urllib.request.Request(
            TURSO_DATABASE_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {TURSO_AUTH_TOKEN}", "Content-Type": "application/json"},
            method="POST"
        )
        
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            
        res = data["results"][0]
        if res["type"] == "error":
            raise RuntimeError(res["error"]["message"])
            
        exec_res = res["response"]["result"]
        cols = [c["name"] for c in exec_res.get("cols", [])]
        self.description = [(c, None, None, None, None, None, None) for c in cols]
        self.rowcount = exec_res.get("affected_row_count", 0)
        last_id = exec_res.get("last_insert_rowid")
        self.lastrowid = int(last_id) if last_id is not None else None
        
        rows = []
        for r in exec_res.get("rows", []):
            vals = []
            for cell in r:
                t = cell.get("type")
                if t == "null":
                    vals.append(None)
                elif t == "integer":
                    vals.append(int(cell["value"]))
                elif t == "float":
                    vals.append(float(cell["value"]))
                elif t == "blob":
                    vals.append(base64.b64decode(cell["base64"]))
                else:
                    vals.append(cell.get("value"))
            rows.append(TursoRow(cols, vals))
            
        self._rows = rows
        self._row_idx = 0
        return self

    def fetchone(self):
        if self._row_idx < len(self._rows):
            r = self._rows[self._row_idx]
            self._row_idx += 1
            return r
        return None

    def fetchall(self):
        r = self._rows[self._row_idx:]
        self._row_idx = len(self._rows)
        return r

class TursoConn:
    def cursor(self):
        return TursoCursor(self)
    def commit(self):
        pass
    def close(self):
        pass

def get_db():
    if TURSO_DATABASE_URL and TURSO_AUTH_TOKEN:
        try:
            return TursoConn()
        except Exception:
            pass
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS materials (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_name TEXT UNIQUE,
        title TEXT,
        folder_path TEXT,
        images_json TEXT,
        copy_text TEXT,
        last_tag TEXT,
        status TEXT DEFAULT 'available',
        assigned_to TEXT,
        assigned_at TEXT,
        created_at TEXT
    )
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS submissions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_name TEXT,
        material_id INTEGER,
        material_name TEXT,
        xhs_link TEXT,
        xhs_title TEXT,
        tag_expected TEXT,
        tag_matched INTEGER DEFAULT 1,
        check_status TEXT DEFAULT 'matched',
        submitted_at TEXT,
        status TEXT DEFAULT 'verified',
        note TEXT,
        settlement_status TEXT DEFAULT 'unsettled',
        settled_at TEXT,
        last_inspected_at TEXT,
        survival_status TEXT DEFAULT 'pending'
    )
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        name TEXT PRIMARY KEY,
        current_material_id INTEGER,
        completed_count INTEGER DEFAULT 0,
        last_active TEXT
    )
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    """)
    
    # Safe column migration
    cursor.execute("PRAGMA table_info(submissions)")
    cols = [r[1] for r in cursor.fetchall()]
    new_cols = [
        ('settlement_status', "TEXT DEFAULT 'unsettled'"),
        ('settled_at', "TEXT"),
        ('last_inspected_at', "TEXT"),
        ('survival_status', "TEXT DEFAULT 'pending'")
    ]
    for c_name, c_type in new_cols:
        if c_name not in cols:
            cursor.execute(f"ALTER TABLE submissions ADD COLUMN {c_name} {c_type}")

    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('passcode', '8888')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('admin_password', '060521')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('admin_security_question', '3金的专属安全暗号是什么？')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('admin_security_answer', '060521')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('auth_mode', 'whitelist')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('whitelist', '[]')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('daily_limit', '3')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('claim_timeout_hours', '2')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('cooldown_minutes', '60')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('strict_tag_check', '0')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('auto_delete_consumed', '0')")
    
    conn.commit()
    conn.close()

def get_setting(key, default=None):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = c.fetchone()
    conn.close()
    return row['value'] if row else default

def set_setting(key, value):
    conn = get_db()
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()
    conn.close()

def auto_release_expired_assignments():
    try:
        timeout_hours = float(get_setting('claim_timeout_hours', '2'))
        if timeout_hours <= 0:
            return 0
        conn = get_db()
        cursor = conn.cursor()
        now_dt = get_beijing_now()
        
        cursor.execute("SELECT id, group_name, assigned_to, assigned_at FROM materials WHERE status = 'assigned'")
        assigned_mats = cursor.fetchall()
        released_count = 0
        for mat in assigned_mats:
            if mat['assigned_at']:
                try:
                    assigned_dt = parse_beijing_time(mat['assigned_at'])
                    if assigned_dt and (now_dt - assigned_dt).total_seconds() > (timeout_hours * 3600):
                        cursor.execute("UPDATE materials SET status = 'available', assigned_to = NULL, assigned_at = NULL WHERE id = ?", (mat['id'],))
                        if mat['assigned_to']:
                            cursor.execute("UPDATE users SET current_material_id = NULL WHERE name = ?", (mat['assigned_to'],))
                        released_count += 1
                except Exception:
                    pass
        conn.commit()
        conn.close()
        return released_count
    except Exception:
        return 0

def auto_inspect_all_submissions_silent():
    """Background silent inspector: automatically re-verifies in_review and pending notes on Xiaohongshu."""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT id, xhs_link, survival_status FROM submissions WHERE survival_status IN ('pending', 'in_review') ORDER BY id DESC LIMIT 30")
        rows = cursor.fetchall()
        if not rows:
            conn.close()
            return 0
            
        now_str = get_beijing_now_str()
        headers = {'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15'}
        updated_count = 0
        
        for r in rows:
            url = r['xhs_link']
            clean_url_m = re.search(r'https?://[a-zA-Z0-9_\-\.\/\?=&%#]+', url)
            clean_url = clean_url_m.group(0) if clean_url_m else url
            survival = r['survival_status']
            try:
                req = urllib.request.Request(clean_url, headers=headers)
                with urllib.request.urlopen(req, timeout=3) as resp:
                    html = resp.read().decode('utf-8', errors='ignore')
                    if any(kw in html for kw in ['该笔记不存在', '已被删除', '内容已删除', '无法查看', '笔记找不到了']):
                        survival = 'dead'
                    elif any(kw in html for kw in ['审核中', '正在审核', '笔记审核中', '仅自己可见']):
                        survival = 'in_review'
                    else:
                        survival = 'active'
            except urllib.error.HTTPError as e:
                if e.code in (404, 410):
                    survival = 'dead'
            except Exception:
                pass
                
            if survival != r['survival_status']:
                cursor.execute("UPDATE submissions SET survival_status = ?, last_inspected_at = ? WHERE id = ?", (survival, now_str, r['id']))
                updated_count += 1
                
        conn.commit()
        conn.close()
        return updated_count
    except Exception:
        return 0

def background_worker_loop():
    """Runs every 5 minutes in background to auto-inspect notes and auto-release expired materials."""
    while True:
        try:
            time.sleep(300)
            auto_inspect_all_submissions_silent()
            auto_release_expired_assignments()
        except Exception:
            pass

# Start background thread
try:
    threading.Thread(target=background_worker_loop, daemon=True).start()
except Exception:
    pass

def extract_last_tag(copy_text):
    if not copy_text:
        return ''
    tags = re.findall(r'#[^\s#]+', copy_text)
    return tags[-1] if tags else ''

def check_worker_auth(user_name, passcode=''):
    auth_mode = get_setting('auth_mode', 'whitelist')
    real_passcode = get_setting('passcode', '8888').strip()
    whitelist_str = get_setting('whitelist', '[]')
    try:
        whitelist = json.loads(whitelist_str)
    except:
        whitelist = []

    if auth_mode == 'none':
        return True, ""
    
    if auth_mode in ('passcode', 'both'):
        if not passcode or passcode.strip() != real_passcode:
            return False, "领料口令错误！请向 3金 索取正确口令。"
            
    if auth_mode in ('whitelist', 'both'):
        if not user_name or user_name.strip() not in whitelist:
            return False, f"⚠️ 未授权的分发人员【{user_name or '匿名'}】！你尚未在 3金 的兼职白名单中，请联系 3金 添加授权。"
            
    return True, ""

def auto_detect_xhs_link_with_tag(url, expected_tag, expected_title):
    if not any(k in url.lower() for k in ['xhslink.com', 'xhslink.cn', 'xiaohongshu.com']):
        return False, "请提供有效的小红书分享链接 (包含 xhslink.com / xhslink.cn / xiaohongshu.com)！", "", False, "invalid_url"
        
    headers = {
        'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1'
    }
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=4) as response:
            html = response.read().decode('utf-8', errors='ignore')
            title_m = re.search(r'<title>(.*?)</title>', html)
            fetched_title = title_m.group(1).replace(' - 小红书', '').strip() if title_m else ''
            
            # 检查是否为小红书审核中页面
            if any(kw in html for kw in ['审核中', '正在审核', '笔记审核中', '仅自己可见', '仅作者可见', '作者正在修改', '作品处理中']):
                return True, "", fetched_title or "⏳ 官方审核中笔记", True, "in_review"
            
            tag_clean = expected_tag.replace('#', '').strip() if expected_tag else ''
            tag_found = (tag_clean in html or tag_clean in fetched_title) if tag_clean else True
            
            keywords = [w for w in re.split(r'[\s_，。：:、！!]+', expected_title) if len(w) >= 2]
            title_matched = any(k in html or k in fetched_title for k in keywords) if keywords else True
            
            check_status = 'matched' if (tag_found or title_matched) else 'in_review'
            return True, "", fetched_title or "已解析到小红书笔记", True, check_status
    except Exception as e:
        return True, "", "⏳ 官方审核中笔记（已提交待自动复核）", True, "in_review"

def resolve_file_path(path_str):
    if not path_str:
        return None
    if os.path.exists(path_str):
        return path_str
    p1 = os.path.join(PROJECT_ROOT, path_str.lstrip('/'))
    if os.path.exists(p1):
        return p1
    clean_parts = path_str.replace('\\', '/').split('/')
    for i in range(len(clean_parts)):
        sub = os.path.join(MATERIALS_DIR, *clean_parts[i:])
        if os.path.exists(sub):
            return sub
    for i in range(len(clean_parts)):
        sub = os.path.join('/Users/air/Desktop/9月1日代运营整', *clean_parts[i:])
        if os.path.exists(sub):
            return sub
    return None

def scan_and_import_materials_from_folder():
    target_dir = MATERIALS_DIR if os.path.exists(MATERIALS_DIR) else '/Users/air/Desktop/9月1日代运营整'
    if not os.path.exists(target_dir):
        return 0

    conn = get_db()
    cursor = conn.cursor()
    imported_count = 0
    now_str = get_beijing_now_str()

    for group in sorted(os.listdir(target_dir)):
        group_path = os.path.join(target_dir, group)
        if not os.path.isdir(group_path) or not re.match(r'^第\d+组', group):
            continue
        
        files = os.listdir(group_path)
        img_files = []
        copy_text = ''
        
        for f in sorted(files):
            f_lower = f.lower()
            if f.endswith('.txt') or f.endswith('.md'):
                txt_path = os.path.join(group_path, f)
                try:
                    with open(txt_path, 'r', encoding='utf-8') as tf:
                        copy_text = tf.read().strip()
                except Exception:
                    pass
            elif any(f_lower.endswith(ext) for ext in ['.png', '.jpg', '.jpeg', '.webp', '.heic', '.mov', '.mp4', '.gif']):
                img_files.append(f)
        
        def img_sort_key(name):
            if '1' in name or '封面' in name: return 1
            if '2' in name or '内容' in name: return 2
            if '3' in name or '尾图' in name: return 3
            return 99
        img_files.sort(key=img_sort_key)
        
        images_rel = [f"materials/{group}/{img}" for img in img_files]
        
        title = group
        if copy_text:
            first_line = copy_text.split('\n')[0].strip()
            if first_line:
                title = first_line[:30]
        
        last_tag = extract_last_tag(copy_text)

        cursor.execute("""
        INSERT OR IGNORE INTO materials (group_name, title, folder_path, images_json, copy_text, last_tag, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 'available', ?)
        """, (group, title, group_path, json.dumps(images_rel, ensure_ascii=False), copy_text, last_tag, now_str))
        if cursor.rowcount > 0:
            imported_count += 1
        else:
            cursor.execute("""
            UPDATE materials SET images_json = ?, copy_text = ?, last_tag = ? WHERE group_name = ?
            """, (json.dumps(images_rel, ensure_ascii=False), copy_text, last_tag, group))

    conn.commit()
    conn.close()
    return imported_count

@app.route('/api/download_zip')
def download_zip():
    mat_id = request.args.get('material_id', type=int)
    if not mat_id:
        return 'Missing material_id', 400
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM materials WHERE id = ?', (mat_id,))
    mat = cursor.fetchone()
    conn.close()
    
    if not mat:
        return 'Material not found', 404
    
    images = json.loads(mat['images_json'])
    copy_text = mat['copy_text']
    
    memory_file = io.BytesIO()
    with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
        for idx, img_ref in enumerate(images):
            if img_ref.startswith('data:'):
                try:
                    header, encoded = img_ref.split(',', 1)
                    img_data = base64.b64decode(encoded)
                    ext = 'png'
                    if 'jpeg' in header or 'jpg' in header: ext = 'jpg'
                    elif 'heic' in header: ext = 'heic'
                    elif 'mov' in header: ext = 'mov'
                    elif 'mp4' in header: ext = 'mp4'
                    elif 'gif' in header: ext = 'gif'
                    zf.writestr(f"图{idx+1}_配图.{ext}", img_data)
                except:
                    pass
            else:
                real_file = resolve_file_path(img_ref)
                if real_file and os.path.exists(real_file):
                    ext = os.path.splitext(real_file)[1]
                    zf.write(real_file, f"图{idx+1}_配图{ext}")
        if copy_text:
            zf.writestr('发布文案.txt', copy_text)
            
    memory_file.seek(0)
    clean_name = re.sub(r'[^\w\u4e00-\u9fa5]', '_', mat['group_name'])[:20]
    filename = f"{clean_name}.zip"
    return send_file(memory_file, mimetype='application/zip', as_attachment=True, download_name=filename)

@app.route('/api/image')
def serve_image():
    path = request.args.get('path', '')
    if not path:
        return 'Image not found', 404
        
    if path.startswith('data:'):
        try:
            header, encoded = path.split(',', 1)
            data = base64.b64decode(encoded)
            mime = header.split(';')[0].replace('data:', '')
            resp = make_response(data)
            resp.headers['Content-Type'] = mime or 'image/jpeg'
            resp.headers['Cache-Control'] = 'public, max-age=604800'
            return resp
        except Exception:
            return 'Invalid base64', 400

    real_path = resolve_file_path(path)
    if not real_path or not os.path.exists(real_path):
        return 'Image not found', 404

    mime, _ = mimetypes.guess_type(real_path)
    resp = make_response(send_file(real_path, mimetype=mime or 'image/jpeg'))
    resp.headers['Cache-Control'] = 'public, max-age=604800'
    return resp

@app.route('/api/user/status', methods=['GET'])
def get_user_status():
    auto_release_expired_assignments()
    name = request.args.get('name', '').strip()
    passcode = request.args.get('passcode', '').strip()
    if not name:
        return jsonify({'success': False, 'error': '请输入姓名/昵称'})
        
    is_auth, msg = check_worker_auth(name, passcode)
    if not is_auth:
        return jsonify({'success': False, 'error': msg, 'auth_failed': True})
    
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM users WHERE name = ?', (name,))
    user = cursor.fetchone()
    
    current_material = None
    if user and user['current_material_id']:
        cursor.execute('SELECT * FROM materials WHERE id = ?', (user['current_material_id'],))
        mat = cursor.fetchone()
        if mat and mat['status'] == 'assigned':
            current_material = {
                'id': mat['id'],
                'group_name': mat['group_name'],
                'title': mat['title'],
                'images': json.loads(mat['images_json']),
                'copy_text': mat['copy_text'],
                'last_tag': mat['last_tag'] or extract_last_tag(mat['copy_text']),
                'assigned_at': mat['assigned_at']
            }
            
    cursor.execute('SELECT * FROM submissions WHERE user_name = ? ORDER BY id DESC', (name,))
    subs = [dict(row) for row in cursor.fetchall()]
    
    today_str = get_beijing_today_str()
    cursor.execute('SELECT COUNT(*) FROM submissions WHERE user_name = ? AND submitted_at LIKE ?', (name, f'{today_str}%'))
    today_count = cursor.fetchone()[0]
    
    daily_limit = int(get_setting('daily_limit', '3'))
    cooldown_min = int(get_setting('cooldown_minutes', '60'))
    in_cooldown = False
    cooldown_remaining_seconds = 0

    if not current_material and subs and cooldown_min > 0:
        try:
            last_time = parse_beijing_time(subs[0]['submitted_at'])
            if last_time:
                diff_sec = (get_beijing_now() - last_time).total_seconds()
                req_sec = cooldown_min * 60
                if diff_sec < req_sec:
                    in_cooldown = True
                    cooldown_remaining_seconds = int(req_sec - diff_sec)
        except Exception:
            pass

    conn.close()
    
    return jsonify({
        'success': True,
        'user': {
            'name': name,
            'completed_count': user['completed_count'] if user else 0,
            'today_count': today_count,
            'daily_limit': daily_limit,
            'current_material': current_material,
            'history': subs,
            'in_cooldown': in_cooldown,
            'cooldown_remaining_seconds': cooldown_remaining_seconds,
            'cooldown_minutes': cooldown_min
        }
    })

@app.route('/api/claim', methods=['POST'])
def claim_material():
    auto_release_expired_assignments()
    data = request.json or {}
    user_name = data.get('user_name', '').strip()
    passcode = data.get('passcode', '').strip()
    xhs_link = data.get('xhs_link', '').strip()
    
    if not user_name:
        return jsonify({'success': False, 'error': '请输入姓名或微信昵称！'})
        
    is_auth, msg = check_worker_auth(user_name, passcode)
    if not is_auth:
        return jsonify({'success': False, 'error': msg, 'auth_failed': True})
        
    conn = get_db()
    cursor = conn.cursor()
    now_dt = get_beijing_now()
    now_str = get_beijing_now_str()
    today_str = get_beijing_today_str()
    
    daily_limit = int(get_setting('daily_limit', '3'))
    cooldown_min = int(get_setting('cooldown_minutes', '60'))
    cursor.execute('SELECT COUNT(*) FROM submissions WHERE user_name = ? AND submitted_at LIKE ?', (user_name, f'{today_str}%'))
    today_submitted = cursor.fetchone()[0]
    
    cursor.execute('SELECT * FROM users WHERE name = ?', (user_name,))
    user = cursor.fetchone()
    current_mat_id = user['current_material_id'] if user else None
    
    # Handle submission of currently assigned material
    if current_mat_id:
        cursor.execute('SELECT * FROM materials WHERE id = ?', (current_mat_id,))
        curr_mat = cursor.fetchone()
        if curr_mat and curr_mat['status'] == 'assigned':
            if not xhs_link:
                conn.close()
                return jsonify({
                    'success': False,
                    'error': f'你当前领取的【{curr_mat["group_name"]}】尚未提交小红书链接！请先粘贴发布链接打卡。',
                    'current_material': {
                        'id': curr_mat['id'],
                        'group_name': curr_mat['group_name'],
                        'title': curr_mat['title'],
                        'images': json.loads(curr_mat['images_json']),
                        'copy_text': curr_mat['copy_text'],
                        'last_tag': curr_mat['last_tag'] or extract_last_tag(curr_mat['copy_text']),
                        'assigned_at': curr_mat['assigned_at']
                    }
                })
            
            url_match = re.search(r'https?://[a-zA-Z0-9_\-\.\/\?=&%#]+', xhs_link)
            clean_url = url_match.group(0).rstrip('。，、！？') if url_match else xhs_link.strip()
            
            cursor.execute('SELECT id, user_name, submitted_at FROM submissions WHERE xhs_link = ?', (clean_url,))
            existing_sub = cursor.fetchone()
            if existing_sub:
                conn.close()
                return jsonify({
                    'success': False,
                    'error': f'⚠️ 该小红书链接已被提交打卡过（提交人：{existing_sub["user_name"]}），请勿使用重复链接！'
                })
            
            expected_tag = curr_mat['last_tag'] or extract_last_tag(curr_mat['copy_text'])
            is_valid_link, err_msg, xhs_title, matched, check_status = auto_detect_xhs_link_with_tag(clean_url, expected_tag, curr_mat['title'])
            if not is_valid_link:
                conn.close()
                return jsonify({'success': False, 'error': err_msg})
            
            cursor.execute("""
            INSERT INTO submissions (user_name, material_id, material_name, xhs_link, xhs_title, tag_expected, tag_matched, check_status, submitted_at, status, settlement_status, survival_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'verified', 'unsettled', ?)
            """, (user_name, curr_mat['id'], curr_mat['group_name'], clean_url, xhs_title, expected_tag, 1 if matched else 0, check_status, now_str, 'in_review' if check_status == 'in_review' else 'active'))
            
            auto_delete = get_setting('auto_delete_consumed', '0') == '1'
            if auto_delete:
                cursor.execute('DELETE FROM materials WHERE id = ?', (curr_mat['id'],))
            else:
                cursor.execute("UPDATE materials SET status = 'completed' WHERE id = ?", (curr_mat['id'],))
            
            today_submitted += 1
            cursor.execute("""
            UPDATE users SET completed_count = completed_count + 1, current_material_id = NULL, last_active = ?
            WHERE name = ?
            """, (now_str, user_name))
            conn.commit()

            req_cooldown_sec = cooldown_min * 60
            conn.close()

            review_tip = "（小红书官方审核中，系统已记录）" if check_status == 'in_review' else ""
            return jsonify({
                'success': True,
                'submitted_success': True,
                'message': f'🎉 打卡成功{review_tip}！已开启 60 分钟防限流保护，冷却结束后可继续领取下一篇。',
                'in_cooldown': cooldown_min > 0,
                'cooldown_remaining_seconds': req_cooldown_sec,
                'user': {
                    'name': user_name,
                    'completed_count': user['completed_count'] + 1 if user else 1,
                    'today_count': today_submitted,
                    'daily_limit': daily_limit,
                    'current_material': None,
                    'in_cooldown': cooldown_min > 0,
                    'cooldown_remaining_seconds': req_cooldown_sec,
                    'cooldown_minutes': cooldown_min
                }
            })
            
    # Handle claiming next available material
    if today_submitted >= daily_limit:
        conn.commit()
        conn.close()
        return jsonify({
            'success': False,
            'reached_limit': True,
            'error': f'🛑 你今天已成功打卡 {today_submitted} 篇，已达到单日领料上限（{daily_limit} 篇/天）！小红书单号频繁发帖易被平台限流，请明天再来领取~'
        })
        
    # Check cooldown
    if cooldown_min > 0:
        cursor.execute('SELECT submitted_at FROM submissions WHERE user_name = ? ORDER BY id DESC LIMIT 1', (user_name,))
        last_sub = cursor.fetchone()
        if last_sub:
            try:
                last_time = parse_beijing_time(last_sub['submitted_at'])
                if last_time:
                    diff_seconds = (now_dt - last_time).total_seconds()
                    required_seconds = cooldown_min * 60
                    if diff_seconds < required_seconds:
                        remaining_seconds = int(required_seconds - diff_seconds)
                        remaining_min = int(remaining_seconds / 60) + 1
                        conn.close()
                        return jsonify({
                            'success': False,
                            'in_cooldown': True,
                            'cooldown_remaining_seconds': remaining_seconds,
                            'error': f'⏳ 小红书养号防限流保护：距离上一篇打卡还需等待 {remaining_min} 分钟冷却时间，稍后再来领取下一组！'
                        })
            except Exception:
                pass
    
    cursor.execute("SELECT * FROM materials WHERE status = 'available' ORDER BY id ASC LIMIT 1")
    next_mat = cursor.fetchone()
    
    if not next_mat:
        conn.commit()
        conn.close()
        return jsonify({
            'success': False,
            'error': '🎉 当前素材池所有内容已全部被领完！请联系 3金 补充新一批素材。',
            'no_more': True
        })
        
    cursor.execute("""
    UPDATE materials SET status = 'assigned', assigned_to = ?, assigned_at = ? WHERE id = ?
    """, (user_name, now_str, next_mat['id']))
    
    if not user:
        cursor.execute("""
        INSERT INTO users (name, current_material_id, completed_count, last_active)
        VALUES (?, ?, 0, ?)
        """, (user_name, next_mat['id'], now_str))
    else:
        cursor.execute("""
        UPDATE users SET current_material_id = ?, last_active = ? WHERE name = ?
        """, (next_mat['id'], now_str, user_name))
        
    conn.commit()
    conn.close()
    
    last_tag = next_mat['last_tag'] or extract_last_tag(next_mat['copy_text'])
    
    return jsonify({
        'success': True,
        'message': f'恭喜领取成功！已为你分配：【{next_mat["group_name"]}】（今日第 {today_submitted + 1}/{daily_limit} 组）',
        'material': {
            'id': next_mat['id'],
            'group_name': next_mat['group_name'],
            'title': next_mat['title'],
            'images': json.loads(next_mat['images_json']),
            'copy_text': next_mat['copy_text'],
            'last_tag': last_tag,
            'assigned_at': now_str
        }
    })

@app.route('/api/admin/login', methods=['POST'])
def admin_login():
    data = request.json or {}
    pwd = data.get('password', '').strip()
    real_pwd = get_setting('admin_password', '060521').strip()
    if pwd == real_pwd:
        return jsonify({'success': True, 'token': 'admin_authed'})
    return jsonify({'success': False, 'error': '管理密码错误！'})

@app.route('/api/admin/settings', methods=['GET', 'POST'])
def admin_settings():
    admin_pwd = request.headers.get('X-Admin-Password', '')
    real_pwd = get_setting('admin_password', '060521').strip()
    if admin_pwd != real_pwd:
        return jsonify({'success': False, 'error': '未授权管理操作'}), 401
        
    if request.method == 'POST':
        data = request.json or {}
        if 'passcode' in data:
            set_setting('passcode', data['passcode'].strip())
        if 'auth_mode' in data:
            set_setting('auth_mode', data['auth_mode'])
        if 'daily_limit' in data:
            set_setting('daily_limit', str(int(data['daily_limit'])))
        if 'claim_timeout_hours' in data:
            set_setting('claim_timeout_hours', str(float(data['claim_timeout_hours'])))
        if 'cooldown_minutes' in data:
            set_setting('cooldown_minutes', str(int(data['cooldown_minutes'])))
        if 'strict_tag_check' in data:
            set_setting('strict_tag_check', '1' if data['strict_tag_check'] else '0')
        if 'auto_delete_consumed' in data:
            set_setting('auto_delete_consumed', '1' if data['auto_delete_consumed'] else '0')
        if 'admin_password' in data and data['admin_password'].strip():
            set_setting('admin_password', data['admin_password'].strip())
        if 'whitelist' in data:
            if isinstance(data['whitelist'], list):
                set_setting('whitelist', json.dumps(data['whitelist'], ensure_ascii=False))
            elif isinstance(data['whitelist'], str):
                names = [n.strip() for n in re.split(r'[,，\n\s]+', data['whitelist']) if n.strip()]
                set_setting('whitelist', json.dumps(names, ensure_ascii=False))
        return jsonify({'success': True, 'message': '设置已成功保存！'})
        
    whitelist_str = get_setting('whitelist', '[]')
    try:
        whitelist = json.loads(whitelist_str)
    except:
        whitelist = []
        
    return jsonify({
        'success': True,
        'passcode': get_setting('passcode', '8888'),
        'auth_mode': get_setting('auth_mode', 'whitelist'),
        'daily_limit': get_setting('daily_limit', '3'),
        'claim_timeout_hours': get_setting('claim_timeout_hours', '2'),
        'cooldown_minutes': get_setting('cooldown_minutes', '60'),
        'strict_tag_check': get_setting('strict_tag_check', '1') == '1',
        'auto_delete_consumed': get_setting('auto_delete_consumed', '0') == '1',
        'admin_password': get_setting('admin_password', '060521'),
        'admin_security_question': get_setting('admin_security_question', '3金的专属安全暗号是什么？'),
        'whitelist': whitelist
    })

@app.route('/api/admin/security_info', methods=['GET'])
def get_admin_security_info():
    return jsonify({
        'success': True,
        'question': get_setting('admin_security_question', '3金的专属安全暗号是什么？')
    })

@app.route('/api/admin/change_password', methods=['POST'])
def admin_change_password():
    admin_pwd = request.headers.get('X-Admin-Password', '').strip()
    real_pwd = get_setting('admin_password', '060521').strip()
    real_ans = get_setting('admin_security_answer', '060521').strip()
    data = request.json or {}

    old_pwd = data.get('old_password', '').strip()
    sec_ans = data.get('security_answer', '').strip()
    new_pwd = data.get('new_password', '').strip()
    new_question = data.get('new_security_question', '').strip()
    new_answer = data.get('new_security_answer', '').strip()

    # Verify either valid admin header or old password
    if admin_pwd != real_pwd and old_pwd != real_pwd:
        return jsonify({'success': False, 'error': '原管理员密码验证不正确！'}), 403

    # Verify security answer
    if sec_ans != real_ans:
        return jsonify({'success': False, 'error': '密保答案不正确，无法修改密码！'}), 403

    if not new_pwd:
        return jsonify({'success': False, 'error': '新管理密码不能为空！'}), 400

    set_setting('admin_password', new_pwd)
    if new_question:
        set_setting('admin_security_question', new_question)
    if new_answer:
        set_setting('admin_security_answer', new_answer)

    return jsonify({
        'success': True,
        'message': '管理员密码及安全密保已成功修改！',
        'new_password': new_pwd
    })

@app.route('/api/admin/reset_password', methods=['POST'])
def admin_reset_password():
    data = request.json or {}
    sec_ans = data.get('security_answer', '').strip()
    new_pwd = data.get('new_password', '').strip()
    real_ans = get_setting('admin_security_answer', '060521').strip()

    if not sec_ans or sec_ans != real_ans:
        return jsonify({'success': False, 'error': '密保答案不正确，无法重置密码！'}), 403

    if not new_pwd:
        return jsonify({'success': False, 'error': '新管理密码不能为空！'}), 400

    set_setting('admin_password', new_pwd)
    return jsonify({
        'success': True,
        'message': '密保核验通过！管理员密码已成功重置！',
        'new_password': new_pwd
    })

@app.route('/api/admin/whitelist/add', methods=['POST'])
def admin_whitelist_add():
    admin_pwd = request.headers.get('X-Admin-Password', '')
    real_pwd = get_setting('admin_password', '060521').strip()
    if admin_pwd != real_pwd:
        return jsonify({'success': False, 'error': '未授权'}), 401
    data = request.json or {}
    name = data.get('name', '').strip()
    if not name:
        return jsonify({'success': False, 'error': '姓名不能为空'})
    whitelist_str = get_setting('whitelist', '[]')
    try:
        whitelist = json.loads(whitelist_str)
    except:
        whitelist = []
    if name not in whitelist:
        whitelist.append(name)
        set_setting('whitelist', json.dumps(whitelist, ensure_ascii=False))
    return jsonify({'success': True, 'whitelist': whitelist, 'message': f'已成功添加【{name}】至白名单！'})

@app.route('/api/admin/whitelist/remove', methods=['POST'])
def admin_whitelist_remove():
    admin_pwd = request.headers.get('X-Admin-Password', '')
    real_pwd = get_setting('admin_password', '060521').strip()
    if admin_pwd != real_pwd:
        return jsonify({'success': False, 'error': '未授权'}), 401
    data = request.json or {}
    name = data.get('name', '').strip()
    whitelist_str = get_setting('whitelist', '[]')
    try:
        whitelist = json.loads(whitelist_str)
    except:
        whitelist = []
    if name in whitelist:
        whitelist = [n for n in whitelist if n != name]
        set_setting('whitelist', json.dumps(whitelist, ensure_ascii=False))
    return jsonify({'success': True, 'whitelist': whitelist, 'message': f'已将【{name}】移出白名单'})

@app.route('/api/admin/submissions/toggle_settlement', methods=['POST'])
def admin_toggle_settlement():
    admin_pwd = request.headers.get('X-Admin-Password', '')
    real_pwd = get_setting('admin_password', '060521').strip()
    if admin_pwd != real_pwd:
        return jsonify({'success': False, 'error': '未授权'}), 401
        
    data = request.json or {}
    sub_id = data.get('id')
    new_status = data.get('status')
    if not sub_id:
        return jsonify({'success': False, 'error': 'Missing sub_id'})
        
    now_str = get_beijing_now_str()
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
    UPDATE submissions SET settlement_status = ?, settled_at = ? WHERE id = ?
    """, (new_status, now_str if new_status == 'settled' else None, sub_id))
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'settlement_status': new_status, 'message': f'状态已更新为【{"已结算" if new_status == "settled" else "未结算"}】'})

@app.route('/api/admin/submissions/inspect_survival', methods=['POST'])
def admin_inspect_survival():
    admin_pwd = request.headers.get('X-Admin-Password', '')
    real_pwd = get_setting('admin_password', '060521').strip()
    if admin_pwd != real_pwd:
        return jsonify({'success': False, 'error': '未授权'}), 401
        
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, xhs_link, submitted_at FROM submissions ORDER BY id DESC")
    rows = cursor.fetchall()
    
    inspected_count = 0
    dead_count = 0
    now_str = get_beijing_now_str()
    headers = {'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15'}
    
    for r in rows:
        url = r['xhs_link']
        clean_url_m = re.search(r'https?://[a-zA-Z0-9_\-\.\/\?=&%#]+', url)
        clean_url = clean_url_m.group(0) if clean_url_m else url
        survival = 'active'
        try:
            req = urllib.request.Request(clean_url, headers=headers)
            with urllib.request.urlopen(req, timeout=3) as resp:
                html = resp.read().decode('utf-8', errors='ignore')
                if any(kw in html for kw in ['该笔记不存在', '已被删除', '内容已删除', '无法查看', '笔记找不到了']):
                    survival = 'dead'
                    dead_count += 1
        except urllib.error.HTTPError as e:
            if e.code in (404, 410):
                survival = 'dead'
                dead_count += 1
        except Exception:
            pass
            
        cursor.execute("UPDATE submissions SET survival_status = ?, last_inspected_at = ? WHERE id = ?", (survival, now_str, r['id']))
        inspected_count += 1
        
    conn.commit()
    conn.close()
    return jsonify({
        'success': True, 
        'message': f'✅ 存活巡检完成！共巡检 {inspected_count} 篇，正常存活 {inspected_count - dead_count} 篇，已失效/删除 {dead_count} 篇。',
        'dead_count': dead_count
    })

@app.route('/api/admin/materials/release_expired', methods=['POST'])
def admin_release_expired():
    admin_pwd = request.headers.get('X-Admin-Password', '')
    real_pwd = get_setting('admin_password', '060521').strip()
    if admin_pwd != real_pwd:
        return jsonify({'success': False, 'error': '未授权'}), 401
    count = auto_release_expired_assignments()
    return jsonify({'success': True, 'message': f'已成功释放 {count} 组超时的素材，退回素材池！'})

@app.route('/api/admin/materials/add', methods=['POST'])
def admin_add_material():
    admin_pwd = request.headers.get('X-Admin-Password', '')
    real_pwd = get_setting('admin_password', '060521').strip()
    if admin_pwd != real_pwd:
        return jsonify({'success': False, 'error': '未授权'}), 401

    data = request.json or {}
    group_name = data.get('group_name', '').strip()
    copy_text = data.get('copy_text', '').strip()
    img1 = data.get('img1', '')
    img2 = data.get('img2', '')
    img3 = data.get('img3', '')
    images = data.get('images', [])
    
    final_images = []
    if img1: final_images.append(img1)
    if img2: final_images.append(img2)
    if img3: final_images.append(img3)
    if not final_images and images:
        final_images = images

    if not group_name:
        return jsonify({'success': False, 'error': '请输入素材组名/标题！'})
    if not copy_text:
        return jsonify({'success': False, 'error': '请填写发布文案！'})
    if len(final_images) == 0:
        return jsonify({'success': False, 'error': '请至少在图1(封面)上传配图！'})
        
    title = group_name
    first_line = copy_text.split('\n')[0].strip()
    if first_line:
        title = first_line[:30]
        
    last_tag = extract_last_tag(copy_text)
    now_str = get_beijing_now_str()
    
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("""
        INSERT INTO materials (group_name, title, folder_path, images_json, copy_text, last_tag, status, created_at)
        VALUES (?, ?, 'cloud_upload', ?, ?, ?, 'available', ?)
        """, (group_name, title, json.dumps(final_images, ensure_ascii=False), copy_text, last_tag, now_str))
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'message': f'🎉 素材【{group_name}】已成功加入素材池！'})
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({'success': False, 'error': f'素材组名【{group_name}】已存在，请换一个名称！'})

@app.route('/api/admin/materials/batch_add', methods=['POST'])
def admin_batch_add():
    admin_pwd = request.headers.get('X-Admin-Password', '')
    real_pwd = get_setting('admin_password', '060521').strip()
    if admin_pwd != real_pwd:
        return jsonify({'success': False, 'error': '未授权'}), 401

    data = request.json or {}
    covers = data.get('covers', [])
    contents = data.get('contents', [])
    tails = data.get('tails', [])
    copies = data.get('copies', [])
    prefix = data.get('prefix', '批量作品_').strip()
    
    if not covers or len(covers) == 0:
        return jsonify({'success': False, 'error': '请至少上传一批【图1 · 封面图】！'})
    if not copies or len(copies) == 0:
        return jsonify({'success': False, 'error': '请至少提供一组文案！'})
        
    count = min(len(covers), len(copies))
    now_str = get_beijing_now_str()
    
    conn = get_db()
    cursor = conn.cursor()
    success_count = 0
    
    for i in range(count):
        g_name = f"{prefix}第{i+1:02d}组_{datetime.datetime.now().strftime('%m%d_%H%M%S')}_{i+1}"
        copy = copies[i].strip()
        first_line = copy.split('\n')[0].strip()
        title = first_line[:30] if first_line else g_name
        last_tag = extract_last_tag(copy)
        
        imgs = []
        if i < len(covers): imgs.append(covers[i])
        if i < len(contents): imgs.append(contents[i])
        elif len(contents) > 0: imgs.append(contents[0])
        if i < len(tails): imgs.append(tails[i])
        elif len(tails) > 0: imgs.append(tails[0])
        
        try:
            cursor.execute("""
            INSERT INTO materials (group_name, title, folder_path, images_json, copy_text, last_tag, status, created_at)
            VALUES (?, ?, 'cloud_batch', ?, ?, ?, 'available', ?)
            """, (g_name, title, json.dumps(imgs, ensure_ascii=False), copy, last_tag, now_str))
            success_count += 1
        except Exception:
            pass
            
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'message': f'🎉 成功一键批量组装并入库 {success_count} 组全新作品！'})

def get_pipeline_queues():
    try:
        covers = json.loads(get_setting('buffer_covers', '[]'))
    except Exception:
        covers = []
    try:
        contents = json.loads(get_setting('buffer_contents', '[]'))
    except Exception:
        contents = []
    try:
        ends = json.loads(get_setting('buffer_ends', '[]'))
    except Exception:
        ends = []
    try:
        copies = json.loads(get_setting('buffer_copies', '[]'))
    except Exception:
        copies = []
    return covers, contents, ends, copies

def save_pipeline_queues(covers, contents, ends, copies):
    set_setting('buffer_covers', json.dumps(covers, ensure_ascii=False))
    set_setting('buffer_contents', json.dumps(contents, ensure_ascii=False))
    set_setting('buffer_ends', json.dumps(ends, ensure_ascii=False))
    set_setting('buffer_copies', json.dumps(copies, ensure_ascii=False))

def trigger_pipeline_auto_assembly(prefix='装配作品_'):
    covers, contents, ends, copies = get_pipeline_queues()
    assemble_count = min(len(covers), len(contents), len(ends), len(copies))
    if assemble_count <= 0:
        return 0, {
            'covers': len(covers),
            'contents': len(contents),
            'ends': len(ends),
            'copies': len(copies),
            'can_assemble': False
        }

    conn = get_db()
    cursor = conn.cursor()
    now_dt = get_beijing_now()
    time_tag = now_dt.strftime('%m%d_%H%M%S')

    for i in range(assemble_count):
        cover = covers.pop(0)
        content = contents.pop(0)
        end = ends.pop(0)
        copy_text = copies.pop(0)

        group_name = f"{prefix}{time_tag}_{i+1:02d}"
        first_line = copy_text.split('\n')[0].strip() if copy_text else ''
        title = first_line[:30] if first_line else group_name
        last_tag = extract_last_tag(copy_text)
        images_json = json.dumps([cover, content, end], ensure_ascii=False)
        now_str = now_dt.strftime('%Y-%m-%d %H:%M:%S')

        cursor.execute("""
        INSERT INTO materials (group_name, title, folder_path, images_json, copy_text, last_tag, status, created_at)
        VALUES (?, ?, 'pipeline_assembled', ?, ?, ?, 'available', ?)
        """, (group_name, title, images_json, copy_text, last_tag, now_str))

    conn.commit()
    conn.close()

    save_pipeline_queues(covers, contents, ends, copies)
    return assemble_count, {
        'covers': len(covers),
        'contents': len(contents),
        'ends': len(ends),
        'copies': len(copies),
        'can_assemble': False
    }

@app.route('/api/admin/pipeline/status', methods=['GET'])
def admin_pipeline_status():
    admin_pwd = request.headers.get('X-Admin-Password', '')
    real_pwd = get_setting('admin_password', '060521').strip()
    if admin_pwd != real_pwd:
        return jsonify({'success': False, 'error': '未授权'}), 401
    
    covers, contents, ends, copies = get_pipeline_queues()
    min_count = min(len(covers), len(contents), len(ends), len(copies))
    return jsonify({
        'success': True,
        'counts': {
            'covers': len(covers),
            'contents': len(contents),
            'ends': len(ends),
            'copies': len(copies)
        },
        'can_assemble': min_count >= 1,
        'min_count': min_count
    })

@app.route('/api/admin/pipeline/push', methods=['POST'])
def admin_pipeline_push():
    admin_pwd = request.headers.get('X-Admin-Password', '')
    real_pwd = get_setting('admin_password', '060521').strip()
    if admin_pwd != real_pwd:
        return jsonify({'success': False, 'error': '未授权'}), 401

    data = request.json or {}
    slot = data.get('slot', '')
    items = data.get('items', [])
    prefix = data.get('prefix', '装配作品_').strip()

    if not slot or not items:
        return jsonify({'success': False, 'error': '缺少入池数据！'}), 400

    covers, contents, ends, copies = get_pipeline_queues()
    if slot == 'covers':
        covers.extend(items)
    elif slot == 'contents':
        contents.extend(items)
    elif slot == 'ends':
        ends.extend(items)
    elif slot == 'copies':
        copies.extend(items)
    else:
        return jsonify({'success': False, 'error': '无效的槽位名称'}), 400

    save_pipeline_queues(covers, contents, ends, copies)
    assembled, status = trigger_pipeline_auto_assembly(prefix)
    
    return jsonify({
        'success': True,
        'assembled': assembled,
        'counts': {
            'covers': len(covers),
            'contents': len(contents),
            'ends': len(ends),
            'copies': len(copies)
        },
        'message': f'🎉 成功入池 {len(items)} 项！' + (f' ⚡️ 满足 3 图+文案条件，已自动拼装并入库 {assembled} 组新作品！' if assembled > 0 else '')
    })

@app.route('/api/admin/pipeline/clear', methods=['POST'])
def admin_pipeline_clear():
    admin_pwd = request.headers.get('X-Admin-Password', '')
    real_pwd = get_setting('admin_password', '060521').strip()
    if admin_pwd != real_pwd:
        return jsonify({'success': False, 'error': '未授权'}), 401

    data = request.json or {}
    slot = data.get('slot', 'all')
    covers, contents, ends, copies = get_pipeline_queues()
    if slot == 'covers' or slot == 'all': covers = []
    if slot == 'contents' or slot == 'all': contents = []
    if slot == 'ends' or slot == 'all': ends = []
    if slot == 'copies' or slot == 'all': copies = []
    save_pipeline_queues(covers, contents, ends, copies)
    return jsonify({'success': True, 'message': '已清空对应缓冲箱'})

@app.route('/api/admin/materials/delete', methods=['POST'])
def admin_delete_material():
    admin_pwd = request.headers.get('X-Admin-Password', '')
    real_pwd = get_setting('admin_password', '060521').strip()
    if admin_pwd != real_pwd:
        return jsonify({'success': False, 'error': '未授权'}), 401
    
    data = request.json or {}
    mat_id = data.get('id')
    if not mat_id:
        return jsonify({'success': False, 'error': 'Missing id'})
        
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM materials WHERE id = ?', (mat_id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'message': '素材已成功删除！'})

@app.route('/api/admin/materials/clear_completed', methods=['POST'])
def admin_clear_completed():
    admin_pwd = request.headers.get('X-Admin-Password', '')
    real_pwd = get_setting('admin_password', '060521').strip()
    if admin_pwd != real_pwd:
        return jsonify({'success': False, 'error': '未授权'}), 401
        
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM materials WHERE status = 'completed'")
    count = cursor.rowcount
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'message': f'已成功清理 {count} 组已消耗的素材！'})

@app.route('/api/admin/stats', methods=['GET'])
def admin_stats():
    auto_release_expired_assignments()
    admin_pwd = request.headers.get('X-Admin-Password', '')
    real_pwd = get_setting('admin_password', '060521').strip()
    if admin_pwd != real_pwd:
        return jsonify({'success': False, 'error': '管理密码错误或未登录'}), 401

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM materials')
    total_materials = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM materials WHERE status = 'available'")
    available = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM materials WHERE status = 'assigned'")
    assigned = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM materials WHERE status = 'completed'")
    completed = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM submissions')
    total_submissions = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM submissions WHERE settlement_status = 'settled'")
    settled_submissions = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM users')
    total_users = cursor.fetchone()[0]
    
    cursor.execute('SELECT id, group_name, title, last_tag, status, assigned_to, assigned_at FROM materials ORDER BY id ASC')
    materials_list = [dict(r) for r in cursor.fetchall()]
    
    cursor.execute('SELECT * FROM submissions ORDER BY id DESC')
    submissions_list = [dict(r) for r in cursor.fetchall()]

    cursor.execute('SELECT name, completed_count, last_active FROM users ORDER BY completed_count DESC')
    all_workers = [dict(r) for r in cursor.fetchall()]
    
    conn.close()
    return jsonify({
        'success': True,
        'stats': {
            'total_materials': total_materials,
            'available': available,
            'assigned': assigned,
            'completed': completed,
            'total_submissions': total_submissions,
            'settled_submissions': settled_submissions,
            'unsettled_submissions': total_submissions - settled_submissions,
            'total_users': total_users
        },
        'materials': materials_list,
        'submissions': submissions_list,
        'workers': all_workers
    })

@app.route('/api/admin/sync', methods=['POST'])
def admin_sync():
    admin_pwd = request.headers.get('X-Admin-Password', '')
    real_pwd = get_setting('admin_password', '060521').strip()
    if admin_pwd != real_pwd:
        return jsonify({'success': False, 'error': '未授权'}), 401
    count = scan_and_import_materials_from_folder()
    return jsonify({'success': True, 'imported': count, 'message': f'成功从文件夹同步导入 {count} 组新素材！'})

@app.route('/api/admin/export_csv', methods=['GET'])
def export_csv():
    admin_pwd = request.args.get('token', request.headers.get('X-Admin-Password', '')).strip()
    real_pwd = get_setting('admin_password', '060521').strip()
    if admin_pwd != real_pwd:
        return jsonify({'success': False, 'error': '未授权访问，请输入管理员密码'}), 401

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT id, user_name, material_name, xhs_link, xhs_title, tag_expected, tag_matched, check_status, submitted_at, settlement_status, settled_at, survival_status, last_inspected_at FROM submissions ORDER BY id DESC')
    rows = cursor.fetchall()
    conn.close()
    
    csv_content = "\ufeffID,分发人员姓名,领取作品组名,结算状态,结算时间,24h存活状态,小红书发布链接,抓取标题,文案核验Tag,Tag核验结果,打卡时间\n"
    for r in rows:
        t_str = r["xhs_title"] if r["xhs_title"] else "-"
        tag_str = r["tag_expected"] if r["tag_expected"] else "-"
        match_str = "已匹配Tag" if r["tag_matched"] == 1 else "未检测到Tag"
        settle_str = "已结算" if r["settlement_status"] == "settled" else "未结算"
        settle_t = r["settled_at"] if r["settled_at"] else "-"
        surv_str = "正常存活" if r["survival_status"] == "active" else "已被删/失效" if r["survival_status"] == "dead" else "待巡检"
        csv_content += f'"{r["id"]}","{r["user_name"]}","{r["material_name"]}","{settle_str}","{settle_t}","{surv_str}","{r["xhs_link"]}","{t_str}","{tag_str}","{match_str}","{r["submitted_at"]}"\n'
        
    filename = f"小红书矩阵打卡与结算总账_{get_beijing_now().strftime('%Y%m%d_%H%M%S')}.csv"
    return Response(
        csv_content,
        mimetype="text/csv",
        headers={"Content-disposition": f"attachment; filename={filename}"}
    )

INDEX_HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>小红书矩阵分发 · 凭链接自动领料平台</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        body { font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", sans-serif; }
        .xhs-gradient { background: linear-gradient(135deg, #ff2442 0%, #ff4d6a 100%); }
    </style>
</head>
<body class="bg-slate-50 text-slate-800 min-h-screen pb-16">

    <!-- Top Navigation Header -->
    <header class="sticky top-0 z-40 bg-white/90 backdrop-blur border-b border-slate-200 px-4 py-3 shadow-sm">
        <div class="max-w-3xl mx-auto flex items-center justify-between">
            <div class="flex items-center space-x-2.5">
                <div class="h-8 px-2.5 rounded-xl bg-[#ff2442] flex items-center justify-center text-white font-black text-xs shadow-md shadow-red-500/20 tracking-wider select-none">
                    小红书
                </div>
                <div>
                    <h1 class="font-bold text-slate-900 leading-none text-base">矩阵分发 · 领料打卡台</h1>
                    <p class="text-xs text-slate-400 mt-0.5">独家素材派发 · 一客一单不复用</p>
                </div>
            </div>
            <div class="flex items-center space-x-2">
                <button onclick="openAdmin()" class="text-xs bg-slate-100 hover:bg-slate-200 text-slate-700 px-3 py-1.5 rounded-full font-bold transition flex items-center space-x-1 border border-slate-200 shadow-sm">
                    <span>👑 3金管理后台</span>
                </button>
            </div>
        </div>
    </header>

    <main class="max-w-3xl mx-auto px-4 pt-4 space-y-4">

        <!-- User Profile Card -->
        <div class="bg-white rounded-2xl p-4 shadow-sm border border-slate-200">
            <div class="flex items-center justify-between pb-2.5 border-b border-slate-100">
                <div class="flex items-center space-x-1.5">
                    <span class="text-base">🔐</span>
                    <span class="font-bold text-sm text-slate-800">分发人员身份验证</span>
                </div>
                <div id="completedBadge" class="text-xs font-semibold px-2.5 py-0.5 rounded-full bg-red-50 text-red-600 border border-red-100 hidden">
                    今日已打卡 <span id="todayCountSpan">0</span>/<span id="dailyLimitSpan">3</span> 组
                </div>
            </div>

            <div class="mt-3">
                <label class="block text-[11px] font-semibold text-slate-500 mb-1">分发人姓名 / 微信昵称（白名单人员）：</label>
                <div class="flex items-center space-x-2">
                    <input type="text" id="userNameInput" placeholder="请输入你的姓名 / 微信昵称 (如: 皮皮 / 三金)" 
                        class="w-full px-3 py-2 text-sm bg-slate-50 border border-slate-200 rounded-xl focus:outline-none focus:ring-2 focus:ring-red-500 focus:bg-white transition"
                        oninput="saveCredentials()" onkeydown="if(event.key==='Enter') checkUserStatus()">
                    <button onclick="checkUserStatus()" class="shrink-0 px-4 py-2 text-xs bg-slate-800 hover:bg-slate-900 text-white rounded-xl font-bold transition shadow-sm">
                        🔐 验证并同步
                    </button>
                </div>
            </div>
        </div>

        <!-- Claiming & Verification Card -->
        <div class="bg-white rounded-2xl p-5 shadow-sm border border-slate-200">
            <h2 class="font-bold text-base text-slate-900 flex items-center space-x-2">
                <span>⚡️</span>
                <span>领料 & 链接打卡</span>
            </h2>

            <!-- State 1: No active task, can claim first task -->
            <div id="firstClaimBox" class="mt-3 space-y-3">
                <div class="p-3 bg-amber-50 rounded-xl border border-amber-200/60 text-xs text-amber-800 leading-relaxed">
                    📌 <strong>领料说明</strong>：输入姓名后，点击下方按钮即可领取专属独家发布素材（每组素材独家派发，不重复使用）！
                </div>
                <button onclick="claimMaterial(false)" class="w-full py-3.5 xhs-gradient hover:opacity-95 text-white rounded-xl font-bold text-sm shadow-md shadow-red-500/20 transition flex items-center justify-center space-x-2">
                    <span>🎁 领取第 1 组独家素材</span>
                </button>
            </div>

            <!-- State 2: Has active task, MUST submit link to get next -->
            <div id="submitLinkBox" class="mt-3 space-y-3 hidden">
                <div class="p-3.5 bg-blue-50 rounded-xl border border-blue-200/60 text-xs text-blue-900">
                    <div class="font-bold flex items-center justify-between">
                        <span>⏳ 当前进行中任务：<span id="activeGroupName" class="text-blue-700"></span></span>
                        <span class="text-xs text-blue-600 bg-blue-100 px-2 py-0.5 rounded font-bold">待回传链接</span>
                    </div>
                    <p class="mt-1.5 text-slate-600 leading-relaxed">在小红书发帖后，直接<strong>复制整段分享口令</strong>长按粘贴在下方（系统会自动提取核心链接并核验），即可领取下一组！</p>
                </div>

                <div>
                    <label class="block text-xs font-semibold text-slate-700 mb-1">粘贴小红书已发布笔记分享口令/链接：</label>
                    <textarea id="xhsLinkInput" rows="2" placeholder="长按直接粘贴小红书复制的整段分享口令 (例如: 00年个人接... https://xhslink.cn/...)"
                        class="w-full p-3 text-sm bg-slate-50 border border-slate-200 rounded-xl focus:outline-none focus:ring-2 focus:ring-red-500 focus:bg-white transition"></textarea>
                </div>

                <button onclick="claimMaterial(true)" id="claimBtn" class="w-full py-3.5 bg-emerald-600 hover:bg-emerald-700 text-white rounded-xl font-bold text-sm shadow-md shadow-emerald-600/20 transition flex items-center justify-center space-x-2">
                    <span>🚀 提交小红书打卡链接</span>
                </button>
            </div>

            <!-- State 3: In 60-Minute Anti-Rate-Limit Cooldown -->
            <div id="cooldownBox" class="mt-3 p-4 bg-gradient-to-br from-amber-50 to-orange-50 rounded-2xl border border-amber-200/80 text-center space-y-2.5 hidden">
                <div class="inline-flex items-center space-x-1.5 px-3 py-1 bg-amber-100/90 text-amber-900 rounded-full text-xs font-bold shadow-sm">
                    <span class="animate-pulse">⏳</span>
                    <span>小红书养号防限流保护中</span>
                </div>
                <div>
                    <div class="text-[11px] font-semibold text-amber-800">距离可领取下一组素材还需等待：</div>
                    <div class="text-3xl sm:text-4xl font-black tracking-widest text-amber-900 font-mono py-1 drop-shadow-sm" id="cooldownTimerDisplay">
                        59:59
                    </div>
                </div>
                <div class="p-2.5 bg-white/80 rounded-xl text-left border border-amber-200/60 text-[11px] text-amber-900 leading-relaxed space-y-1">
                    <p>📌 <strong>防封限流说明</strong>：小红书单号短时间内频繁连续发帖易被系统判定为营销脚本降权限流。</p>
                    <p>🛡️ 系统已开启 <strong>60 分钟安全间隔保护</strong>，倒计时结束后将自动解锁领取下一篇独家素材！</p>
                </div>
            </div>
        </div>

        <!-- Material Details Card (Show when a material is active) -->
        <div id="materialContentCard" class="bg-white rounded-2xl p-5 shadow-sm border border-slate-200 hidden space-y-4">
            <div class="flex items-center justify-between border-b border-slate-100 pb-3">
                <div class="flex items-center space-x-2">
                    <span class="px-2.5 py-0.5 rounded-full text-xs font-bold bg-red-100 text-red-700" id="matBadge">第01组</span>
                    <h3 class="font-bold text-sm text-slate-900 truncate max-w-[200px] sm:max-w-sm" id="matTitle">文案标题</h3>
                </div>
                <span class="text-xs text-slate-400" id="matAssignedTime">刚刚分配</span>
            </div>

            <!-- 3 Images Grid -->
            <div>
                <div class="flex items-center justify-between mb-2">
                    <span class="text-xs font-bold text-slate-700">🖼️ 发布配图 (按 1、2、3 顺序配图)：</span>
                    <div class="flex items-center space-x-2">
                        <a id="downloadZipBtn" href="#" class="text-xs bg-slate-100 hover:bg-slate-200 text-slate-700 px-2.5 py-1 rounded-lg font-bold transition border border-slate-200 flex items-center space-x-1">
                            <span>📥 一键打包下载 (ZIP)</span>
                        </a>
                        <span class="text-xs text-red-500 font-medium hidden sm:inline">💡 点击放大 · 长按保存</span>
                    </div>
                </div>
                <div class="grid grid-cols-3 gap-2" id="imagesGrid">
                </div>
            </div>

            <!-- Copywriting Text Box -->
            <div>
                <div class="flex items-center justify-between mb-2">
                    <span class="text-xs font-bold text-slate-700">📝 发布文案 (第一行为标题，全选直接发布)：</span>
                    <button onclick="copyCopywriting()" id="copyBtn" class="text-xs bg-red-50 hover:bg-red-100 text-red-600 font-bold px-3 py-1 rounded-lg transition border border-red-200 flex items-center space-x-1">
                        <span>📋 一键复制文案</span>
                    </button>
                </div>
                <div class="relative">
                    <pre id="copyTextPre" class="p-3.5 bg-slate-50 border border-slate-200 rounded-xl text-xs text-slate-800 whitespace-pre-wrap font-sans max-h-60 overflow-y-auto leading-relaxed select-all"></pre>
                </div>
            </div>

            <!-- STRICT SOP CARD FOR WORKERS (Anti-ban & Customer Traffic) -->
            <div class="p-4 bg-rose-50/80 rounded-2xl border border-rose-200 text-xs text-rose-950 space-y-2">
                <div class="font-bold text-rose-900 flex items-center space-x-1.5 text-xs">
                    <span>🔥</span>
                    <span>小红书发布规范与引流防封 SOP 铁律</span>
                </div>
                <div class="space-y-1 text-[11px] leading-relaxed text-rose-900">
                    <p>1. <strong>发布顺序</strong>：严格按【图1·封面 ➔ 图2·内容 ➔ 图3·尾图】顺序发布；</p>
                    <p>2. <strong>评论区互动</strong>：有客户在评论区留言咨询时，统一回复“<strong>已私</strong>”，<strong>严禁在评论区直接发微信号或手机号</strong>（会被小红书直接禁言）；</p>
                    <p>3. <strong>私信引流技巧</strong>：在私信中发送事先准备好的引导视频/主页背景图引导，切勿硬发纯文字微信号；</p>
                    <p>4. <strong>提成结算</strong>：客户成功添加微信后，截图发给 3金 当天结算高额提成！</p>
                </div>
            </div>
        </div>

        <!-- History Submissions -->
        <div id="historyCard" class="bg-white rounded-2xl p-5 shadow-sm border border-slate-200 hidden">
            <h3 class="font-bold text-sm text-slate-900 mb-3 flex items-center space-x-1.5">
                <span>📑</span>
                <span>我的打卡记录与结算状态</span>
            </h3>
            <div class="space-y-2 text-xs" id="historyList">
            </div>
        </div>

    </main>

    <!-- Admin Password Prompt Modal -->
    <div id="adminLoginModal" class="fixed inset-0 z-50 bg-black/60 backdrop-blur-sm flex items-center justify-center p-4 hidden">
        <div class="bg-white rounded-2xl max-w-sm w-full p-5 shadow-2xl space-y-4">
            <div class="text-center">
                <div class="w-12 h-12 rounded-full bg-amber-100 text-amber-600 flex items-center justify-center mx-auto text-2xl mb-2">👑</div>
                <h3 class="font-bold text-base text-slate-900">3金 管理后台登录</h3>
                <p class="text-xs text-slate-400 mt-0.5">请输入管理员密码</p>
            </div>
            <div>
                <input type="password" id="adminPwdInput" placeholder="输入管理密码" 
                    class="w-full px-3 py-2.5 text-sm bg-slate-50 border border-slate-200 rounded-xl focus:outline-none focus:ring-2 focus:ring-amber-500 focus:bg-white text-center font-bold tracking-widest"
                    onkeypress="if(event.key==='Enter') verifyAdminLogin()">
            </div>
            <div class="flex gap-2">
                <button onclick="closeAdminLogin()" class="flex-1 py-2.5 text-xs bg-slate-100 hover:bg-slate-200 text-slate-600 rounded-xl font-bold transition">取消</button>
                <button onclick="verifyAdminLogin()" class="flex-1 py-2.5 text-xs bg-slate-900 hover:bg-slate-800 text-white rounded-xl font-bold transition shadow-sm">进入后台</button>
            </div>
            <div class="text-center pt-1 border-t border-slate-100">
                <button onclick="openAdminResetModal()" class="text-[11px] text-amber-600 hover:text-amber-700 font-bold transition hover:underline">
                    ❓ 忘记密码？通过安全密保找回 / 重置
                </button>
            </div>
        </div>
    </div>

    <!-- Admin Reset Password by Security Question Modal -->
    <div id="adminResetModal" class="fixed inset-0 z-50 bg-black/60 backdrop-blur-sm flex items-center justify-center p-4 hidden">
        <div class="bg-white rounded-2xl max-w-sm w-full p-5 shadow-2xl space-y-4">
            <div class="text-center">
                <div class="w-12 h-12 rounded-full bg-red-100 text-red-600 flex items-center justify-center mx-auto text-2xl mb-2">🛡️</div>
                <h3 class="font-bold text-base text-slate-900">密保重置管理员密码</h3>
                <p class="text-xs text-slate-400 mt-0.5">回答安全密保暗号即可重置并登录</p>
            </div>
            <div class="bg-amber-50 p-3 rounded-xl border border-amber-200 space-y-1">
                <div class="text-[11px] font-bold text-amber-800">密保问题：</div>
                <div id="resetModalQuestionText" class="text-xs font-bold text-slate-800 bg-white p-2 rounded-lg border border-amber-200">加载中...</div>
            </div>
            <div class="space-y-2">
                <div>
                    <label class="block text-[11px] font-bold text-slate-700 mb-0.5">密保答案：</label>
                    <input type="text" id="resetSecurityAnswer" placeholder="输入密保答案" 
                        class="w-full px-3 py-2 text-xs bg-slate-50 border border-slate-200 rounded-xl focus:outline-none focus:ring-2 focus:ring-red-500 focus:bg-white font-bold">
                </div>
                <div>
                    <label class="block text-[11px] font-bold text-slate-700 mb-0.5">新管理密码：</label>
                    <input type="password" id="resetNewPassword" placeholder="输入新密码" 
                        class="w-full px-3 py-2 text-xs bg-slate-50 border border-slate-200 rounded-xl focus:outline-none focus:ring-2 focus:ring-red-500 focus:bg-white font-bold">
                </div>
                <div>
                    <label class="block text-[11px] font-bold text-slate-700 mb-0.5">确认新密码：</label>
                    <input type="password" id="resetNewPasswordConfirm" placeholder="再次输入新密码" 
                        class="w-full px-3 py-2 text-xs bg-slate-50 border border-slate-200 rounded-xl focus:outline-none focus:ring-2 focus:ring-red-500 focus:bg-white font-bold">
                </div>
            </div>
            <div class="flex gap-2 pt-1">
                <button onclick="closeAdminResetModal()" class="flex-1 py-2.5 text-xs bg-slate-100 hover:bg-slate-200 text-slate-600 rounded-xl font-bold transition">返回</button>
                <button onclick="submitResetAdminPassword()" class="flex-1 py-2.5 text-xs bg-red-600 hover:bg-red-700 text-white rounded-xl font-bold transition shadow-sm">🚀 验证并重置</button>
            </div>
        </div>
    </div>

    <!-- Admin Change Password & Security Settings Modal -->
    <div id="adminSecurityModal" class="fixed inset-0 z-50 bg-black/60 backdrop-blur-sm flex items-center justify-center p-4 hidden">
        <div class="bg-white rounded-2xl max-w-md w-full p-5 shadow-2xl space-y-4 max-h-[92vh] overflow-y-auto">
            <div class="flex items-center justify-between border-b border-slate-100 pb-3">
                <div class="flex items-center space-x-2">
                    <span class="text-xl">🛡️</span>
                    <div>
                        <h3 class="font-bold text-base text-slate-900">管理员密码与密保管理</h3>
                        <p class="text-[11px] text-slate-400">修改密码需验证密保答案，防止未授权修改</p>
                    </div>
                </div>
                <button onclick="closeAdminSecurityModal()" class="text-slate-400 hover:text-slate-600 text-xl font-bold">&times;</button>
            </div>
            
            <div class="space-y-3 text-xs">
                <div>
                    <label class="block font-bold text-slate-700 mb-1">🔑 当前管理员原密码：</label>
                    <input type="password" id="secOldPassword" placeholder="输入当前管理密码" 
                        class="w-full px-3 py-2 bg-slate-50 border border-slate-200 rounded-xl font-bold focus:outline-none focus:ring-2 focus:ring-amber-500 focus:bg-white">
                </div>

                <div class="bg-amber-50/80 p-3 rounded-xl border border-amber-200 space-y-2">
                    <div class="text-[11px] font-bold text-amber-900 flex items-center space-x-1">
                        <span>🛡️ 安全密保问题：</span>
                    </div>
                    <div id="secCurrentQuestionDisplay" class="font-bold text-slate-900 bg-white p-2.5 rounded-lg border border-amber-200 text-xs">
                        加载中...
                    </div>
                    <div>
                        <label class="block font-bold text-slate-700 mb-1">💬 请输入密保答案进行安全核验：</label>
                        <input type="text" id="secSecurityAnswer" placeholder="输入密保答案" 
                            class="w-full px-3 py-2 bg-white border border-amber-300 rounded-xl font-bold focus:outline-none focus:ring-2 focus:ring-amber-500">
                    </div>
                </div>

                <div class="grid grid-cols-1 sm:grid-cols-2 gap-2">
                    <div>
                        <label class="block font-bold text-slate-700 mb-1">🆕 设置新管理密码：</label>
                        <input type="password" id="secNewPassword" placeholder="输入新密码" 
                            class="w-full px-3 py-2 bg-slate-50 border border-slate-200 rounded-xl font-bold focus:outline-none focus:ring-2 focus:ring-amber-500 focus:bg-white">
                    </div>
                    <div>
                        <label class="block font-bold text-slate-700 mb-1">🆕 确认新管理密码：</label>
                        <input type="password" id="secNewPasswordConfirm" placeholder="再次输入新密码" 
                            class="w-full px-3 py-2 bg-slate-50 border border-slate-200 rounded-xl font-bold focus:outline-none focus:ring-2 focus:ring-amber-500 focus:bg-white">
                    </div>
                </div>

                <div class="pt-2 border-t border-slate-100">
                    <button type="button" onclick="toggleCustomQuestionBox()" class="text-[11px] text-amber-600 hover:text-amber-700 font-bold transition flex items-center space-x-1">
                        <span>🔄 顺便修改密保问题与密保答案 (可选) ▼</span>
                    </button>
                    <div id="customQuestionBox" class="mt-2 space-y-2 p-3 bg-slate-50 rounded-xl border border-slate-200 hidden">
                        <div>
                            <label class="block text-[11px] font-bold text-slate-600 mb-0.5">新的密保问题：</label>
                            <input type="text" id="secNewCustomQuestion" placeholder="如: 你的初中名字/专属暗号" 
                                class="w-full px-3 py-1.5 bg-white border border-slate-300 rounded-lg text-xs font-medium">
                        </div>
                        <div>
                            <label class="block text-[11px] font-bold text-slate-600 mb-0.5">新的密保答案：</label>
                            <input type="text" id="secNewCustomAnswer" placeholder="如: 自定义新答案" 
                                class="w-full px-3 py-1.5 bg-white border border-slate-300 rounded-lg text-xs font-medium">
                        </div>
                    </div>
                </div>
            </div>

            <div class="flex gap-2 pt-2 border-t border-slate-100">
                <button onclick="closeAdminSecurityModal()" class="flex-1 py-2.5 text-xs bg-slate-100 hover:bg-slate-200 text-slate-600 rounded-xl font-bold transition">取消</button>
                <button onclick="submitChangeAdminPassword()" class="flex-1 py-2.5 text-xs bg-amber-600 hover:bg-amber-700 text-white rounded-xl font-bold transition shadow-sm">💾 验证密保并更新</button>
            </div>
        </div>
    </div>

    <!-- Admin Dashboard Modal -->
    <div id="adminModal" class="fixed inset-0 z-50 bg-black/60 backdrop-blur-sm flex items-center justify-center p-4 hidden">
        <div class="bg-white rounded-2xl max-w-4xl w-full max-h-[94vh] flex flex-col shadow-2xl overflow-hidden">
            <!-- Modal Header -->
            <div class="px-5 py-4 border-b border-slate-100 flex items-center justify-between bg-slate-900 text-white">
                <div class="flex items-center space-x-2">
                    <span class="text-xl">👑</span>
                    <div>
                        <h2 class="font-bold text-base">3金 的矩阵管理后台</h2>
                        <p class="text-xs text-slate-400">白名单标签 · 24h存活巡检 · 结算台账 · 超时释放</p>
                    </div>
                </div>
                <div class="flex items-center space-x-2">
                    <button onclick="logoutAdmin()" title="安全退出管理后台" class="text-[11px] bg-red-500/20 hover:bg-red-500/30 text-red-300 hover:text-red-200 px-2.5 py-1 rounded-lg font-bold transition flex items-center space-x-1 border border-red-500/30">
                        <span>🚪 退出登录</span>
                    </button>
                    <button onclick="toggleAdminModal()" class="text-slate-400 hover:text-white text-xl font-bold p-1">&times;</button>
                </div>
            </div>

            <!-- Modal Content (Scrollable) -->
            <div class="p-5 overflow-y-auto space-y-5 flex-1">
                
                <!-- Stat Cards -->
                <div class="grid grid-cols-2 sm:grid-cols-4 gap-3">
                    <div class="bg-slate-50 p-3 rounded-xl border border-slate-200 text-center">
                        <div class="text-[11px] text-slate-500 font-medium">总素材组数</div>
                        <div class="text-xl font-bold text-slate-900 mt-0.5" id="statTotal">0</div>
                    </div>
                    <div class="bg-emerald-50 p-3 rounded-xl border border-emerald-100 text-center">
                        <div class="text-[11px] text-emerald-700 font-medium">剩余待领 (独家)</div>
                        <div class="text-xl font-bold text-emerald-600 mt-0.5" id="statAvailable">0</div>
                    </div>
                    <div class="bg-amber-50 p-3 rounded-xl border border-amber-100 text-center">
                        <div class="text-[11px] text-amber-700 font-medium">⏳ 待结算篇数</div>
                        <div class="text-xl font-bold text-amber-600 mt-0.5" id="statUnsettled">0</div>
                    </div>
                    <div class="bg-blue-50 p-3 rounded-xl border border-blue-100 text-center">
                        <div class="text-[11px] text-blue-700 font-medium">✅ 已结算打卡</div>
                        <div class="text-xl font-bold text-blue-600 mt-0.5" id="statSettled">0</div>
                    </div>
                </div>

                <!-- Security, Tag-based Whitelist & Anti-Cheat Card -->
                <div class="p-4 bg-amber-50/80 rounded-2xl border border-amber-200 space-y-3.5">
                    <div class="flex items-center justify-between border-b border-amber-200/60 pb-2">
                        <h3 class="font-bold text-xs text-amber-900 flex items-center space-x-1.5 uppercase tracking-wider">
                            <span>🛡️</span>
                            <span>兼职白名单与防作弊规则管理</span>
                        </h3>
                        <div class="flex items-center space-x-2">
                            <button onclick="releaseExpiredAssignments()" class="text-[10px] bg-amber-200 hover:bg-amber-300 text-amber-900 px-2 py-0.5 rounded font-bold transition">
                                🔄 立即释放超时领料
                            </button>
                            <span class="text-[10px] text-amber-800 bg-amber-200/80 px-2 py-0.5 rounded font-bold">可点选 + 增删</span>
                        </div>
                    </div>

                    <div class="grid grid-cols-1 sm:grid-cols-4 gap-3 text-xs">
                        <div>
                            <label class="block font-semibold text-slate-700 mb-1">1. 领料验证模式：</label>
                            <select id="settingAuthMode" onchange="saveAdminSettingsSilently()" class="w-full px-3 py-1.5 bg-white border border-amber-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-amber-500 font-bold text-amber-900 text-xs">
                                <option value="whitelist">📋 仅白名单授权 (默认/推荐)</option>
                                <option value="passcode">🔑 仅验证口令</option>
                                <option value="both">🔒 双重验证 (白名单+口令)</option>
                                <option value="none">🌐 完全开放模式 (免验证)</option>
                            </select>
                        </div>

                        <div>
                            <label class="block font-semibold text-slate-700 mb-1">2. 每日领料上限 (组)：</label>
                            <input type="number" id="settingDailyLimit" step="1" min="1" max="100" placeholder="如: 3" 
                                class="w-full px-3 py-1.5 bg-white border border-amber-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-amber-500 font-bold text-amber-900 text-xs">
                        </div>

                        <div>
                            <label class="block font-semibold text-slate-700 mb-1">3. 超时退回 (小时)：</label>
                            <input type="number" id="settingTimeoutHours" step="0.5" min="0.5" max="24" placeholder="如: 2" 
                                class="w-full px-3 py-1.5 bg-white border border-amber-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-amber-500 font-bold text-amber-900 text-xs">
                        </div>

                        <div>
                            <label class="block font-semibold text-slate-700 mb-1">4. 🔐 密码与密保管理：</label>
                            <button type="button" onclick="openAdminSecurityModal()" 
                                class="w-full px-3 py-1.5 bg-red-50 hover:bg-red-100 text-red-700 border border-red-300 rounded-lg text-xs font-bold transition flex items-center justify-center space-x-1 shadow-sm">
                                <span>🛡️ 修改密码 & 密保</span>
                            </button>
                        </div>
                    </div>

                    <!-- INTERACTIVE WHITELIST TAGS MANAGER -->
                    <div class="pt-2 border-t border-amber-200/60 space-y-2">
                        <div class="flex items-center justify-between">
                            <label class="block font-semibold text-slate-700 text-xs">
                                4. 已授权兼职人员标签列表（点击 ✕ 即可删除）：
                            </label>
                            <span class="text-[10px] text-amber-700 font-bold" id="whitelistCountBadge">当前 0 人</span>
                        </div>

                        <!-- Tag Pills Container -->
                        <div id="whitelistTagsContainer" class="flex flex-wrap gap-1.5 p-2 bg-white rounded-xl border border-amber-200 min-h-[44px] items-center">
                        </div>

                        <!-- Add New Worker Tag Input with [+] Button -->
                        <div class="flex gap-2">
                            <input type="text" id="newWhitelistItemInput" placeholder="输入兼职微信昵称 / 姓名 (按回车或点加号)" 
                                class="flex-1 px-3 py-1.5 bg-white border border-amber-300 rounded-xl text-xs focus:outline-none focus:ring-2 focus:ring-amber-500 font-medium"
                                onkeypress="if(event.key==='Enter') addWhitelistItem()">
                            <button onclick="addWhitelistItem()" class="px-4 py-1.5 bg-amber-600 hover:bg-amber-700 text-white rounded-xl text-xs font-bold shadow-sm transition flex items-center space-x-1">
                                <span>➕ 添加兼职</span>
                            </button>
                        </div>

                        <!-- Quick-Pick Chips from Active Registered Workers -->
                        <div id="quickAddWorkersBox" class="pt-1 flex items-center gap-1.5 text-[11px] text-slate-500 flex-wrap hidden">
                            <span class="font-semibold text-slate-700">💡 快捷点击添加：</span>
                            <div id="quickWorkerChips" class="flex flex-wrap gap-1"></div>
                        </div>
                    </div>

                    <div class="flex justify-end pt-1">
                        <button onclick="saveAdminSettings()" class="px-5 py-2 bg-amber-600 hover:bg-amber-700 text-white rounded-xl text-xs font-bold shadow-sm transition">
                            💾 保存所有设置
                        </button>
                    </div>
                </div>

                <!-- UPLOAD TABS CONTAINER -->
                <div class="border border-emerald-200 bg-emerald-50/70 rounded-2xl p-4 space-y-3">
                    <!-- Tab Switcher -->
                    <div class="flex items-center space-x-2 border-b border-emerald-200/80 pb-3 flex-wrap gap-1">
                        <button onclick="switchUploadTab('pipeline')" id="tabBtnPipeline" class="px-3.5 py-1.5 rounded-xl text-xs font-bold bg-emerald-600 text-white shadow-sm transition">
                            🏭 【流水线自动装配池】(零散传图，3槽+文案≥1自动吐出成品)
                        </button>
                        <button onclick="switchUploadTab('batch')" id="tabBtnBatch" class="px-3.5 py-1.5 rounded-xl text-xs font-bold bg-white text-slate-700 border border-slate-200 hover:bg-slate-50 transition">
                            ⚡️ 【批量图库一次性拼装】
                        </button>
                        <button onclick="switchUploadTab('single')" id="tabBtnSingle" class="px-3.5 py-1.5 rounded-xl text-xs font-bold bg-white text-slate-700 border border-slate-200 hover:bg-slate-50 transition">
                            📌 【单组精准上传】
                        </button>
                    </div>

                    <!-- TAB 1: PIPELINE AUTO ASSEMBLER QUEUE (NEW) -->
                    <div id="pipelineUploadPanel" class="space-y-3 text-xs">
                        <!-- Pipeline Live Status Banner -->
                        <div class="p-3 bg-white rounded-xl border border-emerald-200 shadow-sm space-y-2.5">
                            <div class="flex items-center justify-between flex-wrap gap-2">
                                <div class="flex items-center space-x-2">
                                    <span class="text-xs font-bold text-slate-800">🏭 当前零件缓冲箱监控：</span>
                                    <span id="pipelineStatusTip" class="text-[11px] text-emerald-800 font-medium">随时随地随手扔图，各模块 ≥ 1 立即自动装配</span>
                                </div>
                                <div class="flex items-center space-x-2">
                                    <button onclick="clearPipelineBuffer('all')" class="text-[10px] text-slate-400 hover:text-red-600 transition font-medium">
                                        🗑️ 一键清空所有缓冲箱
                                    </button>
                                    <button onclick="refreshPipelineStatus()" class="text-[10px] text-emerald-700 bg-emerald-50 px-2 py-0.5 rounded border border-emerald-200 font-bold hover:bg-emerald-100 transition">
                                        🔄 刷新库存
                                    </button>
                                </div>
                            </div>

                            <!-- 4 Buffer Counter Chips -->
                            <div class="grid grid-cols-2 sm:grid-cols-4 gap-2 text-center text-xs">
                                <div class="p-2 bg-emerald-50 rounded-lg border border-emerald-200">
                                    <div class="text-slate-500 text-[10px] font-medium">🖼️ 封面图池 (图1)</div>
                                    <div id="bufCountCovers" class="text-base font-black text-emerald-700 mt-0.5">0 张</div>
                                    <div id="bufBadgeCovers" class="text-[9px] font-bold text-amber-600">待补充</div>
                                </div>
                                <div class="p-2 bg-blue-50 rounded-lg border border-blue-200">
                                    <div class="text-slate-500 text-[10px] font-medium">🖼️ 内容图池 (图2)</div>
                                    <div id="bufCountContents" class="text-base font-black text-blue-700 mt-0.5">0 张</div>
                                    <div id="bufBadgeContents" class="text-[9px] font-bold text-amber-600">待补充</div>
                                </div>
                                <div class="p-2 bg-purple-50 rounded-lg border border-purple-200">
                                    <div class="text-slate-500 text-[10px] font-medium">🖼️ 尾图池 (图3)</div>
                                    <div id="bufCountEnds" class="text-base font-black text-purple-700 mt-0.5">0 张</div>
                                    <div id="bufBadgeEnds" class="text-[9px] font-bold text-amber-600">待补充</div>
                                </div>
                                <div class="p-2 bg-amber-50 rounded-lg border border-amber-200">
                                    <div class="text-slate-500 text-[10px] font-medium">📝 文案池</div>
                                    <div id="bufCountCopies" class="text-base font-black text-amber-700 mt-0.5">0 篇</div>
                                    <div id="bufBadgeCopies" class="text-[9px] font-bold text-amber-600">待补充</div>
                                </div>
                            </div>

                            <!-- Auto Assembly Progress Banner -->
                            <div id="pipelineAssembleAlert" class="p-2 rounded-lg bg-slate-50 border border-slate-200 text-slate-600 text-[11px] flex items-center justify-between">
                                <span id="pipelineAlertText">💡 提示：4 个箱子均有图文（≥ 1）时，系统会自动消耗并组装成 100% 独家新作品进入下方素材库！</span>
                            </div>
                        </div>

                        <!-- 4 Drop Modules -->
                        <div class="grid grid-cols-1 sm:grid-cols-3 gap-2.5">
                            <!-- Drop Slot 1: Covers -->
                            <div class="bg-white p-3 rounded-xl border border-emerald-300 space-y-2">
                                <div class="font-bold text-emerald-800 flex items-center justify-between">
                                    <span>➕ 扔入【图1·封面图】</span>
                                    <button onclick="clearPipelineBuffer('covers')" class="text-[9px] text-slate-400 hover:text-red-500">清空此箱</button>
                                </div>
                                <input type="file" id="pipeSlot1" multiple accept="image/*,video/*,.heic,.mov" onchange="uploadPipelineSlot('covers', 'pipeSlot1')"
                                    class="w-full text-[10px] text-slate-500 file:mr-2 file:py-1 file:px-2 file:rounded file:border-0 file:text-[10px] file:font-semibold file:bg-emerald-600 file:text-white cursor-pointer">
                                <p class="text-[9px] text-slate-400">选择文件立即自动加入封面缓冲池</p>
                            </div>

                            <!-- Drop Slot 2: Contents -->
                            <div class="bg-white p-3 rounded-xl border border-blue-300 space-y-2">
                                <div class="font-bold text-blue-800 flex items-center justify-between">
                                    <span>➕ 扔入【图2·内容图】</span>
                                    <button onclick="clearPipelineBuffer('contents')" class="text-[9px] text-slate-400 hover:text-red-500">清空此箱</button>
                                </div>
                                <input type="file" id="pipeSlot2" multiple accept="image/*,video/*,.heic,.mov" onchange="uploadPipelineSlot('contents', 'pipeSlot2')"
                                    class="w-full text-[10px] text-slate-500 file:mr-2 file:py-1 file:px-2 file:rounded file:border-0 file:text-[10px] file:font-semibold file:bg-blue-600 file:text-white cursor-pointer">
                                <p class="text-[9px] text-slate-400">选择文件立即自动加入内容缓冲池</p>
                            </div>

                            <!-- Drop Slot 3: Ends -->
                            <div class="bg-white p-3 rounded-xl border border-purple-300 space-y-2">
                                <div class="font-bold text-purple-800 flex items-center justify-between">
                                    <span>➕ 扔入【图3·尾图】</span>
                                    <button onclick="clearPipelineBuffer('ends')" class="text-[9px] text-slate-400 hover:text-red-500">清空此箱</button>
                                </div>
                                <input type="file" id="pipeSlot3" multiple accept="image/*,video/*,.heic,.mov" onchange="uploadPipelineSlot('ends', 'pipeSlot3')"
                                    class="w-full text-[10px] text-slate-500 file:mr-2 file:py-1 file:px-2 file:rounded file:border-0 file:text-[10px] file:font-semibold file:bg-purple-600 file:text-white cursor-pointer">
                                <p class="text-[9px] text-slate-400">选择文件立即自动加入尾图缓冲池</p>
                            </div>
                        </div>

                        <!-- Drop Slot 4: Copies -->
                        <div class="bg-white p-3 rounded-xl border border-amber-300 space-y-2">
                            <div class="flex items-center justify-between font-bold text-amber-800">
                                <span>📝 批量补充文案 (多篇文案用 <code>===</code> 分隔)：</span>
                                <button onclick="clearPipelineBuffer('copies')" class="text-[9px] text-slate-400 hover:text-red-500">清空文案箱</button>
                            </div>
                            <textarea id="pipeCopyInput" rows="3" placeholder="第一篇文案内容...末尾带 #杭州代运营&#10;===&#10;第二篇文案内容...末尾带 #上海代运营&#10;===&#10;第三篇文案..." 
                                class="w-full p-2.5 bg-slate-50 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-amber-500 font-mono text-xs text-slate-800"></textarea>
                            <div class="flex justify-end">
                                <button onclick="uploadPipelineCopies()" id="pipeCopyBtn" class="px-4 py-1.5 bg-amber-600 hover:bg-amber-700 text-white rounded-lg text-xs font-bold transition shadow-sm flex items-center space-x-1">
                                    <span>📥 确认将文案加入文案箱并检测装配</span>
                                </button>
                            </div>
                        </div>
                    </div>

                    <!-- TAB 2: BATCH AUTO ASSEMBLER -->
                    <div id="batchUploadPanel" class="space-y-3 text-xs hidden">
                        <div class="p-2.5 bg-white/90 rounded-xl border border-emerald-200 text-emerald-900 leading-relaxed text-[11px]">
                            💡 <strong>批量拼装玩法</strong>：让同事在【图1】多选 20 张封面实况，在【图2】多选 20 张内容，在【图3】选尾图，下方粘贴 20 段文案（用 <code>===</code> 分隔），点击按钮系统<strong>1 秒自动拼装生成 20 组独家作品！</strong>
                        </div>

                        <div class="grid grid-cols-1 sm:grid-cols-3 gap-2.5">
                            <!-- Batch Slot 1 -->
                            <div class="bg-white p-3 rounded-xl border border-emerald-300 space-y-1.5">
                                <div class="font-bold text-emerald-800 flex items-center justify-between">
                                    <span>🖼️ 批量选【图1·封面图】</span>
                                    <span id="batchCount1" class="text-[10px] text-emerald-600 font-normal">已选 0 张</span>
                                </div>
                                <input type="file" id="batchSlot1" multiple accept="image/*,video/*,.heic,.mov" onchange="updateBatchCount(1)"
                                    class="w-full text-[10px] text-slate-500 file:mr-2 file:py-1 file:px-2 file:rounded file:border-0 file:text-[10px] file:font-semibold file:bg-emerald-600 file:text-white cursor-pointer">
                            </div>

                            <!-- Batch Slot 2 -->
                            <div class="bg-white p-3 rounded-xl border border-blue-300 space-y-1.5">
                                <div class="font-bold text-blue-800 flex items-center justify-between">
                                    <span>🖼️ 批量选【图2·内容图】</span>
                                    <span id="batchCount2" class="text-[10px] text-blue-600 font-normal">已选 0 张</span>
                                </div>
                                <input type="file" id="batchSlot2" multiple accept="image/*,video/*,.heic,.mov" onchange="updateBatchCount(2)"
                                    class="w-full text-[10px] text-slate-500 file:mr-2 file:py-1 file:px-2 file:rounded file:border-0 file:text-[10px] file:font-semibold file:bg-blue-600 file:text-white cursor-pointer">
                            </div>

                            <!-- Batch Slot 3 -->
                            <div class="bg-white p-3 rounded-xl border border-amber-300 space-y-1.5">
                                <div class="font-bold text-amber-800 flex items-center justify-between">
                                    <span>🖼️ 批量选【图3·尾图】</span>
                                    <span id="batchCount3" class="text-[10px] text-amber-600 font-normal">已选 0 张</span>
                                </div>
                                <input type="file" id="batchSlot3" multiple accept="image/*,video/*,.heic,.mov" onchange="updateBatchCount(3)"
                                    class="w-full text-[10px] text-slate-500 file:mr-2 file:py-1 file:px-2 file:rounded file:border-0 file:text-[10px] file:font-semibold file:bg-amber-600 file:text-white cursor-pointer">
                            </div>
                        </div>

                        <div>
                            <label class="block font-semibold text-slate-700 mb-1">
                                批量文案池 (每篇文案用三个等号 <code>===</code> 隔开，第一行自动作为标题)：
                            </label>
                            <textarea id="batchCopyInput" rows="5" placeholder="第一篇文案内容...末尾带 #杭州代运营&#10;===&#10;第二篇文案内容...末尾带 #上海代运营&#10;===&#10;第三篇文案内容..." 
                                class="w-full p-2.5 bg-white border border-emerald-300 rounded-xl focus:outline-none focus:ring-2 focus:ring-emerald-500 font-medium text-slate-800 text-xs font-mono"></textarea>
                        </div>

                        <div class="flex items-center justify-between pt-1">
                            <input type="text" id="batchPrefix" placeholder="组名前缀 (如: 代运营矩阵_)" value="代运营矩阵_"
                                class="px-3 py-1.5 bg-white border border-emerald-300 rounded-lg text-xs w-48 font-medium">
                            <button onclick="submitBatchMaterials()" id="batchSubmitBtn" class="px-5 py-2.5 bg-emerald-600 hover:bg-emerald-700 text-white rounded-xl text-xs font-bold shadow-md shadow-emerald-600/20 transition flex items-center space-x-1">
                                <span>⚡️ 一键批量自动组装并入库</span>
                            </button>
                        </div>
                    </div>

                    <!-- TAB 3: SINGLE SLOT UPLOAD -->
                    <div id="singleUploadPanel" class="space-y-3 text-xs hidden">
                        <div>
                            <label class="block font-semibold text-slate-700 mb-1">作品组名 / 标题：</label>
                            <input type="text" id="newGroupInput" placeholder="例如: 第13组_8年单干老手聊聊代运营" 
                                class="w-full px-3 py-2 bg-white border border-emerald-300 rounded-xl focus:outline-none focus:ring-2 focus:ring-emerald-500 font-medium text-xs">
                        </div>

                        <!-- 3 SEPARATE UPLOAD SLOTS -->
                        <div class="grid grid-cols-1 sm:grid-cols-3 gap-2.5">
                            <div class="bg-white p-3 rounded-xl border-2 border-dashed border-emerald-300 space-y-2 flex flex-col justify-between">
                                <div>
                                    <div class="flex items-center justify-between font-bold text-slate-800 mb-1">
                                        <span class="text-emerald-700">🖼️ 图1 · 封面图</span>
                                        <span class="text-[10px] text-red-500 bg-red-50 px-1.5 py-0.5 rounded">必填</span>
                                    </div>
                                    <p class="text-[10px] text-slate-400">实况动图 / 封面原图</p>
                                </div>
                                <input type="file" id="slot1File" accept="image/*,video/*,.heic,.mov" onchange="previewSlot(1)"
                                    class="w-full text-[10px] text-slate-500 file:mr-2 file:py-1 file:px-2 file:rounded file:border-0 file:text-[10px] file:font-semibold file:bg-emerald-600 file:text-white cursor-pointer">
                                <div id="slot1Preview" class="h-16 bg-slate-50 rounded-lg flex items-center justify-center text-[10px] text-slate-400 border border-slate-100 overflow-hidden">
                                    待选图1
                                </div>
                            </div>

                            <div class="bg-white p-3 rounded-xl border-2 border-dashed border-blue-300 space-y-2 flex flex-col justify-between">
                                <div>
                                    <div class="flex items-center justify-between font-bold text-slate-800 mb-1">
                                        <span class="text-blue-700">🖼️ 图2 · 内容图</span>
                                        <span class="text-[10px] text-blue-500 bg-blue-50 px-1.5 py-0.5 rounded">选填</span>
                                    </div>
                                    <p class="text-[10px] text-slate-400">正文详情实况 / 图表</p>
                                </div>
                                <input type="file" id="slot2File" accept="image/*,video/*,.heic,.mov" onchange="previewSlot(2)"
                                    class="w-full text-[10px] text-slate-500 file:mr-2 file:py-1 file:px-2 file:rounded file:border-0 file:text-[10px] file:font-semibold file:bg-blue-600 file:text-white cursor-pointer">
                                <div id="slot2Preview" class="h-16 bg-slate-50 rounded-lg flex items-center justify-center text-[10px] text-slate-400 border border-slate-100 overflow-hidden">
                                    待选图2
                                </div>
                            </div>

                            <div class="bg-white p-3 rounded-xl border-2 border-dashed border-amber-300 space-y-2 flex flex-col justify-between">
                                <div>
                                    <div class="flex items-center justify-between font-bold text-slate-800 mb-1">
                                        <span class="text-amber-700">🖼️ 图3 · 尾图</span>
                                        <span class="text-[10px] text-amber-500 bg-amber-50 px-1.5 py-0.5 rounded">选填</span>
                                    </div>
                                    <p class="text-[10px] text-slate-400">引导转化 / 尾图</p>
                                </div>
                                <input type="file" id="slot3File" accept="image/*,video/*,.heic,.mov" onchange="previewSlot(3)"
                                    class="w-full text-[10px] text-slate-500 file:mr-2 file:py-1 file:px-2 file:rounded file:border-0 file:text-[10px] file:font-semibold file:bg-amber-600 file:text-white cursor-pointer">
                                <div id="slot3Preview" class="h-16 bg-slate-50 rounded-lg flex items-center justify-center text-[10px] text-slate-400 border border-slate-100 overflow-hidden">
                                    待选图3
                                </div>
                            </div>
                        </div>

                        <div>
                            <label class="block font-semibold text-slate-700 mb-1">发布文案：</label>
                            <textarea id="newCopyInput" rows="3" placeholder="粘贴单篇文案..." 
                                class="w-full p-2.5 bg-white border border-emerald-300 rounded-xl focus:outline-none focus:ring-2 focus:ring-emerald-500 font-medium text-slate-800 text-xs"></textarea>
                        </div>

                        <div class="flex justify-end pt-1">
                            <button onclick="submit3SlotsMaterial()" id="add3SlotsBtn" class="px-5 py-2.5 bg-emerald-600 hover:bg-emerald-700 text-white rounded-xl text-xs font-bold shadow-md shadow-emerald-600/20 transition flex items-center space-x-1">
                                <span>🚀 保存单组并派入素材池</span>
                            </button>
                        </div>
                    </div>
                </div>

                <!-- Material Inventory Management -->
                <div>
                    <div class="flex items-center justify-between mb-2">
                        <h3 class="font-bold text-xs text-slate-800 uppercase tracking-wider">📦 素材库存状态清单 (一客一单 · 独家防复用)：</h3>
                        <button onclick="clearCompletedMaterials()" class="text-[11px] text-red-600 hover:text-red-700 font-bold">
                            🗑️ 一键清空所有已消耗素材
                        </button>
                    </div>
                    <div class="border border-slate-200 rounded-xl overflow-hidden max-h-52 overflow-y-auto">
                        <table class="w-full text-xs text-left border-collapse">
                            <thead class="bg-slate-100 text-slate-600 font-semibold border-b border-slate-200">
                                <tr>
                                    <th class="p-2">组号/名称</th>
                                    <th class="p-2">标题</th>
                                    <th class="p-2">尾Tag</th>
                                    <th class="p-2">状态</th>
                                    <th class="p-2">领走人</th>
                                    <th class="p-2 text-right">操作</th>
                                </tr>
                            </thead>
                            <tbody id="adminMaterialsBody" class="divide-y divide-slate-100">
                                <tr><td colspan="6" class="p-4 text-center text-slate-400">加载中...</td></tr>
                            </tbody>
                        </table>
                    </div>
                </div>

                <!-- Data Query & Submissions Table & Settlement Ledger -->
                <div class="p-3.5 bg-slate-50 rounded-2xl border border-slate-200 space-y-3">
                    <div class="flex items-center justify-between flex-wrap gap-2">
                        <h3 class="font-bold text-xs text-slate-900 uppercase tracking-wider flex items-center space-x-1.5">
                            <span>🔍</span>
                            <span>兼职回传数据精准查询与结算台账</span>
                        </h3>
                        <div class="flex items-center space-x-2">
                            <button onclick="inspectSurvivalStatus()" id="inspectBtn" class="text-[11px] bg-blue-50 hover:bg-blue-100 text-blue-700 border border-blue-200 px-2.5 py-1 rounded-lg font-bold transition flex items-center space-x-1 shadow-sm">
                                <span>🔍 24h存活巡检</span>
                            </button>
                            <button onclick="exportAdminCsv()" class="text-[11px] bg-emerald-50 hover:bg-emerald-100 text-emerald-700 border border-emerald-200 px-2.5 py-1 rounded-lg font-bold transition flex items-center space-x-1 shadow-sm">
                                <span>📥 导出 Excel 账单</span>
                            </button>
                        </div>
                    </div>

                    <!-- Multi-dimensional Filter Bar -->
                    <div class="p-3 bg-white rounded-xl border border-slate-200 space-y-2.5 text-xs shadow-sm">
                        <div class="grid grid-cols-1 sm:grid-cols-3 gap-2.5">
                            <!-- Worker Name Filter -->
                            <div>
                                <label class="block text-[11px] font-semibold text-slate-600 mb-1">👤 按兼职/白名单姓名筛选：</label>
                                <select id="filterWorkerSelect" onchange="applySubmissionsFilter()" class="w-full px-2.5 py-1.5 bg-slate-50 border border-slate-200 rounded-lg text-xs font-semibold text-slate-800 focus:outline-none focus:ring-2 focus:ring-blue-500">
                                    <option value="">-- 全部兼职 (不限) --</option>
                                </select>
                            </div>

                            <!-- Date Filter -->
                            <div>
                                <label class="block text-[11px] font-semibold text-slate-600 mb-1">📅 按打卡日期筛选：</label>
                                <input type="date" id="filterDateInput" onchange="applySubmissionsFilter()" class="w-full px-2.5 py-1.5 bg-slate-50 border border-slate-200 rounded-lg text-xs font-semibold text-slate-800 focus:outline-none focus:ring-2 focus:ring-blue-500">
                            </div>

                            <!-- Settlement Filter -->
                            <div>
                                <label class="block text-[11px] font-semibold text-slate-600 mb-1">💰 24小时结算状态筛选：</label>
                                <select id="filterSettleSelect" onchange="applySubmissionsFilter()" class="w-full px-2.5 py-1.5 bg-slate-50 border border-slate-200 rounded-lg text-xs font-semibold text-slate-800 focus:outline-none focus:ring-2 focus:ring-blue-500">
                                    <option value="">-- 全部状态 --</option>
                                    <option value="ready_24h">💰 满24小时 · 达标待结钱</option>
                                    <option value="under_24h">⏳ 24小时观察中 · 未到期</option>
                                    <option value="unsettled">🟡 全部待结算</option>
                                    <option value="settled">🟢 全部已结算</option>
                                </select>
                            </div>
                        </div>

                        <!-- Quick Filter Tags & Actions -->
                        <div class="flex items-center justify-between flex-wrap gap-2 pt-2 border-t border-slate-100 text-[11px]">
                            <div class="flex items-center gap-1.5 flex-wrap">
                                <span class="text-slate-400 font-medium">快捷筛选:</span>
                                <button onclick="setFilterSettlePreset('ready_24h')" class="px-2 py-0.5 rounded bg-purple-100 hover:bg-purple-200 text-purple-800 font-bold transition shadow-xs">💰 满24H待结</button>
                                <button onclick="setFilterSettlePreset('under_24h')" class="px-2 py-0.5 rounded bg-amber-100 hover:bg-amber-200 text-amber-800 font-bold transition shadow-xs">⏳ 24H观察中</button>
                                <span class="text-slate-300">|</span>
                                <button onclick="setFilterDatePreset('all')" class="px-2 py-0.5 rounded bg-slate-100 hover:bg-slate-200 text-slate-700 font-bold transition">全部日期</button>
                                <button onclick="setFilterDatePreset('today')" class="px-2 py-0.5 rounded bg-blue-50 hover:bg-blue-100 text-blue-700 font-bold transition">今天</button>
                                <button onclick="setFilterDatePreset('yesterday')" class="px-2 py-0.5 rounded bg-slate-100 hover:bg-slate-200 text-slate-700 font-bold transition">昨天</button>
                                <button onclick="setFilterDatePreset('7days')" class="px-2 py-0.5 rounded bg-slate-100 hover:bg-slate-200 text-slate-700 font-bold transition">近7天</button>
                            </div>
                            <div class="flex items-center gap-2">
                                <span id="filterResultCountBadge" class="text-blue-700 font-bold bg-blue-50 px-2 py-0.5 rounded border border-blue-100">共 0 条打卡</span>
                                <button onclick="copyFilteredLinks()" title="一键复制当前查出的所有小红书回传链接" class="px-2.5 py-1 bg-emerald-50 hover:bg-emerald-100 text-emerald-700 border border-emerald-200 rounded-lg font-bold transition flex items-center space-x-1 shadow-sm">
                                    <span>📋 批量复制回传链接</span>
                                </button>
                                <button onclick="resetSubmissionsFilter()" class="px-2 py-1 text-slate-500 hover:text-slate-700 font-bold transition">
                                    🔄 重置筛选
                                </button>
                            </div>
                        </div>
                    </div>

                    <!-- Submissions Table -->
                    <div class="border border-slate-200 rounded-xl overflow-hidden max-h-72 overflow-y-auto bg-white shadow-sm">
                        <table class="w-full text-xs text-left border-collapse">
                            <thead class="bg-slate-100 text-slate-700 font-bold border-b border-slate-200 sticky top-0 z-10 shadow-sm">
                                <tr>
                                    <th class="p-2.5">发布时间 / 24H结算进度</th>
                                    <th class="p-2.5">兼职人员</th>
                                    <th class="p-2.5">作品组名</th>
                                    <th class="p-2.5">回传小红书链接</th>
                                    <th class="p-2.5">Tag / 存活状态</th>
                                    <th class="p-2.5 text-center">24H结算操作</th>
                                </tr>
                            </thead>
                            <tbody id="adminSubmissionsBody" class="divide-y divide-slate-100">
                                <tr><td colspan="6" class="p-4 text-center text-slate-400">暂无打卡记录</td></tr>
                            </tbody>
                        </table>
                    </div>
                </div>

            </div>
        </div>
    </div>

    <!-- Image Preview Modal -->
    <div id="imgModal" class="fixed inset-0 z-50 bg-black/90 flex items-center justify-center p-2 hidden" onclick="closeImgModal()">
        <img id="modalImg" class="max-w-full max-h-full rounded-lg shadow-2xl object-contain">
    </div>

    <!-- Toast Notification -->
    <div id="toast" class="fixed bottom-6 left-1/2 -translate-x-1/2 bg-slate-900/90 backdrop-blur text-white text-xs font-semibold px-4 py-2.5 rounded-full shadow-lg z-50 transition-all duration-300 opacity-0 pointer-events-none">
        通知消息
    </div>

    <script>
        let currentMaterialData = null;
        let adminAuthToken = localStorage.getItem('xhs_admin_pwd') || '';
        let currentWhitelist = JSON.parse(localStorage.getItem('saved_admin_whitelist') || '[]');
        let allAdminSubmissions = [];
        let currentlyFilteredSubmissions = [];

        window.addEventListener('DOMContentLoaded', () => {
            const savedName = localStorage.getItem('xhs_distributor_name') || '';
            const nameEl = document.getElementById('userNameInput');
            if (nameEl && savedName) nameEl.value = savedName;
            if (savedName) checkUserStatus();
        });

        function saveCredentials() {
            const nameEl = document.getElementById('userNameInput');
            if (nameEl) {
                const name = nameEl.value.trim();
                if (name) localStorage.setItem('xhs_distributor_name', name);
            }
        }

        function showToast(msg) {
            const toast = document.getElementById('toast');
            toast.innerText = msg;
            toast.classList.remove('opacity-0', 'pointer-events-none');
            setTimeout(() => {
                toast.classList.add('opacity-0', 'pointer-events-none');
            }, 3500);
        }

        async function checkUserStatus() {
            const nameEl = document.getElementById('userNameInput');
            const name = nameEl ? nameEl.value.trim() : '';
            if (!name) {
                showToast('请先输入你的姓名或微信昵称');
                return;
            }
            saveCredentials();

            try {
                const res = await fetch(`/api/user/status?name=${encodeURIComponent(name)}`);
                const data = await res.json();
                if (data.success) {
                    renderUserState(data.user);
                } else {
                    showToast(data.error);
                }
            } catch (err) {
                showToast('网络连接失败');
            }
        }

        let cooldownTimerInterval = null;

        function startCooldownTimer(seconds) {
            if (cooldownTimerInterval) clearInterval(cooldownTimerInterval);
            let remaining = seconds;

            function updateDisplay() {
                if (remaining <= 0) {
                    clearInterval(cooldownTimerInterval);
                    cooldownTimerInterval = null;
                    showToast('🎉 60 分钟冷却时间已结束，现在可以领取下一组素材啦！');
                    checkUserStatus();
                    return;
                }
                const m = Math.floor(remaining / 60);
                const s = remaining % 60;
                const mStr = String(m).padStart(2, '0');
                const sStr = String(s).padStart(2, '0');
                const timerEl = document.getElementById('cooldownTimerDisplay');
                if (timerEl) timerEl.innerText = `${mStr}:${sStr}`;
                remaining--;
            }

            updateDisplay();
            cooldownTimerInterval = setInterval(updateDisplay, 1000);
        }

        function renderUserState(user) {
            const badge = document.getElementById('completedBadge');
            const todayCountSpan = document.getElementById('todayCountSpan');
            const dailyLimitSpan = document.getElementById('dailyLimitSpan');
            const firstClaimBox = document.getElementById('firstClaimBox');
            const submitLinkBox = document.getElementById('submitLinkBox');
            const cooldownBox = document.getElementById('cooldownBox');
            const activeGroupName = document.getElementById('activeGroupName');
            const matCard = document.getElementById('materialContentCard');
            const historyCard = document.getElementById('historyCard');
            const historyList = document.getElementById('historyList');

            badge.classList.remove('hidden');
            todayCountSpan.innerText = user.today_count || 0;
            dailyLimitSpan.innerText = user.daily_limit || 3;

            if (user.current_material) {
                if (cooldownTimerInterval) { clearInterval(cooldownTimerInterval); cooldownTimerInterval = null; }
                if (cooldownBox) cooldownBox.classList.add('hidden');
                firstClaimBox.classList.add('hidden');
                submitLinkBox.classList.remove('hidden');
                activeGroupName.innerText = user.current_material.group_name;
                renderMaterialCard(user.current_material);
            } else if (user.in_cooldown && user.cooldown_remaining_seconds > 0) {
                firstClaimBox.classList.add('hidden');
                submitLinkBox.classList.add('hidden');
                matCard.classList.add('hidden');
                if (cooldownBox) cooldownBox.classList.remove('hidden');
                startCooldownTimer(user.cooldown_remaining_seconds);
            } else {
                if (cooldownTimerInterval) { clearInterval(cooldownTimerInterval); cooldownTimerInterval = null; }
                if (cooldownBox) cooldownBox.classList.add('hidden');
                firstClaimBox.classList.remove('hidden');
                submitLinkBox.classList.add('hidden');
                matCard.classList.add('hidden');
            }

            if (user.history && user.history.length > 0) {
                historyCard.classList.remove('hidden');
                const nowTime = new Date();
                historyList.innerHTML = user.history.map(item => {
                    let diffHours = 0;
                    let isPast24H = false;
                    let remainStr = '';
                    if (item.submitted_at) {
                        const subDate = new Date(item.submitted_at.replace(/-/g, '/'));
                        diffHours = (nowTime - subDate) / (1000 * 60 * 60);
                        isPast24H = diffHours >= 24;
                        const remainingHours = Math.max(0, 24 - diffHours);
                        remainStr = remainingHours < 1 ? Math.round(remainingHours * 60) + '分钟' : remainingHours.toFixed(1) + '小时';
                    }

                    return `
                    <div class="p-3 bg-slate-50 rounded-xl border border-slate-100 flex items-center justify-between">
                        <div>
                            <div class="font-bold text-slate-800">${item.material_name}</div>
                            <div class="text-[11px] text-slate-500 mt-0.5 flex items-center space-x-1.5 flex-wrap">
                                <span>🏷️ ${item.tag_expected || '-'}</span>
                                <span class="text-slate-300">|</span>
                                <span class="truncate max-w-[140px]">${item.xhs_title || '已打卡'}</span>
                            </div>
                            <a href="${item.xhs_link}" target="_blank" class="text-blue-600 hover:underline truncate max-w-[220px] block mt-0.5 text-xs">
                                🔗 ${item.xhs_link}
                            </a>
                            <div class="mt-1 text-[10px] text-slate-500 flex items-center space-x-1 flex-wrap">
                                <span>📅 发布: ${item.submitted_at ? item.submitted_at.substring(5, 16) : '-'}</span>
                                <span class="text-slate-300">·</span>
                                ${item.settlement_status === 'settled' 
                                    ? '<span class="text-emerald-700 font-bold">提成已发放结清</span>' 
                                    : (isPast24H 
                                        ? '<span class="text-purple-700 font-bold">🎉 满24小时·等待管理员发薪</span>' 
                                        : `<span class="text-amber-700 font-semibold">⏳ 距24H结算还剩 ${remainStr}</span>`)}
                            </div>
                        </div>
                        <div class="text-right text-[11px] text-slate-400 space-y-1 shrink-0 ml-2">
                            <div class="flex items-center justify-end space-x-1">
                                ${item.survival_status === 'in_review' 
                                    ? '<span class="px-2 py-0.5 bg-amber-50 text-amber-700 border border-amber-200 rounded font-bold">⏳ 审核中</span>' 
                                    : (item.survival_status === 'active' 
                                        ? '<span class="px-2 py-0.5 bg-emerald-50 text-emerald-700 border border-emerald-200 rounded font-bold">✅ 存活</span>'
                                        : (item.survival_status === 'dead'
                                            ? '<span class="px-2 py-0.5 bg-rose-50 text-rose-700 border border-rose-200 rounded font-bold">❌ 异常</span>'
                                            : '<span class="px-2 py-0.5 bg-slate-50 text-slate-600 border border-slate-200 rounded font-bold">待复核</span>'))}
                                ${item.settlement_status === 'settled' 
                                    ? '<span class="px-2 py-0.5 bg-emerald-100 text-emerald-700 rounded font-bold">🟢 已结</span>' 
                                    : '<span class="px-2 py-0.5 bg-amber-100 text-amber-700 rounded font-bold">⏳ 待结</span>'}
                            </div>
                        </div>
                    </div>
                `}).join('');
            } else {
                historyCard.classList.add('hidden');
            }
        }

        function renderMaterialCard(mat) {
            currentMaterialData = mat;
            const matCard = document.getElementById('materialContentCard');
            const matBadge = document.getElementById('matBadge');
            const matTitle = document.getElementById('matTitle');
            const matTime = document.getElementById('matAssignedTime');
            const imagesGrid = document.getElementById('imagesGrid');
            const copyPre = document.getElementById('copyTextPre');
            const zipBtn = document.getElementById('downloadZipBtn');

            matBadge.innerText = mat.group_name.split('_')[0] || '独家素材';
            matTitle.innerText = mat.title || mat.group_name;
            matTime.innerText = mat.assigned_at ? mat.assigned_at.split(' ')[1] + ' 分配' : '';
            copyPre.innerText = mat.copy_text;
            zipBtn.href = `/api/download_zip?material_id=${mat.id}`;

            imagesGrid.innerHTML = mat.images.map((imgPath, idx) => {
                const labels = ['图1 · 封面', '图2 · 内容', '图3 · 尾图'];
                const label = labels[idx] || `图${idx+1}`;
                const srcUrl = imgPath.startsWith('data:') ? imgPath : `/api/image?path=${encodeURIComponent(imgPath)}`;
                return `
                    <div class="relative group rounded-xl overflow-hidden border border-slate-200 bg-slate-100 aspect-[3/4] flex flex-col shadow-sm">
                        <img src="${srcUrl}" 
                             onclick="previewImg('${srcUrl}')" 
                             class="w-full h-full object-cover cursor-pointer hover:scale-105 transition duration-200" 
                             alt="${label}">
                        <div class="absolute bottom-0 inset-x-0 bg-black/60 backdrop-blur-sm text-white text-[10px] font-bold px-1.5 py-1 text-center">
                            ${label}
                        </div>
                    </div>
                `;
            }).join('');

            matCard.classList.remove('hidden');
        }

        async function claimMaterial(isNext) {
            const nameEl = document.getElementById('userNameInput');
            const name = nameEl ? nameEl.value.trim() : '';
            if (!name) {
                showToast('请先输入你的姓名或微信昵称！');
                return;
            }
            saveCredentials();

            let xhsLink = '';
            if (isNext) {
                xhsLink = document.getElementById('xhsLinkInput').value.trim();
                if (!xhsLink) {
                    showToast('请粘贴刚刚发布的小红书分享内容后再提交！');
                    return;
                }
            }

            const btn = document.getElementById('claimBtn');
            if (btn && isNext) {
                btn.innerHTML = '<span>🔍 正在智能提取链接并核验...</span>';
                btn.disabled = true;
            }

            try {
                const res = await fetch('/api/claim', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        user_name: name,
                        xhs_link: xhsLink
                    })
                });
                const data = await res.json();
                if (data.success) {
                    showToast(data.message);
                    const linkInput = document.getElementById('xhsLinkInput');
                    if (linkInput) linkInput.value = '';
                    if (data.user) {
                        renderUserState(data.user);
                    } else {
                        checkUserStatus();
                    }
                } else {
                    showToast(data.error);
                    if (data.in_cooldown) {
                        checkUserStatus();
                    }
                }
            } catch (err) {
                showToast('网络请求异常');
            } finally {
                if (btn && isNext) {
                    btn.innerHTML = '<span>🚀 提交小红书打卡链接</span>';
                    btn.disabled = false;
                }
            }
        }

        function copyCopywriting() {
            if (!currentMaterialData || !currentMaterialData.copy_text) return;
            navigator.clipboard.writeText(currentMaterialData.copy_text).then(() => {
                const btn = document.getElementById('copyBtn');
                btn.innerHTML = '<span>✅ 文案已复制！直接去粘贴</span>';
                btn.classList.replace('bg-red-50', 'bg-emerald-50');
                btn.classList.replace('text-red-600', 'text-emerald-700');
                btn.classList.replace('border-red-200', 'border-emerald-200');
                showToast('文案已复制到剪贴板！');
                setTimeout(() => {
                    btn.innerHTML = '<span>📋 一键复制文案</span>';
                    btn.classList.replace('bg-emerald-50', 'bg-red-50');
                    btn.classList.replace('text-emerald-700', 'text-red-600');
                    btn.classList.replace('border-emerald-200', 'border-red-200');
                }, 3000);
            });
        }

        function previewImg(url) {
            document.getElementById('modalImg').src = url;
            document.getElementById('imgModal').classList.remove('hidden');
        }

        function closeImgModal() {
            document.getElementById('imgModal').classList.add('hidden');
        }

        function openAdmin() {
            if (adminAuthToken) {
                toggleAdminModal();
            } else {
                document.getElementById('adminLoginModal').classList.remove('hidden');
                setTimeout(() => {
                    const el = document.getElementById('adminPwdInput');
                    if (el) { el.value = ''; el.focus(); }
                }, 100);
            }
        }

        function closeAdminLogin() {
            document.getElementById('adminLoginModal').classList.add('hidden');
        }

        function logoutAdmin() {
            adminAuthToken = '';
            localStorage.removeItem('xhs_admin_pwd');
            document.getElementById('adminModal').classList.add('hidden');
            showToast('🚪 已安全退出 3金管理后台');
        }

        function exportAdminCsv() {
            if (!adminAuthToken) {
                showToast('请先验证管理密码');
                openAdmin();
                return;
            }
            window.open('/api/admin/export_csv?token=' + encodeURIComponent(adminAuthToken), '_blank');
        }

        async function verifyAdminLogin() {
            const pwd = document.getElementById('adminPwdInput').value.trim();
            if (!pwd) {
                showToast('请输入管理密码');
                return;
            }
            try {
                const res = await fetch('/api/admin/login', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ password: pwd })
                });
                const data = await res.json();
                if (data.success) {
                    adminAuthToken = pwd;
                    localStorage.setItem('xhs_admin_pwd', pwd);
                    closeAdminLogin();
                    toggleAdminModal();
                    showToast('🎉 身份验证通过，欢迎进入 3金管理后台！');
                } else {
                    showToast(data.error || '管理密码错误，拒绝进入！');
                }
            } catch (err) {
                showToast('登录验证异常');
            }
        }

        function toggleAdminModal() {
            const modal = document.getElementById('adminModal');
            if (modal.classList.contains('hidden')) {
                if (!adminAuthToken) {
                    openAdmin();
                    return;
                }
                modal.classList.remove('hidden');
                loadAdminData();
                loadAdminSettings();
            } else {
                modal.classList.add('hidden');
            }
        }

        function switchUploadTab(tab) {
            const pipeP = document.getElementById('pipelineUploadPanel');
            const batchP = document.getElementById('batchUploadPanel');
            const singleP = document.getElementById('singleUploadPanel');
            const pipeB = document.getElementById('tabBtnPipeline');
            const batchB = document.getElementById('tabBtnBatch');
            const singleB = document.getElementById('tabBtnSingle');

            const allP = [pipeP, batchP, singleP];
            const allB = [pipeB, batchB, singleB];

            allP.forEach(p => { if (p) p.classList.add('hidden'); });
            allB.forEach(b => {
                if (b) {
                    b.classList.remove('bg-emerald-600', 'text-white');
                    b.classList.add('bg-white', 'text-slate-700');
                }
            });

            if (tab === 'pipeline') {
                if (pipeP) pipeP.classList.remove('hidden');
                if (pipeB) {
                    pipeB.classList.remove('bg-white', 'text-slate-700');
                    pipeB.classList.add('bg-emerald-600', 'text-white');
                }
                refreshPipelineStatus();
            } else if (tab === 'batch') {
                if (batchP) batchP.classList.remove('hidden');
                if (batchB) {
                    batchB.classList.remove('bg-white', 'text-slate-700');
                    batchB.classList.add('bg-emerald-600', 'text-white');
                }
            } else {
                if (singleP) singleP.classList.remove('hidden');
                if (singleB) {
                    singleB.classList.remove('bg-white', 'text-slate-700');
                    singleB.classList.add('bg-emerald-600', 'text-white');
                }
            }
        }

        async function refreshPipelineStatus() {
            try {
                const res = await fetch('/api/admin/pipeline/status', {
                    headers: { 'X-Admin-Password': adminAuthToken }
                });
                const data = await res.json();
                if (data.success && data.counts) {
                    const c = data.counts;
                    const elCovers = document.getElementById('bufCountCovers');
                    const elContents = document.getElementById('bufCountContents');
                    const elEnds = document.getElementById('bufCountEnds');
                    const elCopies = document.getElementById('bufCountCopies');
                    
                    if (elCovers) elCovers.innerText = c.covers + ' 张';
                    if (elContents) elContents.innerText = c.contents + ' 张';
                    if (elEnds) elEnds.innerText = c.ends + ' 张';
                    if (elCopies) elCopies.innerText = c.copies + ' 篇';

                    const updateBadge = (id, count) => {
                        const el = document.getElementById(id);
                        if (!el) return;
                        if (count >= 1) {
                            el.className = 'text-[9px] font-bold text-emerald-600';
                            el.innerText = '🟢 已就绪 (' + count + ')';
                        } else {
                            el.className = 'text-[9px] font-bold text-red-500';
                            el.innerText = '🔴 缺货待补';
                        }
                    };
                    updateBadge('bufBadgeCovers', c.covers);
                    updateBadge('bufBadgeContents', c.contents);
                    updateBadge('bufBadgeEnds', c.ends);
                    updateBadge('bufBadgeCopies', c.copies);

                    const alertEl = document.getElementById('pipelineAlertText');
                    if (alertEl) {
                        if (c.covers >= 1 && c.contents >= 1 && c.ends >= 1 && c.copies >= 1) {
                            alertEl.innerHTML = '<span class="text-emerald-700 font-bold">🎉 4 个模块均满足条件，系统已自动组装入库！当前素材库已全部就绪！</span>';
                        } else {
                            const missing = [];
                            if (c.covers < 1) missing.push('【图1·封面图】');
                            if (c.contents < 1) missing.push('【图2·内容图】');
                            if (c.ends < 1) missing.push('【图3·尾图】');
                            if (c.copies < 1) missing.push('【文案】');
                            alertEl.innerHTML = '<span class="text-amber-700 font-medium">💡 正在等待补充 ' + missing.join('、') + '，只要各模块数量 ≥ 1，系统将瞬间自动拼装生成作品！</span>';
                        }
                    }
                }
            } catch (err) {}
        }

        async function uploadPipelineSlot(slotName, inputId) {
            const input = document.getElementById(inputId);
            if (!input || !input.files || input.files.length === 0) return;

            const files = input.files;
            showToast('正在将 ' + files.length + ' 个文件送入缓冲池...');

            const base64List = [];
            for (let i = 0; i < files.length; i++) {
                base64List.push(await fileToBase64(files[i]));
            }

            try {
                const res = await fetch('/api/admin/pipeline/push', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-Admin-Password': adminAuthToken
                    },
                    body: JSON.stringify({
                        slot: slotName,
                        items: base64List
                    })
                });
                const data = await res.json();
                if (data.success) {
                    showToast(data.message);
                    input.value = '';
                    refreshPipelineStatus();
                    loadAdminData();
                } else {
                    showToast(data.error || '入池失败');
                }
            } catch (err) {
                showToast('上传入池失败');
            }
        }

        async function uploadPipelineCopies() {
            const input = document.getElementById('pipeCopyInput');
            const text = input ? input.value.trim() : '';
            if (!text) {
                showToast('请先输入或粘贴文案内容！');
                return;
            }

            const copies = text.split('===').map(c => c.trim()).filter(c => c.length > 0);
            if (copies.length === 0) {
                showToast('未识别到有效文案，请用 === 分隔多篇！');
                return;
            }

            const btn = document.getElementById('pipeCopyBtn');
            if (btn) {
                btn.innerHTML = '<span>⏳ 正在入池...</span>';
                btn.disabled = true;
            }

            try {
                const res = await fetch('/api/admin/pipeline/push', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-Admin-Password': adminAuthToken
                    },
                    body: JSON.stringify({
                        slot: 'copies',
                        items: copies
                    })
                });
                const data = await res.json();
                if (data.success) {
                    showToast(data.message);
                    input.value = '';
                    refreshPipelineStatus();
                    loadAdminData();
                } else {
                    showToast(data.error || '文案入池失败');
                }
            } catch (err) {
                showToast('文案入池失败');
            } finally {
                if (btn) {
                    btn.innerHTML = '<span>📥 确认将文案加入文案箱并检测装配</span>';
                    btn.disabled = false;
                }
            }
        }

        async function clearPipelineBuffer(slotName) {
            const nameMap = {
                'covers': '【图1·封面图箱】',
                'contents': '【图2·内容图箱】',
                'ends': '【图3·尾图箱】',
                'copies': '【文案箱】',
                'all': '【所有未装配的缓冲箱】'
            };
            const label = nameMap[slotName] || slotName;
            if (!confirm('确定要清空 ' + label + ' 中的未装配零件吗？')) return;

            try {
                const res = await fetch('/api/admin/pipeline/clear', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-Admin-Password': adminAuthToken
                    },
                    body: JSON.stringify({ slot: slotName })
                });
                const data = await res.json();
                showToast(data.message);
                refreshPipelineStatus();
            } catch (err) {
                showToast('清空失败');
            }
        }

        function updateBatchCount(slot) {
            const input = document.getElementById(`batchSlot${slot}`);
            const span = document.getElementById(`batchCount${slot}`);
            const count = input.files ? input.files.length : 0;
            span.innerText = `已选 ${count} 张`;
        }

        function renderWhitelistTags() {
            const container = document.getElementById('whitelistTagsContainer');
            const countBadge = document.getElementById('whitelistCountBadge');
            countBadge.innerText = `当前 ${currentWhitelist.length} 人`;
            if (currentWhitelist.length === 0) {
                container.innerHTML = '<span class="text-slate-400 text-xs py-1">暂无白名单人员，请在下方添加</span>';
                return;
            }
            container.innerHTML = currentWhitelist.map(name => `
                <span class="inline-flex items-center px-2.5 py-1 rounded-full text-xs font-bold bg-amber-100 text-amber-900 border border-amber-300 shadow-sm">
                    <span>${name}</span>
                    <button onclick="removeWhitelistItem('${name}')" title="删除" class="ml-1.5 text-amber-500 hover:text-red-600 font-bold focus:outline-none text-sm leading-none">&times;</button>
                </span>
            `).join('');
        }

        async function addWhitelistItem(customName) {
            const input = document.getElementById('newWhitelistItemInput');
            const name = (customName || input.value).trim();
            if (!name) {
                showToast('请输入兼职昵称！');
                return;
            }
            if (currentWhitelist.includes(name)) {
                showToast(`【${name}】已在白名单中！`);
                return;
            }
            try {
                const res = await fetch('/api/admin/whitelist/add', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-Admin-Password': adminAuthToken
                    },
                    body: JSON.stringify({ name: name })
                });
                const data = await res.json();
                if (data.success) {
                    currentWhitelist = data.whitelist;
                    localStorage.setItem('saved_admin_whitelist', JSON.stringify(currentWhitelist));
                    if (!customName) input.value = '';
                    renderWhitelistTags();
                    showToast(data.message);
                } else {
                    showToast(data.error);
                }
            } catch (err) {
                showToast('添加白名单失败');
            }
        }

        async function removeWhitelistItem(name) {
            try {
                const res = await fetch('/api/admin/whitelist/remove', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-Admin-Password': adminAuthToken
                    },
                    body: JSON.stringify({ name: name })
                });
                const data = await res.json();
                if (data.success) {
                    currentWhitelist = data.whitelist;
                    localStorage.setItem('saved_admin_whitelist', JSON.stringify(currentWhitelist));
                    renderWhitelistTags();
                    showToast(data.message);
                } else {
                    showToast(data.error);
                }
            } catch (err) {
                showToast('删除失败');
            }
        }

        let currentSecurityQuestion = '3金的专属安全暗号是什么？';

        async function openAdminResetModal() {
            try {
                const res = await fetch('/api/admin/security_info');
                const data = await res.json();
                if (data.success && data.question) {
                    currentSecurityQuestion = data.question;
                }
            } catch (err) {}
            const qEl = document.getElementById('resetModalQuestionText');
            if (qEl) qEl.innerText = currentSecurityQuestion;
            document.getElementById('resetSecurityAnswer').value = '';
            document.getElementById('resetNewPassword').value = '';
            document.getElementById('resetNewPasswordConfirm').value = '';
            document.getElementById('adminLoginModal').classList.add('hidden');
            document.getElementById('adminResetModal').classList.remove('hidden');
        }

        function closeAdminResetModal() {
            document.getElementById('adminResetModal').classList.add('hidden');
            document.getElementById('adminLoginModal').classList.remove('hidden');
        }

        async function submitResetAdminPassword() {
            const answer = document.getElementById('resetSecurityAnswer').value.trim();
            const newPwd = document.getElementById('resetNewPassword').value.trim();
            const newPwdConfirm = document.getElementById('resetNewPasswordConfirm').value.trim();

            if (!answer) {
                showToast('请输入密保答案！');
                return;
            }
            if (!newPwd) {
                showToast('请输入新管理密码！');
                return;
            }
            if (newPwd !== newPwdConfirm) {
                showToast('两次输入的新密码不一致！');
                return;
            }

            try {
                const res = await fetch('/api/admin/reset_password', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        security_answer: answer,
                        new_password: newPwd
                    })
                });
                const data = await res.json();
                if (data.success) {
                    adminAuthToken = data.new_password || newPwd;
                    localStorage.setItem('xhs_admin_pwd', adminAuthToken);
                    document.getElementById('adminResetModal').classList.add('hidden');
                    document.getElementById('adminLoginModal').classList.add('hidden');
                    document.getElementById('adminModal').classList.remove('hidden');
                    loadAdminData();
                    loadAdminSettings();
                    showToast(data.message);
                } else {
                    showToast(data.error);
                }
            } catch (err) {
                showToast('重置密码失败');
            }
        }

        async function openAdminSecurityModal() {
            try {
                const res = await fetch('/api/admin/security_info');
                const data = await res.json();
                if (data.success && data.question) {
                    currentSecurityQuestion = data.question;
                }
            } catch (err) {}
            const qEl = document.getElementById('secCurrentQuestionDisplay');
            if (qEl) qEl.innerText = currentSecurityQuestion;
            document.getElementById('secOldPassword').value = '';
            document.getElementById('secSecurityAnswer').value = '';
            document.getElementById('secNewPassword').value = '';
            document.getElementById('secNewPasswordConfirm').value = '';
            document.getElementById('secNewCustomQuestion').value = '';
            document.getElementById('secNewCustomAnswer').value = '';
            document.getElementById('customQuestionBox').classList.add('hidden');
            document.getElementById('adminSecurityModal').classList.remove('hidden');
        }

        function closeAdminSecurityModal() {
            document.getElementById('adminSecurityModal').classList.add('hidden');
        }

        function toggleCustomQuestionBox() {
            const box = document.getElementById('customQuestionBox');
            if (box) box.classList.toggle('hidden');
        }

        async function submitChangeAdminPassword() {
            const oldPwd = document.getElementById('secOldPassword').value.trim();
            const answer = document.getElementById('secSecurityAnswer').value.trim();
            const newPwd = document.getElementById('secNewPassword').value.trim();
            const newPwdConfirm = document.getElementById('secNewPasswordConfirm').value.trim();
            const newQuestion = document.getElementById('secNewCustomQuestion').value.trim();
            const newAnswer = document.getElementById('secNewCustomAnswer').value.trim();

            if (!oldPwd) {
                showToast('请输入当前管理员原密码！');
                return;
            }
            if (!answer) {
                showToast('请输入密保答案进行安全核验！');
                return;
            }
            if (!newPwd) {
                showToast('请输入新管理密码！');
                return;
            }
            if (newPwd !== newPwdConfirm) {
                showToast('两次输入的新密码不一致！');
                return;
            }

            try {
                const res = await fetch('/api/admin/change_password', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-Admin-Password': adminAuthToken
                    },
                    body: JSON.stringify({
                        old_password: oldPwd,
                        security_answer: answer,
                        new_password: newPwd,
                        new_security_question: newQuestion,
                        new_security_answer: newAnswer
                    })
                });
                const data = await res.json();
                if (data.success) {
                    adminAuthToken = data.new_password || newPwd;
                    localStorage.setItem('xhs_admin_pwd', adminAuthToken);
                    closeAdminSecurityModal();
                    loadAdminSettings();
                    showToast(data.message);
                } else {
                    showToast(data.error);
                }
            } catch (err) {
                showToast('更新密码失败');
            }
        }

        async function loadAdminSettings() {
            try {
                const res = await fetch('/api/admin/settings', {
                    headers: { 'X-Admin-Password': adminAuthToken }
                });
                const data = await res.json();
                if (data.success) {
                    const authModeEl = document.getElementById('settingAuthMode');
                    if (authModeEl) authModeEl.value = data.auth_mode || 'whitelist';
                    const dailyLimitEl = document.getElementById('settingDailyLimit');
                    if (dailyLimitEl) dailyLimitEl.value = data.daily_limit || 3;
                    const timeoutEl = document.getElementById('settingTimeoutHours');
                    if (timeoutEl) timeoutEl.value = data.claim_timeout_hours || 2;
                    if (data.admin_security_question) {
                        currentSecurityQuestion = data.admin_security_question;
                    }
                    
                    const localCached = JSON.parse(localStorage.getItem('saved_admin_whitelist') || '[]');
                    const serverList = Array.isArray(data.whitelist) ? data.whitelist : [];
                    
                    if (serverList.length === 0 && localCached.length > 0) {
                        currentWhitelist = localCached;
                        saveAdminSettingsSilently();
                    } else {
                        currentWhitelist = serverList;
                        localStorage.setItem('saved_admin_whitelist', JSON.stringify(currentWhitelist));
                    }
                    renderWhitelistTags();
                }
            } catch (err) {}
        }

        async function saveAdminSettingsSilently() {
            const authModeEl = document.getElementById('settingAuthMode');
            const auth_mode = authModeEl ? authModeEl.value : 'whitelist';
            const dailyLimitEl = document.getElementById('settingDailyLimit');
            const daily_limit = dailyLimitEl ? dailyLimitEl.value.trim() : '3';
            const timeoutEl = document.getElementById('settingTimeoutHours');
            const timeout_hours = timeoutEl ? timeoutEl.value.trim() : '2';

            try {
                await fetch('/api/admin/settings', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-Admin-Password': adminAuthToken
                    },
                    body: JSON.stringify({
                        auth_mode: auth_mode,
                        daily_limit: daily_limit,
                        claim_timeout_hours: timeout_hours,
                        whitelist: currentWhitelist
                    })
                });
            } catch (err) {}
        }

        async function saveAdminSettings() {
            const authModeEl = document.getElementById('settingAuthMode');
            const auth_mode = authModeEl ? authModeEl.value : 'whitelist';
            const dailyLimitEl = document.getElementById('settingDailyLimit');
            const daily_limit = dailyLimitEl ? dailyLimitEl.value.trim() : '3';
            const timeoutEl = document.getElementById('settingTimeoutHours');
            const timeout_hours = timeoutEl ? timeoutEl.value.trim() : '2';

            try {
                const res = await fetch('/api/admin/settings', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-Admin-Password': adminAuthToken
                    },
                    body: JSON.stringify({
                        auth_mode: auth_mode,
                        daily_limit: daily_limit,
                        claim_timeout_hours: timeout_hours,
                        whitelist: currentWhitelist
                    })
                });
                const data = await res.json();
                if (data.success) {
                    showToast(data.message);
                } else {
                    showToast(data.error);
                }
            } catch (err) {
                showToast('保存设置失败');
            }
        }

        async function releaseExpiredAssignments() {
            try {
                const res = await fetch('/api/admin/materials/release_expired', {
                    method: 'POST',
                    headers: { 'X-Admin-Password': adminAuthToken }
                });
                const data = await res.json();
                showToast(data.message);
                loadAdminData();
            } catch (err) {
                showToast('释放失败');
            }
        }

        async function toggleSettlement(subId, currentStatus) {
            const targetStatus = currentStatus === 'settled' ? 'unsettled' : 'settled';
            try {
                const res = await fetch('/api/admin/submissions/toggle_settlement', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-Admin-Password': adminAuthToken
                    },
                    body: JSON.stringify({ id: subId, status: targetStatus })
                });
                const data = await res.json();
                if (data.success) {
                    showToast(data.message);
                    loadAdminData();
                } else {
                    showToast(data.error);
                }
            } catch (err) {
                showToast('切换结算状态失败');
            }
        }

        async function inspectSurvivalStatus() {
            const btn = document.getElementById('inspectBtn');
            btn.innerHTML = '<span>⏳ 正在巡检小红书笔记存活...</span>';
            btn.disabled = true;

            try {
                const res = await fetch('/api/admin/submissions/inspect_survival', {
                    method: 'POST',
                    headers: { 'X-Admin-Password': adminAuthToken }
                });
                const data = await res.json();
                if (data.success) {
                    showToast(data.message);
                    loadAdminData();
                } else {
                    showToast(data.error);
                }
            } catch (err) {
                showToast('巡检异常');
            } finally {
                btn.innerHTML = '<span>🔍 24h存活巡检</span>';
                btn.disabled = false;
            }
        }

        async function previewSlot(slotNum) {
            const input = document.getElementById(`slot${slotNum}File`);
            const preview = document.getElementById(`slot${slotNum}Preview`);
            if (input.files && input.files[0]) {
                const file = input.files[0];
                const reader = new FileReader();
                reader.onload = function(e) {
                    preview.innerHTML = `<img src="${e.target.result}" class="w-full h-full object-cover">`;
                };
                reader.readAsDataURL(file);
            } else {
                preview.innerHTML = `待选图${slotNum}`;
            }
        }

        async function fileToBase64(file) {
            if (!file) return '';
            return new Promise((resolve) => {
                const reader = new FileReader();
                reader.onload = (e) => resolve(e.target.result);
                reader.readAsDataURL(file);
            });
        }

        async function submit3SlotsMaterial() {
            const group_name = document.getElementById('newGroupInput').value.trim();
            const copy_text = document.getElementById('newCopyInput').value.trim();
            const f1 = document.getElementById('slot1File').files[0];
            const f2 = document.getElementById('slot2File').files[0];
            const f3 = document.getElementById('slot3File').files[0];

            if (!group_name) {
                showToast('请输入作品组名/标题！');
                return;
            }
            if (!copy_text) {
                showToast('请填写发布文案！');
                return;
            }
            if (!f1) {
                showToast('请在【图1 · 封面图】上传封面配图！');
                return;
            }

            const btn = document.getElementById('add3SlotsBtn');
            btn.innerHTML = '<span>⏳ 正在保存入库...</span>';
            btn.disabled = true;

            const b64_1 = await fileToBase64(f1);
            const b64_2 = await fileToBase64(f2);
            const b64_3 = await fileToBase64(f3);

            try {
                const res = await fetch('/api/admin/materials/add', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-Admin-Password': adminAuthToken
                    },
                    body: JSON.stringify({
                        group_name: group_name,
                        copy_text: copy_text,
                        img1: b64_1,
                        img2: b64_2,
                        img3: b64_3
                    })
                });
                const data = await res.json();
                if (data.success) {
                    showToast(data.message);
                    document.getElementById('newGroupInput').value = '';
                    document.getElementById('newCopyInput').value = '';
                    document.getElementById('slot1File').value = '';
                    document.getElementById('slot2File').value = '';
                    document.getElementById('slot3File').value = '';
                    document.getElementById('slot1Preview').innerHTML = '待选图1';
                    document.getElementById('slot2Preview').innerHTML = '待选图2';
                    document.getElementById('slot3Preview').innerHTML = '待选图3';
                    loadAdminData();
                } else {
                    showToast(data.error);
                }
            } catch (err) {
                showToast('上传失败');
            } finally {
                btn.innerHTML = '<span>🚀 保存单组并派入素材池</span>';
                btn.disabled = false;
            }
        }

        async function submitBatchMaterials() {
            const f1_files = document.getElementById('batchSlot1').files;
            const f2_files = document.getElementById('batchSlot2').files;
            const f3_files = document.getElementById('batchSlot3').files;
            const raw_copy = document.getElementById('batchCopyInput').value.trim();
            const prefix = document.getElementById('batchPrefix').value.trim() || '批量矩阵_';

            if (!f1_files || f1_files.length === 0) {
                showToast('请至少在【图1·封面图】选择一批实况/图片！');
                return;
            }
            if (!raw_copy) {
                showToast('请在文案池中粘贴文案内容！');
                return;
            }

            const copies = raw_copy.split('===').map(c => c.trim()).filter(c => c.length > 0);
            if (copies.length === 0) {
                showToast('未能识别到有效文案，请用 === 分隔多篇文案！');
                return;
            }

            const btn = document.getElementById('batchSubmitBtn');
            btn.innerHTML = '<span>⏳ 正在批量极速组装入库...</span>';
            btn.disabled = true;

            const b64_1 = [];
            for (let i = 0; i < f1_files.length; i++) b64_1.push(await fileToBase64(f1_files[i]));
            const b64_2 = [];
            for (let i = 0; i < f2_files.length; i++) b64_2.push(await fileToBase64(f2_files[i]));
            const b64_3 = [];
            for (let i = 0; i < f3_files.length; i++) b64_3.push(await fileToBase64(f3_files[i]));

            try {
                const res = await fetch('/api/admin/materials/batch_add', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-Admin-Password': adminAuthToken
                    },
                    body: JSON.stringify({
                        covers: b64_1,
                        contents: b64_2,
                        tails: b64_3,
                        copies: copies,
                        prefix: prefix
                    })
                });
                const data = await res.json();
                if (data.success) {
                    showToast(data.message);
                    document.getElementById('batchSlot1').value = '';
                    document.getElementById('batchSlot2').value = '';
                    document.getElementById('batchSlot3').value = '';
                    document.getElementById('batchCopyInput').value = '';
                    updateBatchCount(1);
                    updateBatchCount(2);
                    updateBatchCount(3);
                    loadAdminData();
                } else {
                    showToast(data.error);
                }
            } catch (err) {
                showToast('批量上传失败');
            } finally {
                btn.innerHTML = '<span>⚡️ 一键批量自动组装并入库</span>';
                btn.disabled = false;
            }
        }

        async function deleteMaterial(id, name) {
            if (!confirm(`确定要从素材库彻底删除【${name}】吗？`)) return;
            try {
                const res = await fetch('/api/admin/materials/delete', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-Admin-Password': adminAuthToken
                    },
                    body: JSON.stringify({ id: id })
                });
                const data = await res.json();
                showToast(data.message);
                loadAdminData();
            } catch (err) {
                showToast('删除失败');
            }
        }

        async function clearCompletedMaterials() {
            if (!confirm('确定要一键清理所有已消耗/已打卡的素材吗？')) return;
            try {
                const res = await fetch('/api/admin/materials/clear_completed', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-Admin-Password': adminAuthToken
                    }
                });
                const data = await res.json();
                showToast(data.message);
                loadAdminData();
            } catch (err) {
                showToast('清理失败');
            }
        }

        async function loadAdminData() {
            try {
                const res = await fetch('/api/admin/stats', {
                    headers: { 'X-Admin-Password': adminAuthToken }
                });
                const data = await res.json();
                if (data.success) {
                    refreshPipelineStatus();
                    document.getElementById('statTotal').innerText = data.stats.total_materials;
                    document.getElementById('statAvailable').innerText = data.stats.available;
                    document.getElementById('statUnsettled').innerText = data.stats.unsettled_submissions || 0;
                    document.getElementById('statSettled').innerText = data.stats.settled_submissions || 0;

                    if (data.workers && data.workers.length > 0) {
                        const quickBox = document.getElementById('quickAddWorkersBox');
                        const quickChips = document.getElementById('quickWorkerChips');
                        quickBox.classList.remove('hidden');
                        quickChips.innerHTML = data.workers.map(w => {
                            const isAdded = currentWhitelist.includes(w.name);
                            return `
                                <button onclick="addWhitelistItem('${w.name}')" 
                                    class="px-2 py-0.5 rounded-lg border text-[10px] font-bold transition flex items-center space-x-1 ${isAdded ? 'bg-amber-100 text-amber-800 border-amber-300' : 'bg-white text-slate-600 border-slate-200 hover:bg-amber-50'}">
                                    <span>${w.name}</span>
                                    <span>${isAdded ? '✓' : '+'}</span>
                                </button>
                            `;
                        }).join('');
                    }

                    const matBody = document.getElementById('adminMaterialsBody');
                    if (data.materials.length > 0) {
                        matBody.innerHTML = data.materials.map(m => `
                            <tr class="hover:bg-slate-50">
                                <td class="p-2 font-bold text-slate-700 truncate max-w-[100px]">${m.group_name}</td>
                                <td class="p-2 text-slate-600 truncate max-w-[120px]">${m.title}</td>
                                <td class="p-2 font-mono text-[11px] text-blue-600">${m.last_tag || '-'}</td>
                                <td class="p-2">
                                    ${m.status === 'available' ? '<span class="px-2 py-0.5 bg-emerald-100 text-emerald-700 font-bold rounded">待领(独家)</span>' :
                                      m.status === 'assigned' ? '<span class="px-2 py-0.5 bg-amber-100 text-amber-700 font-bold rounded">领用中</span>' :
                                      '<span class="px-2 py-0.5 bg-slate-200 text-slate-600 font-bold rounded">已消耗作废</span>'}
                                </td>
                                <td class="p-2 text-slate-500">${m.assigned_to || '-'}</td>
                                <td class="p-2 text-right">
                                    <button onclick="deleteMaterial(${m.id}, '${m.group_name}')" class="text-red-600 hover:text-red-700 font-bold text-[11px]">
                                        删除
                                    </button>
                                </td>
                            </tr>
                        `).join('');
                    } else {
                        matBody.innerHTML = '<tr><td colspan="6" class="p-4 text-center text-slate-400">暂无素材，请在上方添加新素材</td></tr>';
                    }

                    allAdminSubmissions = data.submissions || [];
                    updateFilterWorkerDropdown(data.workers || []);
                    applySubmissionsFilter();
                } else if (data.error) {
                    adminAuthToken = '';
                    localStorage.removeItem('xhs_admin_pwd');
                    openAdmin();
                }
            } catch (err) {
                showToast('加载管理后台失败');
            }
        }

        function updateFilterWorkerDropdown(workers) {
            const select = document.getElementById('filterWorkerSelect');
            if (!select) return;
            const currentVal = select.value;
            
            const nameSet = new Set();
            currentWhitelist.forEach(n => { if (n) nameSet.add(n); });
            allAdminSubmissions.forEach(s => { if (s.user_name) nameSet.add(s.user_name); });
            workers.forEach(w => { if (w.name) nameSet.add(w.name); });
            
            const sortedNames = Array.from(nameSet).sort();
            select.innerHTML = '<option value="">-- 全部兼职 (不限) --</option>' + 
                sortedNames.map(n => `<option value="${n}">${n}</option>`).join('');
                
            if (sortedNames.includes(currentVal)) {
                select.value = currentVal;
            }
        }

        function setFilterSettlePreset(preset) {
            const sSelect = document.getElementById('filterSettleSelect');
            if (sSelect) {
                sSelect.value = preset;
                applySubmissionsFilter();
            }
        }

        function setFilterDatePreset(preset) {
            const dateInput = document.getElementById('filterDateInput');
            if (!dateInput) return;
            const now = new Date();
            const formatDate = (d) => {
                const year = d.getFullYear();
                const month = String(d.getMonth() + 1).padStart(2, '0');
                const day = String(d.getDate()).padStart(2, '0');
                return `${year}-${month}-${day}`;
            };
            
            if (preset === 'all') {
                dateInput.value = '';
                dateInput.dataset.preset = '';
            } else if (preset === 'today') {
                dateInput.value = formatDate(now);
                dateInput.dataset.preset = '';
            } else if (preset === 'yesterday') {
                const yest = new Date(now);
                yest.setDate(yest.getDate() - 1);
                dateInput.value = formatDate(yest);
                dateInput.dataset.preset = '';
            } else if (preset === '7days') {
                dateInput.value = '';
                dateInput.dataset.preset = '7days';
            }
            applySubmissionsFilter();
        }

        function resetSubmissionsFilter() {
            const wSelect = document.getElementById('filterWorkerSelect');
            const dInput = document.getElementById('filterDateInput');
            const sSelect = document.getElementById('filterSettleSelect');
            if (wSelect) wSelect.value = '';
            if (dInput) { dInput.value = ''; dInput.dataset.preset = ''; }
            if (sSelect) sSelect.value = '';
            applySubmissionsFilter();
            showToast('已重置所有筛选条件');
        }

        function applySubmissionsFilter() {
            const worker = document.getElementById('filterWorkerSelect')?.value || '';
            const dateInput = document.getElementById('filterDateInput');
            const dateVal = dateInput?.value || '';
            const presetVal = dateInput?.dataset?.preset || '';
            const settle = document.getElementById('filterSettleSelect')?.value || '';
            const subBody = document.getElementById('adminSubmissionsBody');
            const countBadge = document.getElementById('filterResultCountBadge');
            
            const now = new Date();
            const sevenDaysAgo = new Date(now);
            sevenDaysAgo.setDate(sevenDaysAgo.getDate() - 7);
            const year = sevenDaysAgo.getFullYear();
            const month = String(sevenDaysAgo.getMonth() + 1).padStart(2, '0');
            const day = String(sevenDaysAgo.getDate()).padStart(2, '0');
            const sevenDaysAgoStr = `${year}-${month}-${day}`;

            currentlyFilteredSubmissions = allAdminSubmissions.filter(s => {
                if (worker && s.user_name !== worker) return false;

                const isSettled = s.settlement_status === 'settled';
                const subDateObj = new Date(s.submitted_at ? s.submitted_at.replace(/-/g, '/') : '');
                const diffHours = (now - subDateObj) / (1000 * 60 * 60);
                const isPast24H = diffHours >= 24;

                if (settle === 'ready_24h' && (isSettled || !isPast24H)) return false;
                if (settle === 'under_24h' && (isSettled || isPast24H)) return false;
                if (settle === 'unsettled' && isSettled) return false;
                if (settle === 'settled' && !isSettled) return false;
                
                const subDate = s.submitted_at ? s.submitted_at.split(' ')[0] : '';
                if (presetVal === '7days') {
                    if (subDate < sevenDaysAgoStr) return false;
                } else if (dateVal && subDate !== dateVal) {
                    return false;
                }
                return true;
            });

            const total = currentlyFilteredSubmissions.length;
            const settledCount = currentlyFilteredSubmissions.filter(s => s.settlement_status === 'settled').length;
            const ready24Count = currentlyFilteredSubmissions.filter(s => s.settlement_status !== 'settled' && ((now - new Date(s.submitted_at.replace(/-/g, '/'))) / (1000 * 60 * 60)) >= 24).length;
            const under24Count = total - settledCount - ready24Count;
            if (countBadge) {
                countBadge.innerHTML = `共 ${total} 条 (💰 满24H可结: <strong class="text-purple-700 font-black">${ready24Count}</strong> / ⏳ 观察中: ${under24Count} / 🟢 已结: ${settledCount})`;
            }

            if (!subBody) return;

            if (currentlyFilteredSubmissions.length > 0) {
                subBody.innerHTML = currentlyFilteredSubmissions.map(s => {
                    const isSettled = s.settlement_status === 'settled';
                    const subDateObj = new Date(s.submitted_at ? s.submitted_at.replace(/-/g, '/') : '');
                    const diffHours = (now - subDateObj) / (1000 * 60 * 60);
                    const isPast24H = diffHours >= 24;
                    const remainingHours = Math.max(0, 24 - diffHours);
                    const remainStr = remainingHours < 1 ? Math.round(remainingHours * 60) + '分钟' : remainingHours.toFixed(1) + '小时';

                    let timerBadge = '';
                    if (isSettled) {
                        timerBadge = `<span class="px-1.5 py-0.5 rounded text-[10px] font-bold bg-emerald-50 text-emerald-700 border border-emerald-200">🟢 提成已结清</span>`;
                    } else if (isPast24H) {
                        timerBadge = `<span class="px-1.5 py-0.5 rounded text-[10px] font-bold bg-purple-100 text-purple-800 border border-purple-300">💰 满24H·可发薪 (已发${diffHours.toFixed(1)}h)</span>`;
                    } else {
                        timerBadge = `<span class="px-1.5 py-0.5 rounded text-[10px] font-bold bg-amber-50 text-amber-800 border border-amber-200">⏳ 观察中 (剩 ${remainStr} 达24H)</span>`;
                    }

                    const survStatus = s.survival_status === 'active' ? '<span class="text-emerald-600 font-bold bg-emerald-50 px-1.5 py-0.5 rounded border border-emerald-200">🟢 正常存活</span>' :
                                       s.survival_status === 'dead' ? '<span class="text-red-600 font-bold bg-red-50 px-1.5 py-0.5 rounded border border-red-200">🔴 笔记已失效</span>' :
                                       s.survival_status === 'in_review' ? '<span class="text-amber-700 font-bold bg-amber-50 px-1.5 py-0.5 rounded border border-amber-200">⏳ 官方审核中</span>' :
                                       '<span class="text-slate-400">待巡检</span>';

                    const tagInfo = s.tag_matched === 1 ? `<span class="text-emerald-700 font-bold bg-emerald-50 px-1.5 py-0.5 rounded border border-emerald-200" title="${s.tag_expected || ''}">✅ 已核Tag</span>` :
                                                          `<span class="text-amber-700 font-bold bg-amber-50 px-1.5 py-0.5 rounded border border-amber-200" title="${s.tag_expected || ''}">⚠️ Tag待查</span>`;
                    return `
                        <tr class="hover:bg-blue-50/50 transition">
                            <td class="p-2.5 whitespace-nowrap">
                                <div class="font-mono text-[11px] font-bold text-slate-800">${s.submitted_at ? s.submitted_at.substring(5, 16) : '-'}</div>
                                <div class="mt-0.5">${timerBadge}</div>
                            </td>
                            <td class="p-2.5 font-bold text-slate-900 whitespace-nowrap">
                                <span class="px-2 py-0.5 rounded-full bg-slate-100 text-slate-800 border border-slate-200">${s.user_name}</span>
                            </td>
                            <td class="p-2.5 text-slate-700 truncate max-w-[130px]" title="${s.material_name}">${s.material_name}</td>
                            <td class="p-2.5">
                                <div class="flex items-center space-x-1.5">
                                    <a href="${s.xhs_link}" target="_blank" class="text-red-600 font-bold hover:underline flex items-center space-x-0.5 truncate max-w-[100px]" title="${s.xhs_link}">
                                        <span>🔗 打开笔记</span>
                                    </a>
                                    <button onclick="copySingleLink('${s.xhs_link}')" title="一键复制链接" class="text-[10px] text-slate-600 hover:text-blue-600 bg-slate-100 hover:bg-slate-200 px-1.5 py-0.5 rounded font-medium transition shadow-xs">
                                        复制
                                    </button>
                                </div>
                            </td>
                            <td class="p-2.5 text-[11px] whitespace-nowrap">
                                <div class="flex items-center space-x-1.5">
                                    <span>${tagInfo}</span>
                                    <span>${survStatus}</span>
                                </div>
                            </td>
                            <td class="p-2.5 text-center whitespace-nowrap">
                                <button onclick="toggleSettlement(${s.id}, '${s.settlement_status || 'unsettled'}')" 
                                    class="px-2.5 py-1 rounded-full text-[11px] font-bold transition shadow-sm ${isSettled ? 'bg-emerald-100 hover:bg-emerald-200 text-emerald-800 border border-emerald-300' : (isPast24H ? 'bg-purple-600 hover:bg-purple-700 text-white shadow-md shadow-purple-600/30 font-extrabold' : 'bg-amber-100 hover:bg-amber-200 text-amber-800 border border-amber-300')}">
                                    <span>${isSettled ? '🟢 已结算' : (isPast24H ? '💰 满24H·点击结钱' : '🟡 待结算')}</span>
                                </button>
                            </td>
                        </tr>
                    `;
                }).join('');
            } else {
                subBody.innerHTML = '<tr><td colspan="6" class="p-6 text-center text-slate-400 font-medium">没有查到符合条件的打卡记录，请尝试调整筛选条件</td></tr>';
            }
        }

        function copySingleLink(link) {
            if (!link) return;
            navigator.clipboard.writeText(link).then(() => {
                showToast('已复制小红书链接到剪贴板！');
            }).catch(() => {
                showToast('复制失败，请手动长按复制');
            });
        }

        function copyFilteredLinks() {
            if (!currentlyFilteredSubmissions || currentlyFilteredSubmissions.length === 0) {
                showToast('当前没有查询到任何回传记录！');
                return;
            }
            const nl = String.fromCharCode(10);
            const linkList = currentlyFilteredSubmissions.map((s, idx) => 
                (idx + 1) + '. 【' + (s.user_name || '') + '】 ' + (s.material_name || '') + ' (' + (s.submitted_at || '-') + '):' + nl + s.xhs_link
            ).join(nl + nl);
            
            navigator.clipboard.writeText(linkList).then(() => {
                showToast('🎉 成功批量复制 ' + currentlyFilteredSubmissions.length + ' 条回传链接！');
            }).catch(() => {
                showToast('复制失败，请重试');
            });
        }
    </script>
</body>
</html>
"""

@app.route('/')
def index():
    return render_template_string(INDEX_HTML)

if __name__ == '__main__':
    init_db()
    scan_and_import_materials_from_folder()
    print("Starting server on port 5050...")
    app.run(host='0.0.0.0', port=5050, debug=False)
