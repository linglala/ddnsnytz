import os
import json
import time
import random
import string
import threading
import subprocess
import logging
import requests
from datetime import datetime
from functools import wraps
from flask import Flask, request, jsonify, session, render_template, redirect, url_for
from flask_socketio import SocketIO, emit

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'awssb-panel-secret-2024')
socketio = SocketIO(app, cors_allowed_origins="*")

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

CONFIG_FILE = 'config.json'
HISTORY_FILE = 'history.json'

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {
        'username': os.environ.get('PANEL_USER', 'admin'),
        'password': os.environ.get('PANEL_PASS', 'admin123'),
        'groups': [],
        'instances': [],
        'check_interval': 60,
        'fail_threshold': 3,
        'check_port': 22,
        'bark_url': '',
        'report_key': ''
    }

def save_config(cfg):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)

def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE) as f:
            return json.load(f)
    return []

def save_history(h):
    with open(HISTORY_FILE, 'w') as f:
        json.dump(h[-200:], f, indent=2, ensure_ascii=False)

config = load_config()
history = load_history()
runtime = {}

def get_runtime(profile_id):
    if profile_id not in runtime:
        runtime[profile_id] = {
            'ipv4_fails': 0, 'ipv6_fails': 0,
            'ipv4_status': 'unknown', 'ipv6_status': 'unknown',
            'ipv4': '', 'ipv6': '', 'replacing': False, 'last_check': ''
        }
    return runtime[profile_id]

def rand_r(n=11):
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=n))

def add_history(profile_id, name, event, detail, status='info'):
    entry = {
        'id': rand_r(8),
        'profile_id': profile_id,
        'name': name,
        'event': event,
        'detail': detail,
        'status': status,
        'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }
    history.insert(0, entry)
    save_history(history)
    socketio.emit('history_update', entry)
    return entry

def send_bark(msg):
    bark_url = config.get('bark_url', '').strip()
    if not bark_url:
        return
    try:
        url = bark_url.rstrip('/') + '/' + requests.utils.quote(msg)
        requests.get(url, timeout=5)
    except Exception as e:
        log.warning(f'Bark 通知失败: {e}')

# ========== AWS.sb API ==========
AWSSB_BASE = 'https://aws.sb/api'
HEADERS = {
    'Content-Type': 'application/json',
    'Referer': 'https://aws.sb/',
    'Origin': 'https://aws.sb',
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36'
}

def fetch_instance_profiles(sgt):
    r = rand_r()
    resp = requests.get(f'{AWSSB_BASE}/ec2-instance-profiles?r={r}',
                        headers={**HEADERS, 'x-share-group-token': sgt}, timeout=15)
    resp.raise_for_status()
    return resp.json()

def do_replace_ip(instance_id, profile_id, sgt):
    r = rand_r()
    resp = requests.post(
        f'{AWSSB_BASE}/filter-tasks?r={r}',
        headers={**HEADERS, 'x-share-group-token': sgt},
        json={'tags': [instance_id, profile_id]},
        timeout=15
    )
    resp.raise_for_status()
    return resp.json()

# ========== Cloudflare API ==========
CF_BASE = 'https://api.cloudflare.com/client/v4'

def cf_headers(token):
    return {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}

def cf_list_records(token, zone_id, name):
    resp = requests.get(f'{CF_BASE}/zones/{zone_id}/dns_records',
                        params={'name': name, 'type': 'A'},
                        headers=cf_headers(token), timeout=10)
    data = resp.json()
    return data.get('result', [])

def cf_list_aaaa_records(token, zone_id, name):
    resp = requests.get(f'{CF_BASE}/zones/{zone_id}/dns_records',
                        params={'name': name, 'type': 'AAAA'},
                        headers=cf_headers(token), timeout=10)
    data = resp.json()
    return data.get('result', [])

def cf_add_record(token, zone_id, name, ip, record_type='A'):
    resp = requests.post(f'{CF_BASE}/zones/{zone_id}/dns_records',
                         headers=cf_headers(token),
                         json={'type': record_type, 'name': name, 'content': ip, 'ttl': 60, 'proxied': False},
                         timeout=10)
    return resp.json()

def cf_delete_record(token, zone_id, record_id):
    resp = requests.delete(f'{CF_BASE}/zones/{zone_id}/dns_records/{record_id}',
                           headers=cf_headers(token), timeout=10)
    return resp.json()

def cf_update_dns(group, old_ip, new_ip):
    """换IP后更新CF DNS：删除旧IP记录，添加新IP记录"""
    token = group.get('cf_token', '')
    zone_id = group.get('cf_zone_id', '')
    domain = group.get('cf_domain', '')
    if not token or not zone_id or not domain:
        return
    try:
        records = cf_list_records(token, zone_id, domain)
        for r in records:
            if r['content'] == old_ip:
                cf_delete_record(token, zone_id, r['id'])
                log.info(f'CF DNS 删除旧IP: {old_ip}')
        existing_ips = [r['content'] for r in cf_list_records(token, zone_id, domain)]
        if new_ip not in existing_ips:
            cf_add_record(token, zone_id, domain, new_ip)
            log.info(f'CF DNS 添加新IP: {new_ip}')
    except Exception as e:
        log.warning(f'CF DNS 更新失败: {e}')

def cf_remove_blocked_ip(group, ip):
    """检测到IP被墙，从CF DNS删除"""
    token = group.get('cf_token', '')
    zone_id = group.get('cf_zone_id', '')
    domain = group.get('cf_domain', '')
    if not token or not zone_id or not domain:
        return
    try:
        records = cf_list_records(token, zone_id, domain)
        for r in records:
            if r['content'] == ip:
                cf_delete_record(token, zone_id, r['id'])
                log.info(f'CF DNS 删除被墙IP: {ip}')
    except Exception as e:
        log.warning(f'CF DNS 删除失败: {e}')

# ========== IP 检测 ==========
def check_tcp(ip, port, timeout=5):
    import socket
    start = time.time()
    try:
        af = socket.AF_INET6 if ':' in ip else socket.AF_INET
        sock = socket.socket(af, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        result = sock.connect_ex((ip, port))
        sock.close()
        latency = int((time.time() - start) * 1000)
        return result == 0, latency
    except Exception:
        return False, 0

def check_ping(ip, timeout=5):
    flag = '-6' if ':' in ip else '-4'
    try:
        start = time.time()
        result = subprocess.run(['ping', flag, '-c', '1', '-W', str(timeout), ip],
                                capture_output=True, timeout=timeout+2)
        latency = int((time.time() - start) * 1000)
        return result.returncode == 0, latency
    except Exception:
        return False, 0

def check_ip(ip, port=22):
    if port:
        ok, ms = check_tcp(ip, port)
        if ok:
            return True, ms
    ok, ms = check_ping(ip)
    return ok, ms

# ========== 监控主循环 ==========
stop_event = threading.Event()

def get_group(group_id):
    return next((g for g in config.get('groups', []) if g['id'] == group_id), None)

def monitor_loop():
    log.info('监控线程启动')
    while not stop_event.is_set():
        for inst in config.get('instances', []):
            if stop_event.is_set():
                break
            profile_id = inst['profile_id']
            rt = get_runtime(profile_id)
            if rt['replacing']:
                continue

            port = inst.get('check_port', config.get('check_port', 22))
            threshold = config.get('fail_threshold', 3)
            now = datetime.now().strftime('%H:%M:%S')
            rt['last_check'] = now
            check_ipv4 = inst.get('check_ipv4', True)
            check_ipv6 = inst.get('check_ipv6', False)

            # 检测 IPv4
            if check_ipv4 and rt['ipv4']:
                ok4, ms4 = check_ip(rt['ipv4'], port)
                if ok4:
                    rt['ipv4_fails'] = 0
                    rt['ipv4_status'] = f'通 {ms4}ms'
                else:
                    rt['ipv4_fails'] += 1
                    rt['ipv4_status'] = f'不通 {rt["ipv4_fails"]}/{threshold}'
            elif not check_ipv4:
                rt['ipv4_status'] = '未检测'
            else:
                rt['ipv4_status'] = '无IP'

            # 检测 IPv6
            if check_ipv6 and rt['ipv6']:
                ok6, ms6 = check_ip(rt['ipv6'], port)
                if ok6:
                    rt['ipv6_fails'] = 0
                    rt['ipv6_status'] = f'通 {ms6}ms'
                else:
                    rt['ipv6_fails'] += 1
                    rt['ipv6_status'] = f'不通 {rt["ipv6_fails"]}/{threshold}'
            elif not check_ipv6:
                rt['ipv6_status'] = '未检测'
            else:
                rt['ipv6_status'] = '无IPv6'

            socketio.emit('status_update', {
                'profile_id': profile_id,
                'ipv4': rt['ipv4'],
                'ipv6': rt['ipv6'],
                'ipv4_status': rt['ipv4_status'],
                'ipv6_status': rt['ipv6_status'],
                'last_check': now
            })

            need_replace = (
                (check_ipv4 and rt['ipv4'] and rt['ipv4_fails'] >= threshold) or
                (check_ipv6 and rt['ipv6'] and rt['ipv6_fails'] >= threshold)
            )

            if need_replace and inst.get('auto_replace', True):
                log.info(f'{inst["name"]} 检测到IP被墙，触发换IP')
                rt['replacing'] = True
                old_ip = rt['ipv4']
                rt['ipv4_fails'] = 0
                rt['ipv6_fails'] = 0
                socketio.emit('replacing', {'profile_id': profile_id})

                # 先从CF DNS删除被墙IP
                group = get_group(inst.get('group_id', ''))
                if group:
                    cf_remove_blocked_ip(group, old_ip)

                try:
                    result = do_replace_ip(inst['instance_id'], profile_id, inst['sgt'])
                    msg = 'IP被墙，已自动换IP'
                    add_history(profile_id, inst['name'], '自动换IP', msg, 'success')
                    send_bark(f'[{inst["name"]}] {msg}')
                    time.sleep(300)  # 等待新IP生效
                except Exception as e:
                    msg = f'换IP失败: {e}'
                    add_history(profile_id, inst['name'], '换IP失败', msg, 'error')
                    send_bark(f'[{inst["name"]}] {msg}')
                finally:
                    rt['replacing'] = False

        stop_event.wait(config.get('check_interval', 60))
    log.info('监控线程停止')

def start_monitor():
    global stop_event
    stop_event.clear()
    t = threading.Thread(target=monitor_loop, daemon=True)
    t.start()

def restart_monitor():
    global stop_event
    stop_event.set()
    time.sleep(1)
    start_monitor()

# ========== 登录验证 ==========
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            if request.is_json:
                return jsonify({'error': 'unauthorized'}), 401
            return redirect(url_for('login_page'))
        return f(*args, **kwargs)
    return decorated

# ========== 路由 ==========
@app.route('/')
def index():
    if not session.get('logged_in'):
        return redirect(url_for('login_page'))
    return render_template('index.html')

@app.route('/login')
def login_page():
    return render_template('login.html')

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    if data.get('username') == config['username'] and data.get('password') == config['password']:
        session['logged_in'] = True
        return jsonify({'ok': True})
    return jsonify({'ok': False, 'error': '用户名或密码错误'}), 401

@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'ok': True})

# ========== 分组 ==========
@app.route('/api/groups', methods=['GET'])
@login_required
def get_groups():
    return jsonify(config.get('groups', []))

@app.route('/api/groups', methods=['POST'])
@login_required
def add_group():
    data = request.json
    if not data.get('name'):
        return jsonify({'error': '名称必填'}), 400
    group = {
        'id': rand_r(8),
        'name': data['name'],
        'cf_token': data.get('cf_token', ''),
        'cf_zone_id': data.get('cf_zone_id', ''),
        'cf_domain': data.get('cf_domain', '')
    }
    config.setdefault('groups', []).append(group)
    save_config(config)
    return jsonify({'ok': True, 'group': group})

@app.route('/api/groups/<group_id>', methods=['PUT'])
@login_required
def update_group(group_id):
    group = next((g for g in config.get('groups', []) if g['id'] == group_id), None)
    if not group:
        return jsonify({'error': '不存在'}), 404
    data = request.json
    for k in ['name', 'cf_token', 'cf_zone_id', 'cf_domain']:
        if k in data:
            group[k] = data[k]
    save_config(config)
    return jsonify({'ok': True})

@app.route('/api/groups/<group_id>', methods=['DELETE'])
@login_required
def delete_group(group_id):
    config['groups'] = [g for g in config.get('groups', []) if g['id'] != group_id]
    save_config(config)
    return jsonify({'ok': True})

@app.route('/api/groups/<group_id>/cf-records', methods=['GET'])
@login_required
def get_cf_records(group_id):
    group = get_group(group_id)
    if not group:
        return jsonify({'error': '不存在'}), 404
    try:
        records = cf_list_records(group['cf_token'], group['cf_zone_id'], group['cf_domain'])
        return jsonify({'ok': True, 'records': records})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ========== 实例 ==========
@app.route('/api/instances', methods=['GET'])
@login_required
def get_instances():
    result = []
    for inst in config.get('instances', []):
        pid = inst['profile_id']
        rt = get_runtime(pid)
        result.append({**inst, **rt})
    return jsonify(result)

@app.route('/api/instances', methods=['POST'])
@login_required
def add_instance():
    data = request.json
    for f in ['instance_id', 'profile_id', 'sgt']:
        if not data.get(f):
            return jsonify({'error': f'{f} 必填'}), 400
    inst = {
        'name': data.get('name') or data['instance_id'],
        'sgt': data['sgt'],
        'instance_id': data['instance_id'],
        'profile_id': data['profile_id'],
        'check_port': int(data.get('check_port', 22)),
        'auto_replace': data.get('auto_replace', True),
        'check_ipv4': data.get('check_ipv4', True),
        'check_ipv6': data.get('check_ipv6', False),
        'region': data.get('region', ''),
        'group_id': data.get('group_id', '')
    }
    config.setdefault('instances', []).append(inst)
    save_config(config)
    add_history(inst['profile_id'], inst['name'], '添加实例', '实例已添加', 'info')
    return jsonify({'ok': True, 'instance': inst})

@app.route('/api/instances/<profile_id>', methods=['DELETE'])
@login_required
def delete_instance(profile_id):
    config['instances'] = [i for i in config['instances'] if i['profile_id'] != profile_id]
    save_config(config)
    if profile_id in runtime:
        del runtime[profile_id]
    return jsonify({'ok': True})

@app.route('/api/instances/<profile_id>/edit', methods=['POST'])
@login_required
def edit_instance(profile_id):
    inst = next((i for i in config['instances'] if i['profile_id'] == profile_id), None)
    if not inst:
        return jsonify({'error': '不存在'}), 404
    data = request.json
    for k in ['name', 'check_port', 'region', 'group_id', 'auto_replace', 'check_ipv4', 'check_ipv6']:
        if k in data:
            inst[k] = data[k]
    save_config(config)
    return jsonify({'ok': True})

@app.route('/api/instances/<profile_id>/replace', methods=['POST'])
@login_required
def manual_replace(profile_id):
    inst = next((i for i in config['instances'] if i['profile_id'] == profile_id), None)
    if not inst:
        return jsonify({'error': '不存在'}), 404
    try:
        rt = get_runtime(profile_id)
        old_ip = rt.get('ipv4', '')
        result = do_replace_ip(inst['instance_id'], profile_id, inst['sgt'])
        add_history(profile_id, inst['name'], '手动换IP', '手动触发换IP成功', 'success')
        send_bark(f'[{inst["name"]}] 手动换IP成功')
        rt['ipv4_fails'] = 0
        rt['ipv6_fails'] = 0
        return jsonify({'ok': True, 'result': result})
    except Exception as e:
        add_history(profile_id, inst['name'], '手动换IP失败', str(e), 'error')
        return jsonify({'error': str(e)}), 500

@app.route('/api/instances/<profile_id>/toggle', methods=['POST'])
@login_required
def toggle_auto(profile_id):
    inst = next((i for i in config['instances'] if i['profile_id'] == profile_id), None)
    if not inst:
        return jsonify({'error': '不存在'}), 404
    inst['auto_replace'] = not inst.get('auto_replace', True)
    save_config(config)
    return jsonify({'ok': True, 'auto_replace': inst['auto_replace']})

@app.route('/api/instances/replace-all', methods=['POST'])
@login_required
def replace_all():
    results = []
    for inst in config.get('instances', []):
        try:
            do_replace_ip(inst['instance_id'], inst['profile_id'], inst['sgt'])
            results.append({'profile_id': inst['profile_id'], 'ok': True})
        except Exception as e:
            results.append({'profile_id': inst['profile_id'], 'ok': False, 'error': str(e)})
    return jsonify({'ok': True, 'results': results})

@app.route('/api/import', methods=['POST'])
@login_required
def import_from_sgt():
    data = request.json
    sgt = data.get('sgt', '').strip()
    group_id = data.get('group_id', '')
    if not sgt:
        return jsonify({'error': 'sgt 不能为空'}), 400
    try:
        profiles = fetch_instance_profiles(sgt)
        added = 0
        for p in profiles:
            existing = any(i['profile_id'] == p['id'] for i in config['instances'])
            if not existing:
                inst = {
                    'name': p.get('instanceName') or p['instanceId'],
                    'sgt': sgt,
                    'instance_id': p['instanceId'],
                    'profile_id': p['id'],
                    'check_port': 22,
                    'auto_replace': True,
                    'check_ipv4': True,
                    'check_ipv6': False,
                    'region': p.get('regionName', ''),
                    'group_id': group_id
                }
                config['instances'].append(inst)
                added += 1
        save_config(config)
        return jsonify({'ok': True, 'added': added, 'total': len(profiles)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ========== IP 上报 ==========
@app.route('/api/report-ip', methods=['POST'])
def report_ip():
    data = request.json
    key = request.headers.get('X-Access-Key') or data.get('key', '')
    if key != config.get('report_key', ''):
        return jsonify({'error': 'Unauthorized'}), 401
    instance_id = data.get('instance_id', '')
    profile_id = data.get('profile_id', '')
    ip = data.get('ip', '')
    ipv6 = data.get('ipv6', '')
    if not ip:
        return jsonify({'error': 'ip required'}), 400

    inst = None
    if instance_id:
        inst = next((i for i in config['instances'] if i.get('instance_id') == instance_id), None)
    if not inst and profile_id:
        inst = next((i for i in config['instances'] if i.get('profile_id') == profile_id), None)
    if not inst:
        return jsonify({'error': 'instance not found'}), 404

    pid = inst['profile_id']
    rt = get_runtime(pid)
    old_ip = rt.get('ipv4', '')
    rt['ipv4'] = ip
    if ipv6:
        rt['ipv6'] = ipv6
    rt['ipv4_fails'] = 0
    rt['ipv6_fails'] = 0

    # 更新CF DNS
    if old_ip and old_ip != ip:
        group = get_group(inst.get('group_id', ''))
        if group:
            cf_update_dns(group, old_ip, ip)

    socketio.emit('status_update', {
        'profile_id': pid,
        'ipv4': ip,
        'ipv6': ipv6 or rt.get('ipv6', ''),
        'ipv4_status': '通 (新IP)',
        'ipv6_status': rt.get('ipv6_status', 'unknown'),
        'last_check': datetime.now().strftime('%H:%M:%S')
    })
    log.info(f'收到IP上报 {inst["name"]}: {old_ip} -> {ip}')
    add_history(pid, inst['name'], 'IP上报', f'新IP: {ip}', 'info')
    return jsonify({'ok': True})

# ========== 历史 ==========
@app.route('/api/history', methods=['GET'])
@login_required
def get_history():
    return jsonify(history[:100])

@app.route('/api/history', methods=['DELETE'])
@login_required
def clear_history():
    history.clear()
    save_history(history)
    return jsonify({'ok': True})

# ========== 设置 ==========
@app.route('/api/settings', methods=['GET'])
@login_required
def get_settings():
    return jsonify({
        'check_interval': config.get('check_interval', 60),
        'fail_threshold': config.get('fail_threshold', 3),
        'check_port': config.get('check_port', 22),
        'bark_url': config.get('bark_url', ''),
        'username': config.get('username', 'admin'),
        'report_key': config.get('report_key', '')
    })

@app.route('/api/settings', methods=['POST'])
@login_required
def update_settings():
    data = request.json
    if 'check_interval' in data:
        config['check_interval'] = max(30, int(data['check_interval']))
    if 'fail_threshold' in data:
        config['fail_threshold'] = max(1, int(data['fail_threshold']))
    if 'check_port' in data:
        config['check_port'] = int(data['check_port'])
    if 'bark_url' in data:
        config['bark_url'] = data['bark_url']
    if 'password' in data and data['password']:
        config['password'] = data['password']
    if 'username' in data and data['username']:
        config['username'] = data['username']
    if 'report_key' in data:
        config['report_key'] = data['report_key']
    save_config(config)
    restart_monitor()
    return jsonify({'ok': True})

if __name__ == '__main__':
    start_monitor()
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port, debug=False)
