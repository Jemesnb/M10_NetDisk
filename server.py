#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""USB 网盘 —— 通过行空板 USB 链路访问的文件服务。

零依赖，只用 Python 3.7 标准库（板子上装包不一定方便）。
绑定 0.0.0.0，所以 USB 链路 (10.1.2.3) 和 WiFi 都能访问。

用法：
    python3 server.py              # 默认 8090 端口
    python3 server.py 9000         # 指定端口
    python3 server.py --list       # 只列出可用的存储根，不启动服务
"""

import html
import os
import posixpath
import random
import shutil
import socket
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------- 配置

PORT = 8090
CHUNK = 256 * 1024          # 下载/上传的分块大小

# 默认只暴露 U 盘。板载系统目录不对外 —— /root 下面有配置、密钥、
# .vscode-server 之类的东西，挂到局域网上等于全公开。
# 确实要额外共享某个目录，启动时显式加：
#     python3 server.py --root /root/shared
EXTRA_ROOTS = []            # [(显示名, 路径)]，由 --root 填充

# U 盘常见的挂载位置
USB_MOUNT_DIRS = ["/media", "/mnt", "/run/media", "/media/root"]

MAX_UPLOAD = 2 * 1024 * 1024 * 1024      # 单文件上限 2GB

# 访问口令。首次启动自动生成并写入 token.txt。没有它谁都进不来，
# 包括同一个局域网里的其他设备。
TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "token.txt")

TOKEN = ""                  # 由 main() 读取或生成后填充


# ---------------------------------------------------------------- 存储根探测

def _is_usb_mount(dev, mnt):
    """判断一个挂载点是不是可移动存储。"""
    if not dev.startswith("/dev/sd") and not dev.startswith("/dev/mmcblk0"):
        return False
    for skip in ("/boot", "/", "/media/root"):
        if mnt == skip:
            return False
    return True


def detect_usb_mounts():
    """从 /proc/mounts 里找出已挂载的 U 盘。"""
    found = []
    try:
        with open("/proc/mounts", "r") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) < 3:
                    continue
                dev, mnt = parts[0], parts[1]
                if _is_usb_mount(dev, mnt) and os.path.isdir(mnt):
                    found.append((dev, mnt))
    except IOError:
        pass

    # 也扫一下常见目录，兜住自动挂载的情况
    for base in USB_MOUNT_DIRS:
        if not os.path.isdir(base):
            continue
        try:
            for name in os.listdir(base):
                mnt = os.path.join(base, name)
                if os.path.ismount(mnt):
                    found.append(("", mnt))
        except OSError:
            pass

    # 去重，保持顺序
    seen, out = set(), []
    for dev, mnt in found:
        if mnt not in seen:
            seen.add(mnt)
            out.append((dev, mnt))
    return out


def build_roots():
    """返回 [(显示名, 绝对路径), ...]。默认只有 U 盘 + 显式指定的目录。"""
    roots = []
    for dev, mnt in detect_usb_mounts():
        label = "U盘 " + os.path.basename(mnt)
        if dev:
            label += " (" + os.path.basename(dev) + ")"
        roots.append((label, mnt))
    roots.extend(EXTRA_ROOTS)
    return roots


def resolve_root(root):
    """校验 root 是不是允许暴露的存储根之一。

    这里必须校验：root 是直接从 URL 的 ?r= 参数来的，
    不做检查的话 ?r=/etc 就能把整个系统翻出来。
    返回规范化后的真实路径，不合法返回 None。
    """
    if not root:
        return None
    real = os.path.realpath(root)
    for _name, path in build_roots():
        if os.path.realpath(path) == real:
            return real
    return None


# ---------------------------------------------------------------- 工具

def human_size(n):
    if n < 1024:
        return "%d B" % n
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024.0
        if n < 1024:
            return "%.1f %s" % (n, unit)
    return "%.1f PB" % n


def unique_path(path):
    """目标已存在就加 (2) (3) 之类后缀，不覆盖已有文件。"""
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    n = 2
    while n < 1000:
        cand = "%s (%d)%s" % (base, n, ext)
        if not os.path.exists(cand):
            return cand
        n += 1
    return "%s (%d)%s" % (base, time.time(), ext)


def safe_join(root, rel):
    """把相对路径拼到 root 下，挡住 ../ 越界。"""
    rel = urllib.parse.unquote(rel or "")
    rel = rel.replace("\\", "/").lstrip("/")
    parts = [p for p in rel.split("/") if p not in ("", ".", "..")]
    target = os.path.join(root, *parts)
    # 再确认一次真实路径没跑出 root
    real_root = os.path.realpath(root)
    real_target = os.path.realpath(target)
    if real_target != real_root and not real_target.startswith(real_root + os.sep):
        return None
    return real_target


def disk_usage(path):
    try:
        u = shutil.disk_usage(path)
        return u.total, u.used, u.free
    except OSError:
        return None


# ---------------------------------------------------------------- HTML

CSS = """
*{box-sizing:border-box}
body{margin:0;background:#0E1116;color:#E6EDF3;
     font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
     font-size:15px;line-height:1.5}
a{color:#58A6FF;text-decoration:none}
a:hover{text-decoration:underline}
.wrap{max-width:860px;margin:0 auto;padding:16px}
h1{font-size:19px;margin:0 0 4px;font-weight:600}
.sub{color:#7D8590;font-size:13px;margin-bottom:16px}
.crumb{background:#161B22;border:1px solid #30363D;border-radius:6px;
       padding:8px 12px;margin-bottom:12px;font-size:14px;word-break:break-all}
.crumb a{color:#58A6FF}
table{width:100%;border-collapse:collapse}
th{text-align:left;color:#7D8590;font-weight:500;font-size:12px;
   padding:6px 8px;border-bottom:1px solid #30363D;text-transform:uppercase}
td{padding:8px;border-bottom:1px solid #21262D;font-size:14px}
tr:hover td{background:#161B22}
.icon{width:20px;display:inline-block}
.size{color:#7D8590;text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
.time{color:#7D8590;white-space:nowrap;font-size:13px}
.card{background:#161B22;border:1px solid #30363D;border-radius:8px;
      padding:14px;margin-bottom:12px}
.bar{height:8px;background:#21262D;border-radius:4px;overflow:hidden;margin:8px 0 4px}
.bar i{display:block;height:100%;background:#3FB950}
.bar.warn i{background:#D29922}
.bar.bad i{background:#F85149}
input[type=file]{color:#E6EDF3;font-size:14px}
button{background:#238636;color:#fff;border:0;border-radius:6px;
       padding:8px 16px;font-size:14px;cursor:pointer}
button:hover{background:#2EA043}
.empty{color:#7D8590;padding:24px;text-align:center}
.pick{width:30px;text-align:center}
.actions{margin:12px 0;color:#E6EDF3;font-size:14px}
.actions input[type=text]{background:#0D1117;border:1px solid #30363D;
    color:#E6EDF3;border-radius:6px;padding:7px 9px;font-size:14px}
.hint{color:#7D8590;font-size:12px;margin-left:8px}
.foot{color:#484F58;font-size:12px;margin-top:20px;text-align:center}
"""


def page(title, body):
    return (
        "<!DOCTYPE html><html lang=zh><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>%s</title><style>%s</style></head><body>"
        "<div class=wrap>%s<div class=foot>行空板 USB 网盘</div></div>"
        "</body></html>" % (html.escape(title), CSS, body)
    )


def render_root_list(roots, port):
    rows = []
    for name, path in roots:
        du = disk_usage(path)
        if du:
            total, used, free = du
            pct = used * 100.0 / total if total else 0
            cls = "" if pct < 75 else ("warn" if pct < 90 else "bad")
            bar = (
                "<div class=bar><i class='%s' style='width:%.1f%%'></i></div>"
                "<div class=sub style='margin:0'>%s 可用 / 共 %s</div>"
                % (cls, pct, human_size(free), human_size(total))
            )
        else:
            bar = ""
        rows.append(
            "<div class=card><h1><a href='/b?r=%s&p='>%s</a></h1>"
            "<div class=sub>%s</div>%s</div>"
            % (urllib.parse.quote(path), html.escape(name), html.escape(path), bar)
        )
    body = (
        "<h1>选择存储</h1>"
        "<div class=sub>通过 USB 链路 10.1.2.3:%d 或局域网访问</div>%s"
        % (port, "".join(rows))
    )
    return page("USB 网盘", body)


def render_browse(root, rel, entries, du):
    root_name = root
    for name, path in build_roots():
        if path == root:
            root_name = name
            break

    # 面包屑
    crumbs = ["<a href='/b?r=%s&p='>%s</a>" % (urllib.parse.quote(root),
                                               html.escape(root_name))]
    acc = []
    for part in [p for p in rel.split("/") if p]:
        acc.append(part)
        sub = "/".join(acc)
        crumbs.append(
            "<a href='/b?r=%s&p=%s'>%s</a>"
            % (urllib.parse.quote(root), urllib.parse.quote(sub), html.escape(part))
        )
    crumb_html = " / ".join(crumbs)

    # 列表（带勾选框，供复制用）
    if not entries:
        rows = "<tr><td colspan=4 class=empty>这个目录是空的</td></tr>"
    else:
        rows = []
        for name, is_dir, size, mtime in entries:
            full = posixpath.join(rel, name) if rel else name
            q = urllib.parse.quote(full)
            icon = "📁" if is_dir else "📄"
            if is_dir:
                link = "/b?r=%s&p=%s" % (urllib.parse.quote(root), q)
                shown_size = "—"
            else:
                link = "/d?r=%s&p=%s" % (urllib.parse.quote(root), q)
                shown_size = human_size(size)
            rows.append(
                "<tr>"
                "<td class=pick><input type=checkbox name=f value=\"%s\"></td>"
                "<td><span class=icon>%s</span><a href='%s'>%s</a></td>"
                "<td class=size>%s</td><td class=time>%s</td></tr>"
                % (html.escape(name, quote=True), icon, link, html.escape(name),
                   shown_size, time.strftime("%m-%d %H:%M",
                                             time.localtime(mtime)))
            )
        rows = "".join(rows)

    # 剩余空间
    if du:
        total, used, free = du
        pct = used * 100.0 / total if total else 0
        cls = "" if pct < 75 else ("warn" if pct < 90 else "bad")
        disk_html = (
            "<div class=card><div class=sub style='margin:0 0 4px'>"
            "%s 可用 / 共 %s</div>"
            "<div class=bar><i class='%s' style='width:%.1f%%'></i></div></div>"
            % (human_size(free), human_size(total), cls, pct)
        )
    else:
        disk_html = ""

    upload = (
        "<div class=card><form method=post action='/u?r=%s&p=%s' "
        "enctype=multipart/form-data>"
        "<input type=file name=f multiple> "
        "<button type=submit>上传到当前目录</button></form></div>"
        % (urllib.parse.quote(root), urllib.parse.quote(rel))
    )

    copy_form = (
        "<form method=post action='/cp?r=%s&p=%s'>"
        "<table><tr><th class=pick></th><th>名称</th>"
        "<th style='text-align:right'>大小</th><th>修改时间</th></tr>"
        "%s</table>"
        "<div class=actions>复制勾选项到 "
        "<input type=text name=dest size=16 placeholder='如 backup/0701'> "
        "<button type=submit>复制</button>"
        "<span class=hint>留空=复制到当前目录（重名自动加序号，不覆盖）</span>"
        "</div></form>"
        % (urllib.parse.quote(root), urllib.parse.quote(rel), rows)
    )

    body = (
        "<h1>%s</h1><div class=crumb>%s</div>%s%s%s"
        % (html.escape(root_name), crumb_html, disk_html, copy_form, upload)
    )
    return page(root_name, body)


# ---------------------------------------------------------------- 请求处理

class Handler(BaseHTTPRequestHandler):
    server_version = "M10NetDisk/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def _cookie_token(self):
        for piece in self.headers.get("Cookie", "").split(";"):
            piece = piece.strip()
            if piece.startswith("k="):
                return piece[2:]
        return ""

    def _authed(self):
        """验口令。放行返回 True，否则回 401 并返回 False。"""
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        from_query = qs.get("k", [""])[0]
        supplied = from_query or self._cookie_token()
        if supplied and supplied == TOKEN:
            if from_query:
                self._set_cookie = True       # 之后浏览器自动带着，不用每次加 ?k=
            return True
        self.send_response(401)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        body = page(
            "需要口令",
            "<h1>需要访问口令</h1><div class=card>"
            "在地址后面加上 <code>?k=你的口令</code> 就能进来，之后浏览器会记住。"
            "<br><br>口令在板子的 <code>/root/M10_NetDisk/token.txt</code> 里。"
            "</div>",
        ).encode("utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return False

    def _send_html(self, body, code=200):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        if getattr(self, "_set_cookie", False):
            self.send_header("Set-Cookie", "k=%s; Path=/; Max-Age=604800" % TOKEN)
        self.end_headers()
        self.wfile.write(data)

    def _error(self, code, msg):
        self._send_html(page("出错了",
                             "<h1>%d</h1><div class=card>%s</div>"
                             "<p><a href='/'>返回首页</a></p>"
                             % (code, html.escape(msg))), code)

    def _query(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        root = qs.get("r", [""])[0]
        rel = qs.get("p", [""])[0]
        return parsed.path, root, rel

    def do_GET(self):
        self._set_cookie = False
        if not self._authed():
            return
        path, root, rel = self._query()

        if path == "/" or path == "/index.html":
            self._send_html(render_root_list(build_roots(), self.server.server_port))
            return

        if path == "/b":
            root = resolve_root(root)
            if root is None:
                self._error(403, "这个位置不在可访问范围内")
                return
            target = safe_join(root, rel)
            if target is None or not os.path.isdir(target):
                self._error(404, "目录不存在")
                return
            entries = []
            try:
                for name in os.listdir(target):
                    if name.startswith("."):
                        continue
                    full = os.path.join(target, name)
                    try:
                        st = os.stat(full)
                        entries.append((name, os.path.isdir(full),
                                        st.st_size, st.st_mtime))
                    except OSError:
                        continue
            except OSError as exc:
                self._error(403, "读不了这个目录：%s" % exc)
                return
            # 目录优先，然后按名字
            entries.sort(key=lambda e: (not e[1], e[0].lower()))
            self._send_html(render_browse(root, rel, entries, disk_usage(target)))
            return

        if path == "/d":
            root = resolve_root(root)
            if root is None:
                self._error(403, "这个位置不在可访问范围内")
                return
            target = safe_join(root, rel)
            if target is None or not os.path.isfile(target):
                self._error(404, "文件不存在")
                return
            self._send_file(target, os.path.basename(target))
            return

        self._error(404, "没有这个页面")

    def _send_file(self, path, name):
        try:
            size = os.path.getsize(path)
            fh = open(path, "rb")
        except OSError as exc:
            self._error(403, "打不开：%s" % exc)
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header(
            "Content-Disposition",
            "attachment; filename*=UTF-8''%s" % urllib.parse.quote(name))
        self.end_headers()
        try:
            with fh:
                while True:
                    chunk = fh.read(CHUNK)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass          # 客户端取消下载，正常

    def do_POST(self):
        self._set_cookie = False
        if not self._authed():
            return
        path, root, rel = self._query()

        # 和 GET 一样必须校验 root —— 这里也是从 URL 来的
        root = resolve_root(root)
        if root is None:
            self._error(403, "这个位置不在可访问范围内")
            return

        if path == "/u":
            self._handle_upload(root, rel)
        elif path == "/cp":
            self._handle_copy(root, rel)
        else:
            self._error(404, "没有这个页面")

    def _redirect_back(self, root, rel):
        self.send_response(303)
        self.send_header(
            "Location",
            "/b?r=%s&p=%s" % (urllib.parse.quote(root), urllib.parse.quote(rel)))
        self.end_headers()

    def _handle_upload(self, root, rel):
        target_dir = safe_join(root, rel)
        if target_dir is None or not os.path.isdir(target_dir):
            self._error(400, "目标目录不存在")
            return

        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ctype:
            self._error(400, "需要 multipart/form-data")
            return

        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_UPLOAD:
            self._error(413, "文件太大（上限 %s）" % human_size(MAX_UPLOAD))
            return

        # 手写 multipart 解析：cgi 模块在 3.7 能用但已标记废弃，
        # 自己拆，免得以后换系统版本踩坑。
        boundary = None
        for piece in ctype.split(";"):
            piece = piece.strip()
            if piece.startswith("boundary="):
                boundary = piece[9:].strip('"')
        if not boundary:
            self._error(400, "缺少 boundary")
            return

        body = self.rfile.read(length)
        self._parse_and_save(body, boundary.encode(), target_dir)
        self._redirect_back(root, rel)

    def _handle_copy(self, root, rel):
        """把勾选的条目复制到同一个 root 下的另一个位置。"""
        length = int(self.headers.get("Content-Length") or 0)
        if length > 256 * 1024:
            self._error(413, "表单太大")
            return
        form = urllib.parse.parse_qs(
            self.rfile.read(length).decode("utf-8", "replace"))
        names = form.get("f", [])
        dest = (form.get("dest") or [""])[0].strip()

        src_dir = safe_join(root, rel)
        dst_dir = safe_join(root, dest)
        if src_dir is None or not os.path.isdir(src_dir):
            self._error(400, "源目录不存在")
            return
        if dst_dir is None:
            self._error(400, "目标路径不合法")
            return
        if not names:
            self._error(400, "没有勾选任何条目")
            return
        if os.path.realpath(src_dir) == os.path.realpath(dst_dir):
            self._error(400, "源目录和目标目录是同一个")
            return

        if not os.path.isdir(dst_dir):
            try:
                os.makedirs(dst_dir)
            except OSError as exc:
                self._error(400, "建不了目标目录：%s" % exc)
                return

        for raw in names:
            name = os.path.basename(raw.replace("\\", "/"))
            if not name or name.startswith("."):
                continue
            src = os.path.join(src_dir, name)
            if not os.path.exists(src):
                continue
            dst = unique_path(os.path.join(dst_dir, name))
            try:
                if os.path.isdir(src):
                    shutil.copytree(src, dst)
                else:
                    shutil.copy2(src, dst)
            except (OSError, shutil.Error) as exc:
                sys.stderr.write("复制失败 %s: %s\n" % (src, exc))

        self._redirect_back(root, rel)

    def _parse_and_save(self, body, boundary, target_dir):
        """把 multipart 里的文件逐个落盘，返回保存成功的文件名列表。"""
        saved = []
        delim = b"--" + boundary
        for part in body.split(delim):
            if not part.strip() or part.strip() == b"--":
                continue
            head, _, data = part.partition(b"\r\n\r\n")
            if not _:
                continue
            data = data.rstrip(b"\r\n")          # 去掉尾部的 CRLF

            filename = None
            for line in head.split(b"\r\n"):
                if line.lower().startswith(b"content-disposition:"):
                    for item in line.decode("utf-8", "replace").split(";"):
                        item = item.strip()
                        if item.startswith("filename="):
                            filename = item[9:].strip('"')
            if not filename:
                continue

            # 只取基名，防止写到别处
            filename = os.path.basename(filename.replace("\\", "/"))
            if not filename or filename.startswith("."):
                continue

            dest = os.path.join(target_dir, filename)
            try:
                with open(dest, "wb") as fh:
                    fh.write(data)
                saved.append(filename)
            except OSError as exc:
                sys.stderr.write("写入失败 %s: %s\n" % (dest, exc))
        return saved


# ---------------------------------------------------------------- 入口

def load_or_create_token():
    """读 token.txt，没有就生成一个。"""
    try:
        with open(TOKEN_FILE, "r") as fh:
            tok = fh.read().strip()
        if tok:
            return tok
    except IOError:
        pass
    # 去掉了容易看错的 0/O/1/l
    alphabet = "abcdefghijkmnpqrstuvwxyz23456789"
    tok = "".join(random.choice(alphabet) for _ in range(12))
    try:
        with open(TOKEN_FILE, "w") as fh:
            fh.write(tok + "\n")
        os.chmod(TOKEN_FILE, 0o600)
    except OSError as exc:
        sys.stderr.write("写 token.txt 失败：%s\n" % exc)
    return tok


def main():
    global TOKEN
    try:
        sys.stdout.reconfigure(errors="replace")
        sys.stderr.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

    args = sys.argv[1:]
    port = PORT
    want_list = False
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--list":
            want_list = True
        elif a == "--root":
            i += 1
            if i >= len(args):
                print("--root 后面要跟一个目录路径")
                return 1
            p = os.path.abspath(args[i])
            if not os.path.isdir(p):
                print("目录不存在：%s" % p)
                return 1
            EXTRA_ROOTS.append((os.path.basename(p) or p, p))
        else:
            try:
                port = int(a)
            except ValueError:
                print(__doc__)
                return 1
        i += 1

    TOKEN = load_or_create_token()
    roots = build_roots()

    if want_list:
        if not roots:
            print("没有找到可共享的存储。插上 U 盘，或用 --root 指定目录。")
            return 1
        for name, path in roots:
            du = disk_usage(path)
            extra = ""
            if du:
                extra = "  可用 %s / 共 %s" % (human_size(du[2]), human_size(du[0]))
            print("%-24s %s%s" % (name, path, extra))
        return 0

    if not roots:
        print("没有找到可共享的存储。")
        print("  插上 U 盘后再启动，或者用 --root /某个目录 显式指定。")
        return 1

    print("可访问的存储：")
    for name, path in roots:
        print("  %-24s %s" % (name, path))

    # 把本机的可用地址都列出来，省得用户猜
    ips = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None,
                                       socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except socket.error:
        pass
    # USB 链路的固定地址
    if "10.1.2.3" not in ips:
        ips.insert(0, "10.1.2.3")

    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)

    print("\n访问口令：%s" % TOKEN)
    print("（已存进 %s，下次启动沿用）" % TOKEN_FILE)
    print("\n用浏览器打开（第一次要带口令）：")
    for ip in ips:
        print("  http://%s:%d/?k=%s" % (ip, port, TOKEN))
    if ips:
        print("\n浏览器记住之后，以后直接开 http://%s:%d/ 就行" % (ips[0], port))
    print("\nCtrl-C 停止")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
