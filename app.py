import os
import re
import io
import json
import base64
import sqlite3
import datetime
import zipfile
import mimetypes
import urllib.request
from flask import Flask, request, jsonify, send_file, render_template_string, Response

app = Flask(__name__)
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'distributor.db')
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
MATERIALS_DIR = os.path.join(PROJECT_ROOT, 'materials')

def get_db():
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
        note TEXT
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
    
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('passcode', '8888')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('admin_password', '060521')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('auth_mode', 'passcode')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('whitelist', '[\"y\", \"小明\", \"小红\"]')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('daily_limit', '3')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('cooldown_minutes', '0')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('strict_tag_check', '1')")
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

def extract_last_tag(copy_text):
    if not copy_text:
        return ''
    tags = re.findall(r'#[^\s#]+', copy_text)
    return tags[-1] if tags else ''

def check_worker_auth(user_name, passcode):
    auth_mode = get_setting('auth_mode', 'passcode')
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
        if user_name not in whitelist:
            return False, f"⚠️ 未授权的分发人员【{user_name}】！你尚未在 3金 的兼职白名单中，请联系 3金 添加授权。"
            
    return True, ""

def auto_detect_xhs_link_with_tag(url, expected_tag, expected_title):
    if not ('xhslink.com' in url or 'xiaohongshu.com' in url):
        return False, "请提供有效的小红书笔记分享链接 (xhslink.com 或 xiaohongshu.com)！", "", False, "invalid_url"
        
    headers = {
        'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1'
    }
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=4) as response:
            html = response.read().decode('utf-8', errors='ignore')
            title_m = re.search(r'<title>(.*?)</title>', html)
            fetched_title = title_m.group(1).replace(' - 小红书', '').strip() if title_m else ''
            
            tag_clean = expected_tag.replace('#', '').strip() if expected_tag else ''
            tag_found = (tag_clean in html or tag_clean in fetched_title) if tag_clean else True
            
            keywords = [w for w in re.split(r'[\s_，。：:、！!]+', expected_title) if len(w) >= 2]
            title_matched = any(k in html or k in fetched_title for k in keywords) if keywords else True
            
            check_status = 'matched' if (tag_found or title_matched) else 'suspicious'
            return True, "", fetched_title or "已解析到有效笔记", (tag_found or title_matched), check_status
    except Exception as e:
        return True, "", "已提交待后台复核", True, "unverified"

def scan_and_import_materials_from_folder():
    target_dir = MATERIALS_DIR if os.path.exists(MATERIALS_DIR) else '/Users/air/Desktop/9月1日代运营整'
    if not os.path.exists(target_dir):
        return 0

    conn = get_db()
    cursor = conn.cursor()
    imported_count = 0
    now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

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
        
        images_full = [os.path.join(group_path, img) for img in img_files]
        
        title = group
        if copy_text:
            first_line = copy_text.split('\n')[0].strip()
            if first_line:
                title = first_line[:30]
        
        last_tag = extract_last_tag(copy_text)

        cursor.execute("""
        INSERT OR IGNORE INTO materials (group_name, title, folder_path, images_json, copy_text, last_tag, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 'available', ?)
        """, (group, title, group_path, json.dumps(images_full, ensure_ascii=False), copy_text, last_tag, now_str))
        if cursor.rowcount > 0:
            imported_count += 1
        else:
            cursor.execute("""
            UPDATE materials SET copy_text = ?, last_tag = ? WHERE group_name = ?
            """, (copy_text, last_tag, group))

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
            elif os.path.exists(img_ref):
                ext = os.path.splitext(img_ref)[1]
                zf.write(img_ref, f"图{idx+1}_配图{ext}")
        if copy_text:
            zf.writestr('发布文案.txt', copy_text)
            
    memory_file.seek(0)
    clean_name = re.sub(r'[^\w\u4e00-\u9fa5]', '_', mat['group_name'])[:20]
    filename = f"{clean_name}.zip"
    return send_file(memory_file, mimetype='application/zip', as_attachment=True, download_name=filename)

@app.route('/api/image')
def serve_image():
    path = request.args.get('path', '')
    if not path or not os.path.exists(path):
        return 'Image not found', 404
    mime, _ = mimetypes.guess_type(path)
    return send_file(path, mimetype=mime or 'image/jpeg')

@app.route('/api/user/status', methods=['GET'])
def get_user_status():
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
    
    today_str = datetime.datetime.now().strftime('%Y-%m-%d')
    cursor.execute('SELECT COUNT(*) FROM submissions WHERE user_name = ? AND submitted_at LIKE ?', (name, f'{today_str}%'))
    today_count = cursor.fetchone()[0]
    
    daily_limit = int(get_setting('daily_limit', '3'))
    conn.close()
    
    return jsonify({
        'success': True,
        'user': {
            'name': name,
            'completed_count': user['completed_count'] if user else 0,
            'today_count': today_count,
            'daily_limit': daily_limit,
            'current_material': current_material,
            'history': subs
        }
    })

@app.route('/api/claim', methods=['POST'])
def claim_material():
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
    now_dt = datetime.datetime.now()
    now_str = now_dt.strftime('%Y-%m-%d %H:%M:%S')
    today_str = now_dt.strftime('%Y-%m-%d')
    
    daily_limit = int(get_setting('daily_limit', '3'))
    cursor.execute('SELECT COUNT(*) FROM submissions WHERE user_name = ? AND submitted_at LIKE ?', (user_name, f'{today_str}%'))
    today_submitted = cursor.fetchone()[0]
    
    cursor.execute('SELECT * FROM users WHERE name = ?', (user_name,))
    user = cursor.fetchone()
    current_mat_id = user['current_material_id'] if user else None
    
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
            
            url_match = re.search(r'https?://[^\s]+', xhs_link)
            clean_url = url_match.group(0) if url_match else xhs_link
            
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
            
            strict_tag = get_setting('strict_tag_check', '1') == '1'
            if strict_tag and not matched and check_status == 'suspicious':
                conn.close()
                return jsonify({
                    'success': False,
                    'error': f'❌ 核验未通过：系统未在该小红书作品中检测到文案专属 Tag【{expected_tag}】或标题关键词！\n请确认是否按要求完整复制发布，切勿提交他人或无关笔记。'
                })
            
            cursor.execute("""
            INSERT INTO submissions (user_name, material_id, material_name, xhs_link, xhs_title, tag_expected, tag_matched, check_status, submitted_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'verified')
            """, (user_name, curr_mat['id'], curr_mat['group_name'], clean_url, xhs_title, expected_tag, 1 if matched else 0, check_status, now_str))
            
            auto_delete = get_setting('auto_delete_consumed', '0') == '1'
            if auto_delete:
                cursor.execute('DELETE FROM materials WHERE id = ?', (curr_mat['id'],))
            else:
                cursor.execute('UPDATE materials SET status = "completed" WHERE id = ?', (curr_mat['id'],))
            
            today_submitted += 1
            cursor.execute("""
            UPDATE users SET completed_count = completed_count + 1, current_material_id = NULL, last_active = ?
            WHERE name = ?
            """, (now_str, user_name))
            
    if today_submitted >= daily_limit:
        conn.commit()
        conn.close()
        return jsonify({
            'success': False,
            'reached_limit': True,
            'error': f'🛑 你今天已成功打卡 {today_submitted} 篇，已达到单日领料上限（{daily_limit} 篇/天）！小红书单号频繁发帖易被平台限流，请明天再来领取~'
        })
        
    cooldown_min = int(get_setting('cooldown_minutes', '0'))
    if cooldown_min > 0:
        cursor.execute('SELECT submitted_at FROM submissions WHERE user_name = ? ORDER BY id DESC LIMIT 1', (user_name,))
        last_sub = cursor.fetchone()
        if last_sub:
            last_time = datetime.datetime.strptime(last_sub['submitted_at'], '%Y-%m-%d %H:%M:%S')
            diff_seconds = (now_dt - last_time).total_seconds()
            required_seconds = cooldown_min * 60
            if diff_seconds < required_seconds:
                remaining_min = int((required_seconds - diff_seconds) / 60) + 1
                conn.commit()
                conn.close()
                return jsonify({
                    'success': False,
                    'error': f'⏳ 小红书养号防封保护：距离上一篇发布还需等待 {remaining_min} 分钟冷却时间，稍后再来领取下一组！'
                })
    
    cursor.execute('SELECT * FROM materials WHERE status = "available" ORDER BY id ASC LIMIT 1')
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
    UPDATE materials SET status = "assigned", assigned_to = ?, assigned_at = ? WHERE id = ?
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
        'auth_mode': get_setting('auth_mode', 'passcode'),
        'daily_limit': get_setting('daily_limit', '3'),
        'cooldown_minutes': get_setting('cooldown_minutes', '0'),
        'strict_tag_check': get_setting('strict_tag_check', '1') == '1',
        'auto_delete_consumed': get_setting('auto_delete_consumed', '0') == '1',
        'admin_password': get_setting('admin_password', '060521'),
        'whitelist': whitelist
    })

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
    now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
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
    now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
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
    cursor.execute('DELETE FROM materials WHERE status = "completed"')
    count = cursor.rowcount
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'message': f'已成功清理 {count} 组已消耗的素材！'})

@app.route('/api/admin/stats', methods=['GET'])
def admin_stats():
    admin_pwd = request.headers.get('X-Admin-Password', '')
    real_pwd = get_setting('admin_password', '060521').strip()
    if admin_pwd != real_pwd:
        return jsonify({'success': False, 'error': '管理密码错误或未登录'}), 401

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM materials')
    total_materials = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM materials WHERE status = "available"')
    available = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM materials WHERE status = "assigned"')
    assigned = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM materials WHERE status = "completed"')
    completed = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM submissions')
    total_submissions = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM users')
    total_users = cursor.fetchone()[0]
    
    cursor.execute('SELECT id, group_name, title, last_tag, status, assigned_to, assigned_at FROM materials ORDER BY id ASC')
    materials_list = [dict(r) for r in cursor.fetchall()]
    
    cursor.execute('SELECT * FROM submissions ORDER BY id DESC')
    submissions_list = [dict(r) for r in cursor.fetchall()]
    
    conn.close()
    return jsonify({
        'success': True,
        'stats': {
            'total_materials': total_materials,
            'available': available,
            'assigned': assigned,
            'completed': completed,
            'total_submissions': total_submissions,
            'total_users': total_users
        },
        'materials': materials_list,
        'submissions': submissions_list
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
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT id, user_name, material_name, xhs_link, xhs_title, tag_expected, tag_matched, check_status, submitted_at, status FROM submissions ORDER BY id DESC')
    rows = cursor.fetchall()
    conn.close()
    
    csv_content = "\ufeffID,分发人员姓名,领取作品组名,小红书发布链接,抓取标题,文案核验Tag,Tag核验结果,系统质检状态,提交打卡时间,核验状态\n"
    for r in rows:
        t_str = r["xhs_title"] if r["xhs_title"] else "-"
        tag_str = r["tag_expected"] if r["tag_expected"] else "-"
        match_str = "已匹配Tag" if r["tag_matched"] == 1 else "未检测到Tag"
        c_str = r["check_status"] if r["check_status"] else "-"
        csv_content += f'"{r["id"]}","{r["user_name"]}","{r["material_name"]}","{r["xhs_link"]}","{t_str}","{tag_str}","{match_str}","{c_str}","{r["submitted_at"]}","{r["status"]}"\n'
        
    filename = f"小红书矩阵打卡统计_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
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
    <!-- Tailwind CSS CDN -->
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
            <div class="flex items-center space-x-2">
                <div class="w-8 h-8 rounded-lg xhs-gradient flex items-center justify-center text-white font-bold text-lg shadow-sm">
                    小
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
                    <span class="font-bold text-sm text-slate-800">分发人员身份与口令验证</span>
                </div>
                <div id="completedBadge" class="text-xs font-semibold px-2.5 py-0.5 rounded-full bg-red-50 text-red-600 border border-red-100 hidden">
                    今日已打卡 <span id="todayCountSpan">0</span>/<span id="dailyLimitSpan">3</span> 组
                </div>
            </div>

            <div class="mt-3 grid grid-cols-1 sm:grid-cols-2 gap-2">
                <div>
                    <label class="block text-[11px] font-semibold text-slate-500 mb-1">分发人姓名 / 微信昵称：</label>
                    <input type="text" id="userNameInput" placeholder="例如: y / 小明" 
                        class="w-full px-3 py-2 text-sm bg-slate-50 border border-slate-200 rounded-xl focus:outline-none focus:ring-2 focus:ring-red-500 focus:bg-white transition"
                        oninput="saveCredentials()">
                </div>
                <div>
                    <label class="block text-[11px] font-semibold text-slate-500 mb-1">领料口令 / 工号：</label>
                    <input type="password" id="passcodeInput" placeholder="请输入领料口令" 
                        class="w-full px-3 py-2 text-sm bg-slate-50 border border-slate-200 rounded-xl focus:outline-none focus:ring-2 focus:ring-red-500 focus:bg-white transition"
                        oninput="saveCredentials()">
                </div>
            </div>
            <div class="mt-2.5 flex justify-end">
                <button onclick="checkUserStatus()" class="px-4 py-2 text-xs bg-slate-800 hover:bg-slate-900 text-white rounded-xl font-bold transition shadow-sm">
                    🔐 验证并同步状态
                </button>
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
                    📌 <strong>领料说明</strong>：输入姓名与口令后，点击下方按钮即可领取专属独家发布素材（每组素材独家派发，不重复使用）！
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
                    <p class="mt-1.5 text-slate-600 leading-relaxed">小红书发布完成后，复制该作品的<strong>分享链接</strong>粘贴在下方，即可提交打卡并领取下一组新素材！</p>
                </div>

                <div>
                    <label class="block text-xs font-semibold text-slate-700 mb-1">粘贴小红书已发布笔记链接：</label>
                    <textarea id="xhsLinkInput" rows="2" placeholder="长按粘贴小红书笔记分享链接 (例如: http://xhslink.com/... 或完整分享文本)"
                        class="w-full p-3 text-sm bg-slate-50 border border-slate-200 rounded-xl focus:outline-none focus:ring-2 focus:ring-red-500 focus:bg-white transition"></textarea>
                </div>

                <button onclick="claimMaterial(true)" id="claimBtn" class="w-full py-3.5 bg-emerald-600 hover:bg-emerald-700 text-white rounded-xl font-bold text-sm shadow-md shadow-emerald-600/20 transition flex items-center justify-center space-x-2">
                    <span>🚀 提交打卡 ➔ 领取下一组</span>
                </button>
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
                        <span class="text-xs text-red-500 font-medium hidden sm:inline">💡 点击可放大 · 长按保存</span>
                    </div>
                </div>
                <div class="grid grid-cols-3 gap-2" id="imagesGrid">
                    <!-- Image Cards populated by JS -->
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

            <!-- Rules Reminder Card -->
            <div class="p-3.5 bg-slate-50 rounded-xl border border-slate-200 text-xs text-slate-600 space-y-1.5">
                <div class="font-bold text-slate-800 flex items-center space-x-1">
                    <span>⚠️</span>
                    <span>发布规范与客户引流指引</span>
                </div>
                <p>1. <strong>发布顺序</strong>：图片必须按 图1(封面)、图2(内容)、图3(尾图) 顺序选择上传；</p>
                <p>2. <strong>客户留资</strong>：有人在评论区留言时，先回“已私”，然后在私信发引导视频；</p>
                <p>3. <strong>提成结算</strong>：客户成功添加微信后，截图发给 3金 当天结算！</p>
            </div>
        </div>

        <!-- History Submissions -->
        <div id="historyCard" class="bg-white rounded-2xl p-5 shadow-sm border border-slate-200 hidden">
            <h3 class="font-bold text-sm text-slate-900 mb-3 flex items-center space-x-1.5">
                <span>📑</span>
                <span>我的打卡记录</span>
            </h3>
            <div class="space-y-2 text-xs" id="historyList">
                <!-- History Items -->
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
                    class="w-full px-3 py-2.5 text-sm bg-slate-50 border border-slate-200 rounded-xl focus:outline-none focus:ring-2 focus:ring-amber-500 focus:bg-white text-center font-bold tracking-widest">
            </div>
            <div class="flex gap-2">
                <button onclick="closeAdminLogin()" class="flex-1 py-2.5 text-xs bg-slate-100 hover:bg-slate-200 text-slate-600 rounded-xl font-bold transition">取消</button>
                <button onclick="verifyAdminLogin()" class="flex-1 py-2.5 text-xs bg-slate-900 hover:bg-slate-800 text-white rounded-xl font-bold transition shadow-sm">进入后台</button>
            </div>
        </div>
    </div>

    <!-- Admin Dashboard Modal -->
    <div id="adminModal" class="fixed inset-0 z-50 bg-black/60 backdrop-blur-sm flex items-center justify-center p-4 hidden">
        <div class="bg-white rounded-2xl max-w-3xl w-full max-h-[92vh] flex flex-col shadow-2xl overflow-hidden">
            <!-- Modal Header -->
            <div class="px-5 py-4 border-b border-slate-100 flex items-center justify-between bg-slate-900 text-white">
                <div class="flex items-center space-x-2">
                    <span class="text-xl">👑</span>
                    <div>
                        <h2 class="font-bold text-base">3金 的矩阵管理后台</h2>
                        <p class="text-xs text-slate-400">白名单授权 · 批量拼装 · 团队协同</p>
                    </div>
                </div>
                <button onclick="toggleAdminModal()" class="text-slate-400 hover:text-white text-xl font-bold">&times;</button>
            </div>

            <!-- Modal Content (Scrollable) -->
            <div class="p-5 overflow-y-auto space-y-5 flex-1">
                
                <!-- Stat Cards -->
                <div class="grid grid-cols-3 gap-3">
                    <div class="bg-slate-50 p-3 rounded-xl border border-slate-200 text-center">
                        <div class="text-xs text-slate-500 font-medium">总素材组数</div>
                        <div class="text-xl font-bold text-slate-900 mt-0.5" id="statTotal">0</div>
                    </div>
                    <div class="bg-emerald-50 p-3 rounded-xl border border-emerald-100 text-center">
                        <div class="text-xs text-emerald-700 font-medium">剩余待领 (独家)</div>
                        <div class="text-xl font-bold text-emerald-600 mt-0.5" id="statAvailable">0</div>
                    </div>
                    <div class="bg-blue-50 p-3 rounded-xl border border-blue-100 text-center">
                        <div class="text-xs text-blue-700 font-medium">已消耗作废</div>
                        <div class="text-xl font-bold text-blue-600 mt-0.5" id="statCompleted">0</div>
                    </div>
                </div>

                <!-- Security, Whitelist & Anti-Cheat Settings Card -->
                <div class="p-4 bg-amber-50/80 rounded-2xl border border-amber-200 space-y-3.5">
                    <div class="flex items-center justify-between border-b border-amber-200/60 pb-2">
                        <h3 class="font-bold text-xs text-amber-900 flex items-center space-x-1.5 uppercase tracking-wider">
                            <span>🛡️</span>
                            <span>兼职白名单与防作弊规则管理</span>
                        </h3>
                        <span class="text-[10px] text-amber-800 bg-amber-200/80 px-2 py-0.5 rounded font-bold">即时生效</span>
                    </div>

                    <div class="grid grid-cols-1 sm:grid-cols-2 gap-3 text-xs">
                        <div>
                            <label class="block font-semibold text-slate-700 mb-1">1. 兼职领料验证模式：</label>
                            <select id="settingAuthMode" class="w-full px-3 py-1.5 bg-white border border-amber-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-amber-500 font-bold text-amber-900">
                                <option value="passcode">🔑 仅验证口令 (默认: 知道口令就能领，名字随便填)</option>
                                <option value="whitelist">📋 仅验证白名单 (名字必须在下方白名单内)</option>
                                <option value="both">🔒 双重验证 (必须在白名单 且 口令正确，最严格)</option>
                                <option value="none">🌐 开放模式 (免验证，任意领)</option>
                            </select>
                        </div>

                        <div>
                            <label class="block font-semibold text-slate-700 mb-1">2. 统一领料口令：</label>
                            <input type="text" id="settingPasscode" placeholder="如: 8888" 
                                class="w-full px-3 py-1.5 bg-white border border-amber-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-amber-500 font-bold text-amber-900">
                        </div>

                        <div>
                            <label class="block font-semibold text-slate-700 mb-1">3. 每日单人领料上限 (篇/天)：</label>
                            <input type="number" id="settingDailyLimit" min="1" max="20" placeholder="如: 3" 
                                class="w-full px-3 py-1.5 bg-white border border-amber-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-amber-500 font-bold text-amber-900">
                        </div>

                        <div>
                            <label class="block font-semibold text-slate-700 mb-1">4. 已发作品防复用策略：</label>
                            <select id="settingAutoDelete" class="w-full px-3 py-1.5 bg-white border border-amber-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-amber-500 font-medium text-slate-800">
                                <option value="0">标记为【已消耗】(保留记录，绝不再派发任何人)</option>
                                <option value="1">打卡后【立即自动物理销毁】(完全不留痕迹)</option>
                            </select>
                        </div>
                    </div>

                    <!-- WHITELIST NAMES EDITOR -->
                    <div class="pt-1">
                        <div class="flex items-center justify-between mb-1">
                            <label class="block font-semibold text-slate-700 text-xs">
                                5. 授权兼职人员姓名/微信昵称白名单 (多个名字用逗号或换行隔开)：
                            </label>
                            <span class="text-[10px] text-slate-400">只有名单内的人可领料</span>
                        </div>
                        <textarea id="settingWhitelist" rows="2" placeholder="例如: y, 小明, 小红, 矩阵兼职01, 张三" 
                            class="w-full p-2 bg-white border border-amber-300 rounded-lg text-xs font-mono text-slate-800 focus:outline-none focus:ring-2 focus:ring-amber-500"></textarea>
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
                    <div class="flex items-center space-x-2 border-b border-emerald-200/80 pb-3">
                        <button onclick="switchUploadTab('batch')" id="tabBtnBatch" class="px-3.5 py-1.5 rounded-xl text-xs font-bold bg-emerald-600 text-white shadow-sm transition">
                            ⚡️ 【批量图库拼装】(同事多选图1/图2/图3，一键生成几十组)
                        </button>
                        <button onclick="switchUploadTab('single')" id="tabBtnSingle" class="px-3.5 py-1.5 rounded-xl text-xs font-bold bg-white text-slate-700 border border-slate-200 hover:bg-slate-50 transition">
                            📌 【单组精准上传】
                        </button>
                    </div>

                    <!-- TAB 1: BATCH AUTO ASSEMBLER -->
                    <div id="batchUploadPanel" class="space-y-3 text-xs">
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

                    <!-- TAB 2: SINGLE SLOT UPLOAD -->
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
                    <div class="border border-slate-200 rounded-xl overflow-hidden max-h-56 overflow-y-auto">
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

                <!-- Recent Submissions Table -->
                <div>
                    <div class="flex items-center justify-between mb-2">
                        <h3 class="font-bold text-xs text-slate-800 uppercase tracking-wider">📋 实时打卡审核表：</h3>
                        <a href="/api/admin/export_csv" class="text-[11px] text-emerald-600 hover:text-emerald-700 font-bold">
                            📥 导出 Excel 统计表
                        </a>
                    </div>
                    <div class="border border-slate-200 rounded-xl overflow-hidden">
                        <table class="w-full text-xs text-left border-collapse">
                            <thead class="bg-slate-100 text-slate-600 font-semibold border-b border-slate-200">
                                <tr>
                                    <th class="p-2.5">时间</th>
                                    <th class="p-2.5">人员</th>
                                    <th class="p-2.5">领取组名</th>
                                    <th class="p-2.5">文案尾Tag</th>
                                    <th class="p-2.5">小红书链接</th>
                                    <th class="p-2.5">质检</th>
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

        window.addEventListener('DOMContentLoaded', () => {
            const savedName = localStorage.getItem('xhs_distributor_name') || '';
            const savedPasscode = localStorage.getItem('xhs_distributor_passcode') || '8888';
            if (savedName) document.getElementById('userNameInput').value = savedName;
            if (savedPasscode) document.getElementById('passcodeInput').value = savedPasscode;
            if (savedName && savedPasscode) checkUserStatus();
        });

        function saveCredentials() {
            const name = document.getElementById('userNameInput').value.trim();
            const passcode = document.getElementById('passcodeInput').value.trim();
            if (name) localStorage.setItem('xhs_distributor_name', name);
            if (passcode) localStorage.setItem('xhs_distributor_passcode', passcode);
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
            const name = document.getElementById('userNameInput').value.trim();
            const passcode = document.getElementById('passcodeInput').value.trim();
            if (!name) {
                showToast('请先输入你的姓名或微信昵称');
                return;
            }
            saveCredentials();

            try {
                const res = await fetch(`/api/user/status?name=${encodeURIComponent(name)}&passcode=${encodeURIComponent(passcode)}`);
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

        function renderUserState(user) {
            const badge = document.getElementById('completedBadge');
            const todayCountSpan = document.getElementById('todayCountSpan');
            const dailyLimitSpan = document.getElementById('dailyLimitSpan');
            const firstClaimBox = document.getElementById('firstClaimBox');
            const submitLinkBox = document.getElementById('submitLinkBox');
            const activeGroupName = document.getElementById('activeGroupName');
            const matCard = document.getElementById('materialContentCard');
            const historyCard = document.getElementById('historyCard');
            const historyList = document.getElementById('historyList');

            badge.classList.remove('hidden');
            todayCountSpan.innerText = user.today_count || 0;
            dailyLimitSpan.innerText = user.daily_limit || 3;

            if (user.current_material) {
                firstClaimBox.classList.add('hidden');
                submitLinkBox.classList.remove('hidden');
                activeGroupName.innerText = user.current_material.group_name;
                renderMaterialCard(user.current_material);
            } else {
                firstClaimBox.classList.remove('hidden');
                submitLinkBox.classList.add('hidden');
                matCard.classList.add('hidden');
            }

            if (user.history && user.history.length > 0) {
                historyCard.classList.remove('hidden');
                historyList.innerHTML = user.history.map(item => `
                    <div class="p-3 bg-slate-50 rounded-xl border border-slate-100 flex items-center justify-between">
                        <div>
                            <div class="font-bold text-slate-800">${item.material_name}</div>
                            <div class="text-[11px] text-slate-500 mt-0.5 flex items-center space-x-1.5">
                                <span>🏷️ ${item.tag_expected || '-'}</span>
                                <span class="text-slate-300">|</span>
                                <span class="truncate max-w-[140px]">${item.xhs_title || '已打卡'}</span>
                            </div>
                            <a href="${item.xhs_link}" target="_blank" class="text-blue-600 hover:underline truncate max-w-[220px] block mt-0.5">
                                🔗 ${item.xhs_link}
                            </a>
                        </div>
                        <div class="text-right text-[11px] text-slate-400">
                            <div>${item.submitted_at.split(' ')[1]}</div>
                            <span class="inline-block mt-0.5 px-2 py-0.5 bg-emerald-100 text-emerald-700 rounded font-semibold">已核验</span>
                        </div>
                    </div>
                `).join('');
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
                    <div class="relative group rounded-xl overflow-hidden border border-slate-200 bg-slate-100 aspect-[3/4] flex flex-col">
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
            const name = document.getElementById('userNameInput').value.trim();
            const passcode = document.getElementById('passcodeInput').value.trim();
            if (!name) {
                showToast('请先输入你的姓名或微信昵称！');
                return;
            }
            saveCredentials();

            let xhsLink = '';
            if (isNext) {
                xhsLink = document.getElementById('xhsLinkInput').value.trim();
                if (!xhsLink) {
                    showToast('请粘贴刚刚发布的小红书链接后再提交！');
                    return;
                }
            }

            const btn = document.getElementById('claimBtn');
            if (btn && isNext) {
                btn.innerHTML = '<span>🔍 正在提交核验...</span>';
                btn.disabled = true;
            }

            try {
                const res = await fetch('/api/claim', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        user_name: name,
                        passcode: passcode,
                        xhs_link: xhsLink
                    })
                });
                const data = await res.json();
                if (data.success) {
                    showToast(data.message);
                    document.getElementById('xhsLinkInput').value = '';
                    checkUserStatus();
                } else {
                    showToast(data.error);
                }
            } catch (err) {
                showToast('网络请求异常');
            } finally {
                if (btn && isNext) {
                    btn.innerHTML = '<span>🚀 提交打卡 ➔ 领取下一组</span>';
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
            }
        }

        function closeAdminLogin() {
            document.getElementById('adminLoginModal').classList.add('hidden');
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
                } else {
                    showToast(data.error);
                }
            } catch (err) {
                showToast('登录验证异常');
            }
        }

        function toggleAdminModal() {
            const modal = document.getElementById('adminModal');
            if (modal.classList.contains('hidden')) {
                modal.classList.remove('hidden');
                loadAdminData();
                loadAdminSettings();
            } else {
                modal.classList.add('hidden');
            }
        }

        function switchUploadTab(tab) {
            const batchP = document.getElementById('batchUploadPanel');
            const singleP = document.getElementById('singleUploadPanel');
            const batchB = document.getElementById('tabBtnBatch');
            const singleB = document.getElementById('tabBtnSingle');
            if (tab === 'batch') {
                batchP.classList.remove('hidden');
                singleP.classList.add('hidden');
                batchB.classList.replace('bg-white', 'bg-emerald-600');
                batchB.classList.replace('text-slate-700', 'text-white');
                singleB.classList.replace('bg-emerald-600', 'bg-white');
                singleB.classList.replace('text-white', 'text-slate-700');
            } else {
                batchP.classList.add('hidden');
                singleP.classList.remove('hidden');
                singleB.classList.replace('bg-white', 'bg-emerald-600');
                singleB.classList.replace('text-slate-700', 'text-white');
                batchB.classList.replace('bg-emerald-600', 'bg-white');
                batchB.classList.replace('text-white', 'text-slate-700');
            }
        }

        function updateBatchCount(slot) {
            const input = document.getElementById(`batchSlot${slot}`);
            const span = document.getElementById(`batchCount${slot}`);
            const count = input.files ? input.files.length : 0;
            span.innerText = `已选 ${count} 张`;
        }

        async function loadAdminSettings() {
            try {
                const res = await fetch('/api/admin/settings', {
                    headers: { 'X-Admin-Password': adminAuthToken }
                });
                const data = await res.json();
                if (data.success) {
                    document.getElementById('settingAuthMode').value = data.auth_mode || 'passcode';
                    document.getElementById('settingPasscode').value = data.passcode || '8888';
                    document.getElementById('settingDailyLimit').value = data.daily_limit || 3;
                    document.getElementById('settingAutoDelete').value = data.auto_delete_consumed ? '1' : '0';
                    const list = data.whitelist || [];
                    document.getElementById('settingWhitelist').value = Array.isArray(list) ? list.join(', ') : list;
                }
            } catch (err) {}
        }

        async function saveAdminSettings() {
            const auth_mode = document.getElementById('settingAuthMode').value;
            const passcode = document.getElementById('settingPasscode').value.trim();
            const daily_limit = document.getElementById('settingDailyLimit').value.trim();
            const auto_delete = document.getElementById('settingAutoDelete').value === '1';
            const whitelist_raw = document.getElementById('settingWhitelist').value.trim();

            try {
                const res = await fetch('/api/admin/settings', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-Admin-Password': adminAuthToken
                    },
                    body: JSON.stringify({
                        auth_mode: auth_mode,
                        passcode: passcode,
                        daily_limit: daily_limit,
                        auto_delete_consumed: auto_delete,
                        whitelist: whitelist_raw
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

            const copies = raw_copy.split(/===+|\n---+\n/).map(c => c.trim()).filter(c => c.length > 0);
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
                    document.getElementById('statTotal').innerText = data.stats.total_materials;
                    document.getElementById('statAvailable').innerText = data.stats.available;
                    document.getElementById('statCompleted').innerText = data.stats.completed;

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

                    const subBody = document.getElementById('adminSubmissionsBody');
                    if (data.submissions.length > 0) {
                        subBody.innerHTML = data.submissions.map(s => `
                            <tr class="hover:bg-slate-50">
                                <td class="p-2.5 text-slate-500 whitespace-nowrap">${s.submitted_at.split(' ')[1]}</td>
                                <td class="p-2.5 font-bold text-slate-800">${s.user_name}</td>
                                <td class="p-2.5 text-slate-700 truncate max-w-[100px]">${s.material_name}</td>
                                <td class="p-2.5 font-mono text-[11px] font-bold text-blue-600">${s.tag_expected || '-'}</td>
                                <td class="p-2.5">
                                    <a href="${s.xhs_link}" target="_blank" class="text-red-600 text-[11px] font-semibold hover:underline flex items-center space-x-1 truncate max-w-[130px]">
                                        <span>🔗 点此核验</span>
                                    </a>
                                </td>
                                <td class="p-2.5 whitespace-nowrap">
                                    ${s.tag_matched === 1 ? '<span class="px-2 py-0.5 bg-emerald-100 text-emerald-700 font-bold rounded">🟢 匹配</span>' :
                                      '<span class="px-2 py-0.5 bg-amber-100 text-amber-700 font-bold rounded">🟡 异常</span>'}
                                </td>
                            </tr>
                        `).join('');
                    } else {
                        subBody.innerHTML = '<tr><td colspan="6" class="p-4 text-center text-slate-400">暂无打卡记录</td></tr>';
                    }
                } else if (data.error) {
                    adminAuthToken = '';
                    localStorage.removeItem('xhs_admin_pwd');
                    openAdmin();
                }
            } catch (err) {
                showToast('加载管理后台失败');
            }
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
