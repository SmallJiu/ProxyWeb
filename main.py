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
        # 检查日志消息中是否包含状态码，通常格式如 "GET /path HTTP/1.1" 200 -
        message = record.getMessage()
        # 简单解析，寻找 " 200 " 或 " 200 -" 的模式
        # 若包含状态码 200 则过滤掉（返回 False 表示不输出）
        if ' 200 ' in message or ' 200 -' in message or message.endswith(' 200'):
            return False
			
        if ' 304 ' in message or ' 304 -' in message or message.endswith(' 304'):
            return False
		
        if '_navbar.md' in message or '_sidebar.md' in message:
            return False
		
        return True

# 获取 werkzeug 的 logger 并添加过滤器
werkzeug_logger = logging.getLogger('werkzeug')
werkzeug_logger.addFilter(LoggerFilter())

# 可选：调整日志级别
# werkzeug_logger.setLevel(logging.INFO)

app = Flask(__name__)
app.secret_key = 'sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxx'  # 用于 session（备用）

# 基础配置
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

    # 支持顶层直接数组或包含 "webs" 键的对象
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
            'hide': item.get('hide', False)
        })
    return configs

def scan_sites():
    """
    扫描生成站点列表：
    1. 从 webs/index.json 加载代理配置。
    2. 扫描 webs/ 下子目录，若有 index.html 则视为本地站点。
    注意：若本地站点路径与代理路径冲突，优先代理（因为已从配置加载）。
    """
    sites = []

    # 1. 加载代理站点
    proxy_configs = load_proxy_configs()
    proxy_paths = set()  # 用于冲突检测
    for cfg in proxy_configs:
        if not cfg['path']:
            continue  # 忽略无效配置
        proxy_paths.add(cfg['path'])
        if not cfg['hide']:
            sites.append({
                'name': cfg['name'],
                'path': cfg['path'],
                'type': 'proxy',
                'url': cfg['url']
            })

    # 2. 扫描本地站点（排除已被代理占用的路径）
    if os.path.isdir(WEBS_DIR):
        for entry in os.listdir(WEBS_DIR):
            full_path = os.path.join(WEBS_DIR, entry)
            if not os.path.isdir(full_path):
                continue
            # 检查是否有 index.html
            index_html = os.path.join(full_path, 'index.html')
            if os.path.isfile(index_html):
                # 若路径未被代理配置占用，添加为本地站点
                if entry not in proxy_paths:
                    sites.append({
                        'name': entry,
                        'path': entry,
                        'type': 'local',
                        'url': None
                    })

    # 按名称排序
    sites.sort(key=lambda x: x['name'])
    return sites

def get_proxy_config(path_prefix):
    """根据代理路径前缀查找对应的代理配置（从已加载配置中）"""
    configs = load_proxy_configs()
    for cfg in configs:
        if cfg['path'] == path_prefix:
            return cfg
    return None

def is_safe_redirect(target):
    """防止开放重定向漏洞"""
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
    return render_template('index.html', sites=sites)

# ---------- 本地静态文件服务 & 代理分发 ----------
@app.route('/<path:subpath>', methods=['GET', 'POST', 'PUT', 'DELETE'])
def serve_or_proxy(subpath):
    """
    统一入口：根据路径前缀判断是本地文件还是代理请求。
    """
    # 提取第一段路径作为可能的站点路径
    parts = subpath.split('/')
    first_part = parts[0] if parts else ''

    # 1. 检查是否为代理站点前缀
    proxy_config = get_proxy_config(first_part)
    if proxy_config:
        # 代理请求处理
        return handle_proxy(subpath, proxy_config)

    # 2. 否则尝试作为本地静态文件服务
    # 安全构建本地路径
    local_path = safe_join(WEBS_DIR, subpath)
    if local_path is None:
        abort(404)

    # 如果是目录且没有以斜杠结尾，重定向到带斜杠的URL
    if os.path.isdir(local_path) and not request.path.endswith('/'):
        return redirect(request.path + '/', code=301)

    # 如果请求的是目录，尝试返回 index.html
    if os.path.isdir(local_path):
        index_file = os.path.join(local_path, 'index.html')
        if os.path.isfile(index_file):
            return send_from_directory(local_path, 'index.html')
        else:
            abort(404)

    # 普通文件服务
    if os.path.isfile(local_path):
        return send_from_directory(os.path.dirname(local_path), os.path.basename(local_path))

    # 文件/目录不存在，404
    abort(404)

# ---------- 代理处理核心 ----------
def handle_proxy(path, proxy_config):
    """
    处理代理请求：
    - path: 完整的请求路径，如 'formless/some/page' 或 'formless/static/css/style.css'
    - proxy_config: 代理配置字典
    """
    target_base = proxy_config['url']
    proxy_prefix = proxy_config['path']

    # 计算去除代理前缀后的剩余路径
    if path == proxy_prefix:
        remaining = ''
    elif path.startswith(proxy_prefix + '/'):
        remaining = path[len(proxy_prefix)+1:]
    else:
        abort(404)

    # 构建目标URL
    target_url = urljoin(target_base + '/', remaining)
    if request.query_string:
        target_url += '?' + request.query_string.decode('utf-8')

    # 准备请求头（过滤掉 hop-by-hop 头）
    headers = {key: value for key, value in request.headers if key.lower() not in (
        'host', 'connection', 'keep-alive', 'proxy-connection',
        'transfer-encoding', 'upgrade'
    )}
    # 添加真实客户端信息（可选）
    headers['X-Forwarded-For'] = request.remote_addr
    headers['X-Forwarded-Host'] = request.host
    headers['X-Forwarded-Proto'] = request.scheme

    try:
        # 发起代理请求
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

    # 处理重定向
    if resp.status_code in (301, 302, 303, 307, 308):
        location = resp.headers.get('Location')
        if location:
            # 将目标服务器的重定向 Location 改写为我们的代理前缀
            parsed = urlparse(location)
            if parsed.netloc:
                # 绝对 URL -> 改为相对代理路径
                new_location = '/' + proxy_prefix + parsed.path
                if parsed.query:
                    new_location += '?' + parsed.query
            else:
                # 相对路径 -> 基于当前代理路径拼接
                new_location = urljoin('/' + proxy_prefix + '/', location)
            return redirect(new_location, code=resp.status_code)

    # 构建响应（排除 hop-by-hop 头）
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
    """
    拦截所有请求，如果请求路径是绝对路径（不以代理前缀开头），
    但 Referer 表明来自某个代理站点，则将其视为该代理站点的资源并重写到正确路径。
    例如：Referer: http://localhost:5000/formless/ → 请求 /static/css/style.css
         自动重写为 /formless/static/css/style.css
    """
    if request.path == '/' or request.path.startswith('/static/'):
        # 全局静态文件或首页，不处理
        return

    referer = request.headers.get('Referer')
    if not referer:
        return

    # 解析 Referer 中的路径
    parsed_ref = urlparse(referer)
    ref_path = parsed_ref.path

    # 检查 Referer 路径是否以某个代理前缀开头
    sites = scan_sites()
    for site in sites:
        if site['type'] != 'proxy':
            continue
        prefix = '/' + site['path']
        if ref_path.startswith(prefix + '/') or ref_path == prefix:
            # 当前请求路径若不是以该前缀开头，则重写
            if not request.path.startswith(prefix + '/') and request.path != prefix:
                new_path = prefix + request.path
                # 保留查询参数
                if request.query_string:
                    new_path += '?' + request.query_string.decode('utf-8')
                return redirect(new_path, code=307)  # 临时重定向，保持方法

# ---------- 启动入口 ----------
if __name__ == '__main__':
    # 确保 webs 目录存在
    os.makedirs(WEBS_DIR, exist_ok=True)
    app.run(host='0.0.0.0', port=80)