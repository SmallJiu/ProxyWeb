import os
import json
import logging
import requests
from flask import Flask, render_template, request, Response, abort, send_from_directory, redirect, url_for
from werkzeug.utils import safe_join
from urllib.parse import urljoin, urlparse

# ---------- 自定义日志过滤器：只记录非 200 状态码的请求 ----------
class LoggerFilter(logging.Filter):
    def filter(self, record):
        message = record.getMessage()
        if ' 200 ' in message or ' 200 -' in message or message.endswith(' 200'):
            return False
        if ' 304 ' in message or ' 304 -' in message or message.endswith(' 304'):
            return False
        if '_navbar.md' in message or '_sidebar.md' in message:
            return False
        return True

werkzeug_logger = logging.getLogger('werkzeug')
werkzeug_logger.addFilter(LoggerFilter())

app = Flask(__name__)
app.secret_key = 'sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxx'

WEBS_DIR = 'webs'
PROXY_TIMEOUT = 30
CONFIG_FILE = os.path.join(WEBS_DIR, 'index.json')

# ---------- 工具函数 ----------
def load_proxy_configs():
    """
    从 webs/index.json 加载代理配置。
    返回代理站点列表，每个元素 dict:
	{
	  'name': str, # 在导航页面上显示的名称
	  'path': str, # URL 子路径，如 `site1` 对应 `/site1/`
	  'url': str,  # 实际代理的后端地址（支持 HTTP/HTTPS）
	  'hide': bool # 可选，设置为 `true` 可隐藏在导航页面中，但仍可通过直接访问路径访问
	}
    """
    configs = []
    if not os.path.isfile(CONFIG_FILE):
        return configs

    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        app.logger.error(f"解析 {CONFIG_FILE} 失败: {e}")
        return configs

    if isinstance(data, list):
        items = data
    elif isinstance(data, dict) and 'webs' in data:
        items = data['webs']
    else:
        items = []

    for item in items:
        configs.append({
            'name': item.get('name', ''),
            'path': item.get('path', '').strip('/'),
            'url': item.get('url', '').rstrip('/'),
            'hide': item.get('hide', False),
            'direct': item.get('direct', False)
        })
    return configs

# ---------- 新增：加载本地站点 config.json ----------
def load_local_config(local_dir_path):
    """
    读取本地站点目录下的 config.json，返回配置字典。
    若文件不存在或无效，返回空字典。
    """
    config_path = os.path.join(local_dir_path, 'config.json')
    if not os.path.isfile(config_path):
        return {}
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        app.logger.error(f"解析 {config_path} 失败: {e}")
        return {}

def scan_sites():
    """
    扫描生成站点列表：
    1. 从 webs/index.json 加载代理配置。
    2. 扫描 webs/ 下子目录，若有 index.html 则视为本地站点。
    支持本地站点 config.json 自定义名称(name)和路径(path)。
    注意：若本地站点路径与代理路径冲突，优先代理。
    """
    sites = []
    used_paths = set()  # 记录已使用的路径（用于冲突检测）

    # 1. 加载代理站点
    proxy_configs = load_proxy_configs()
    for cfg in proxy_configs:
        if not cfg['path']:
            continue
        used_paths.add(cfg['path'])
        if not cfg['hide']:
            sites.append({
                'name': cfg['name'],
                'path': cfg['path'],
                'type': 'proxy',
                'url': cfg['url'],
                'direct': cfg['direct']
            })

    # 2. 扫描本地站点（排除已被代理占用的路径）
    if os.path.isdir(WEBS_DIR):
        for entry in os.listdir(WEBS_DIR):
            full_path = os.path.join(WEBS_DIR, entry)
            if not os.path.isdir(full_path):
                continue
            # 必须有 index.html 才视为本地站点
            index_html = os.path.join(full_path, 'index.html')
            if not os.path.isfile(index_html):
                continue

            # 读取该目录下的 config.json
            local_cfg = load_local_config(full_path)

            # 确定显示名称：优先 config.json 的 name，否则用目录名
            site_name = local_cfg.get('name', '').strip()
            if not site_name:
                site_name = entry

            # 确定访问路径：优先 config.json 的 path，否则用目录名
            site_path = local_cfg.get('path', '').strip()
            if not site_path:
                site_path = entry

            # 若该路径已被占用，跳过并警告
            if site_path in used_paths:
                app.logger.warning(f"本地站点路径 '{site_path}' 已被占用，已忽略 (目录: {entry})")
                continue

            used_paths.add(site_path)
            sites.append({
                'name': site_name,
                'path': site_path,
                'type': 'local',
                'url': None,
                'physical_dir': entry  # 保留实际目录名，用于后续文件服务
            })

    sites.sort(key=lambda x: x['name'])
    return sites

def get_proxy_config(path_prefix):
    """根据代理路径前缀查找对应的代理配置"""
    configs = load_proxy_configs()
    for cfg in configs:
        if cfg['path'] == path_prefix:
            return cfg
    return None

def get_local_dir_by_path(site_path):
    """
    根据配置的路径（可能不同于物理目录名）找到实际物理目录名。
    返回物理目录名，若未找到则返回 None。
    """
    if not os.path.isdir(WEBS_DIR):
        return None

    for entry in os.listdir(WEBS_DIR):
        full_path = os.path.join(WEBS_DIR, entry)
        if not os.path.isdir(full_path):
            continue
        index_html = os.path.join(full_path, 'index.html')
        if not os.path.isfile(index_html):
            continue

        local_cfg = load_local_config(full_path)
        cfg_path = local_cfg.get('path', '').strip()
        if not cfg_path:
            cfg_path = entry
        if cfg_path == site_path:
            return entry
    return None

def is_safe_redirect(target):
    parsed = urlparse(target)
    return parsed.scheme in ('http', 'https') and parsed.netloc

# ---------- 错误处理 ----------
@app.errorhandler(404)
def page_not_found(e):
    return render_template('404.html'), 404

@app.errorhandler(502)
def bad_gateway(e):
    return render_template('502.html'), 502

# ---------- 首页：树形导航 ----------
@app.route('/')
def index():
    sites = scan_sites()
	# 处理 direct 代理站点的 URL 替换（本地地址 → 当前请求主机）
    for site in sites:
        if site['type'] == 'proxy' and site.get('direct'):
            original_url = site['url']
            parsed = urlparse(original_url)
            # 判断是否为本地回环地址
            if parsed.hostname in ('127.0.0.1', 'localhost', '::1'):
                # 获取当前请求的主机名（不含端口）
                current_hostname = request.host.split(':')[0]
                # 保留原端口
                if parsed.port:
                    new_netloc = f"{current_hostname}:{parsed.port}"
                else:
                    new_netloc = current_hostname
                # 构造新 URL
                new_parsed = parsed._replace(netloc=new_netloc)
                site['direct_url'] = urlunparse(new_parsed)
            else:
                site['direct_url'] = original_url
    return render_template('index.html', sites=sites)

# ---------- 本地静态文件服务 & 代理分发 ----------
@app.route('/<path:subpath>', methods=['GET', 'POST', 'PUT', 'DELETE'])
def serve_or_proxy(subpath):
    parts = subpath.split('/')
    first_part = parts[0] if parts else ''

    # 1. 代理请求处理
    proxy_config = get_proxy_config(first_part)
    if proxy_config:
        return handle_proxy(subpath, proxy_config)

    # 2. 检查是否为配置了自定义路径的本地站点
    local_physical = get_local_dir_by_path(first_part)
    if local_physical:
        # 将路径中的第一段替换为实际物理目录名
        remaining = '/'.join(parts[1:]) if len(parts) > 1 else ''
        if remaining:
            new_subpath = local_physical + '/' + remaining
        else:
            new_subpath = local_physical

        local_path = safe_join(WEBS_DIR, new_subpath)
        if local_path is None:
            abort(404)

        # 目录重定向时保留自定义路径
        if os.path.isdir(local_path) and not request.path.endswith('/'):
            redirect_path = '/' + first_part + '/'
            if remaining:
                redirect_path = '/' + first_part + '/' + remaining + '/'
            return redirect(redirect_path, code=301)

        if os.path.isdir(local_path):
            index_file = os.path.join(local_path, 'index.html')
            if os.path.isfile(index_file):
                return send_from_directory(local_path, 'index.html')
            else:
                abort(404)

        if os.path.isfile(local_path):
            return send_from_directory(os.path.dirname(local_path), os.path.basename(local_path))

    # 3. 回退：按原始物理目录名访问
    local_path = safe_join(WEBS_DIR, subpath)
    if local_path is None:
        abort(404)

    if os.path.isdir(local_path) and not request.path.endswith('/'):
        return redirect(request.path + '/', code=301)

    if os.path.isdir(local_path):
        index_file = os.path.join(local_path, 'index.html')
        if os.path.isfile(index_file):
            return send_from_directory(local_path, 'index.html')
        else:
            abort(404)

    if os.path.isfile(local_path):
        return send_from_directory(os.path.dirname(local_path), os.path.basename(local_path))

    abort(404)

# ---------- 代理处理核心 ----------
def handle_proxy(path, proxy_config):
    target_base = proxy_config['url']
    proxy_prefix = proxy_config['path']

    if path == proxy_prefix:
        remaining = ''
    elif path.startswith(proxy_prefix + '/'):
        remaining = path[len(proxy_prefix)+1:]
    else:
        abort(404)

    target_url = urljoin(target_base + '/', remaining)
    if request.query_string:
        target_url += '?' + request.query_string.decode('utf-8')

    headers = {key: value for key, value in request.headers if key.lower() not in (
        'host', 'connection', 'keep-alive', 'proxy-connection',
        'transfer-encoding', 'upgrade'
    )}
    headers['X-Forwarded-For'] = request.remote_addr
    headers['X-Forwarded-Host'] = request.host
    headers['X-Forwarded-Proto'] = request.scheme

    try:
        resp = requests.request(
            method=request.method,
            url=target_url,
            headers=headers,
            data=request.get_data(),
            cookies=request.cookies,
            allow_redirects=False,
            timeout=PROXY_TIMEOUT,
            stream=True
        )
    except requests.exceptions.RequestException as e:
        app.logger.error(f"代理请求失败: {target_url} - {e}")
        abort(502)

    if resp.status_code in (301, 302, 303, 307, 308):
        location = resp.headers.get('Location')
        if location:
            parsed = urlparse(location)
            if parsed.netloc:
                new_location = '/' + proxy_prefix + parsed.path
                if parsed.query:
                    new_location += '?' + parsed.query
            else:
                new_location = urljoin('/' + proxy_prefix + '/', location)
            return redirect(new_location, code=resp.status_code)

    excluded_headers = ('content-encoding', 'content-length', 'transfer-encoding', 'connection')
    headers = [(name, value) for name, value in resp.raw.headers.items()
               if name.lower() not in excluded_headers]

    return Response(
        resp.iter_content(chunk_size=1024),
        status=resp.status_code,
        headers=headers,
        content_type=resp.headers.get('Content-Type')
    )

# ---------- 绝对路径资源智能补全（基于 Referer）----------
@app.before_request
def handle_absolute_resource():
    if request.path == '/' or request.path.startswith('/static/'):
        return

    referer = request.headers.get('Referer')
    if not referer:
        return

    parsed_ref = urlparse(referer)
    ref_path = parsed_ref.path

    # 使用全部代理配置（包含隐藏的）
    all_proxy_configs = load_proxy_configs()
    for cfg in all_proxy_configs:
        prefix = '/' + cfg['path']
        if ref_path.startswith(prefix + '/') or ref_path == prefix:
            if not request.path.startswith(prefix + '/') and request.path != prefix:
                new_path = prefix + request.path
                if request.query_string:
                    new_path += '?' + request.query_string.decode('utf-8')
                return redirect(new_path, code=307)

# ---------- 启动入口 ----------
if __name__ == '__main__':
    os.makedirs(WEBS_DIR, exist_ok=True)
    app.run(host='0.0.0.0', port=80, threaded=True)