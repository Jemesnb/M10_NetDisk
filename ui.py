#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""USB 网盘 —— 行空板上的控制界面。

在板子的 240x320 屏幕上显示服务状态、访问口令、各类地址和 U 盘信息，
用 A/B 键启停服务和翻页。不接电脑也能看状态、读口令。

关于按键（踩过的坑）：
    板载 A/B 键**不是**通过读 GPIO 工作的，而是被 X 服务器映射成键盘
    事件 'a' / 'b'。所以必须用 unihiker GUI 库的 on_a_click() /
    on_b_click() 回调，用 pinpong 去轮询 /sys/class/gpio/gpio219/value
    是永远读不到变化的（实测值一直是 1，按下也不变）。
    GUI 类在构造时会自己起一个后台线程跑 Tk mainloop，
    所以 bind 上去的回调会被正常触发，不需要手动泵事件。

用法（在板子上）：
    DISPLAY=:0 XAUTHORITY=/var/run/lightdm/root/:0 python3 ui.py
    python3 ui.py --page log        # 直接开在日志页（调试排版用）
"""

import os
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "server.py")
TOKEN_FILE = os.path.join(HERE, "token.txt")
LOG_FILE = os.path.join(HERE, "netdisk.log")
PORT = 8090

# 配色和上位机网页保持一致
BG = "#0E1116"
FG = "#E6EDF3"
DIM = "#7D8590"
TRACK = "#21262D"
OK = "#3FB950"
BAD = "#F85149"
ACCENT = "#58A6FF"

W, H = 240, 320


# ---------------------------------------------------------------- 服务管理

class Server(object):
    """管着 server.py 子进程和它的日志。"""

    def __init__(self):
        self.proc = None
        self.log_fh = None

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self):
        if self.running():
            return
        try:
            self.log_fh = open(LOG_FILE, "a")
            self.log_fh.write("\n===== %s 启动 =====\n"
                              % time.strftime("%m-%d %H:%M:%S"))
            self.log_fh.flush()
            self.proc = subprocess.Popen(
                [sys.executable, "-u", SERVER, str(PORT)],
                cwd=HERE, stdout=self.log_fh,
                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        except Exception as exc:
            self._append("启动失败：%r" % (exc,))

    def stop(self):
        if self.running():
            try:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=2)
            except Exception as exc:
                self._append("停止失败：%r" % (exc,))
            self._append("===== %s 已停止 =====" % time.strftime("%m-%d %H:%M:%S"))
        self.proc = None

    def _append(self, line):
        try:
            with open(LOG_FILE, "a") as fh:
                fh.write(line + "\n")
        except IOError:
            pass

    def log_tail(self, lines):
        try:
            with open(LOG_FILE, "r", errors="replace") as fh:
                data = fh.read()
        except IOError:
            return ["（还没有日志）"]
        rows = [r.rstrip() for r in data.splitlines() if r.strip()]
        return rows[-lines:] if rows else ["（还没有日志）"]

    def cleanup(self):
        self.stop()
        if self.log_fh:
            try:
                self.log_fh.close()
            except IOError:
                pass


# ---------------------------------------------------------------- 信息读取

def read_token():
    try:
        with open(TOKEN_FILE, "r") as fh:
            return fh.read().strip() or "（未生成）"
    except IOError:
        return "（未生成）"


def local_ips():
    """返回 (局域网地址, USB链路地址)。

    局域网地址从网卡上读；USB 链路的 10.1.2.3 是固定值，单独留着，
    因为它不走 WiFi，是插上数据线就能用的那条路。
    """
    lan = None
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None,
                                       socket.AF_INET):
            ip = info[4][0]
            if ip.startswith("127.") or ip == "10.1.2.3":
                continue
            lan = ip
            break
    except socket.error:
        pass
    if lan is None:
        # getaddrinfo 有时候给不出，退回用 UDP 探一下出口地址
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            lan = s.getsockname()[0]
            s.close()
        except socket.error:
            lan = None
    return lan, "10.1.2.3"


def usb_info():
    """返回 (标签, 描述)。没有 U 盘时描述说明原因。"""
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    try:
        import server as srv
    except Exception:
        return None, "加载 server.py 失败"

    mounts = srv.detect_usb_mounts()
    if not mounts:
        return None, "没有检测到 U 盘"

    dev, mnt = mounts[0]
    du = srv.disk_usage(mnt)
    try:
        count = len([n for n in os.listdir(mnt) if not n.startswith(".")])
    except OSError:
        count = -1

    label = "U盘 " + os.path.basename(mnt)
    free = srv.human_size(du[2]) if du else "?"
    text = "%s 可用" % free
    if count >= 0:
        text += " · %d 个条目" % count
    return label, text


# ---------------------------------------------------------------- 界面

PAGE_STATUS = 0
PAGE_LOG = 1
LOG_LINES = 8


class Panel(object):

    def __init__(self):
        from unihiker import GUI

        self.g = GUI(w=W, h=H)
        self.g.canvas.config(bg=BG)
        g = self.g

        # 按键走 GUI 库的事件回调（X11 键盘事件），不是 GPIO 轮询。
        # 回调在 Tk 的后台线程里跑，所以只往队列里塞标记，实际操作交给主循环。
        self.pending = []
        g.on_a_click(lambda: self.pending.append("a"))
        g.on_b_click(lambda: self.pending.append("b"))

        # 标题 + 分隔线（两页共用）
        self.title = g.draw_text(x=10, y=8, text="USB 网盘", color=FG,
                                 font_size=16, origin="nw")
        g.draw_line(x0=10, y0=34, x1=230, y1=34, color=TRACK, width=1)

        # 退出按钮。屏幕是触摸的，点它就能退。
        # 必须留这个口子：窗口是全屏的，会把板子的 Home 菜单盖住，
        # 没有出口的话用户只能去杀进程。
        self.quit_requested = False
        self.exit_btn = g.draw_text(x=W - 26, y=6, text="✕", color=DIM,
                                    font_size=18, origin="nw",
                                    onclick=self._request_quit)
        # 顺带把 Home 键也绑上，多一条退路（有些环境会给这个键）
        try:
            g.master.bind("<Home>", lambda _e: self._request_quit())
        except Exception:
            pass

        # ---- 状态页 ----
        self.status = g.draw_text(x=10, y=42, text="", color=DIM,
                                  font_size=14, origin="nw")
        self.lbl_tok = g.draw_text(x=10, y=74, text="访问口令", color=DIM,
                                   font_size=11, origin="nw")
        self.token = g.draw_text(x=10, y=90, text="", color=ACCENT,
                                 font_size=17, origin="nw")
        self.lbl_url = g.draw_text(x=10, y=126, text="浏览器打开", color=DIM,
                                   font_size=11, origin="nw")
        self.url_lan = g.draw_text(x=10, y=142, text="", color=FG,
                                   font_size=13, origin="nw")
        self.url_usb = g.draw_text(x=10, y=164, text="", color=DIM,
                                   font_size=11, origin="nw")
        self.usb = g.draw_text(x=10, y=192, text="", color=FG,
                               font_size=12, origin="nw")
        self.usb_sub = g.draw_text(x=10, y=212, text="", color=DIM,
                                   font_size=11, origin="nw")

        # ---- 日志页 ----
        # 行距 20px：10 号字渲染高度约 20px（26 号字实测占 51px，按比例推的）
        self.log_rows = []
        y = 42
        for _ in range(LOG_LINES):
            self.log_rows.append(
                g.draw_text(x=8, y=y, text="", color=DIM,
                            font_size=10, origin="nw"))
            y += 20

        # ---- 底部提示（两页共用）----
        g.draw_line(x0=10, y0=266, x1=230, y1=266, color=TRACK, width=1)
        self.hint1 = g.draw_text(x=10, y=274, text="", color=DIM,
                                 font_size=11, origin="nw")
        self.hint2 = g.draw_text(x=10, y=294, text="", color=DIM,
                                 font_size=11, origin="nw")

        self.page = PAGE_STATUS

    # ---- 按键 ----

    def _request_quit(self):
        """从 Tk 线程里调过来的，只设标记，真正退出交给主循环。"""
        self.quit_requested = True

    def poll_buttons(self):
        """返回 'a' / 'b' / None。"""
        if self.pending:
            return self.pending.pop(0)
        return None

    # ---- 渲染 ----

    def _clear_status(self):
        for elem in (self.status, self.lbl_tok, self.token, self.lbl_url,
                     self.url_lan, self.url_usb, self.usb, self.usb_sub):
            elem.config(text="")

    def show_status(self, srv):
        self.page = PAGE_STATUS
        self.title.config(text="USB 网盘")

        on = srv.running()
        self.status.config(text="● 运行中" if on else "● 已停止",
                           color=OK if on else BAD)

        self.lbl_tok.config(text="访问口令")
        self.token.config(text=read_token())
        self.lbl_url.config(text="浏览器打开")

        lan, usb_ip = local_ips()
        if lan:
            self.url_lan.config(text="%s:%d" % (lan, PORT), color=FG)
        else:
            self.url_lan.config(text="（无局域网地址）", color=DIM)
        self.url_usb.config(text="%s:%d  (USB 直连)" % (usb_ip, PORT))

        label, desc = usb_info()
        if label:
            self.usb.config(text=label, color=FG)
            self.usb_sub.config(text=desc, color=DIM)
        else:
            self.usb.config(text="无 U 盘", color=BAD)
            self.usb_sub.config(text=desc, color=DIM)

        for row in self.log_rows:
            row.config(text="")

        self.hint1.config(text="A键 " + ("停止服务" if on else "启动服务"))
        self.hint2.config(text="B键 查看日志")

    def show_log(self, srv):
        self.page = PAGE_LOG
        self.title.config(text="服务日志")
        self._clear_status()

        rows = srv.log_tail(LOG_LINES)
        for i, row in enumerate(self.log_rows):
            if i < len(rows):
                text = rows[i]
                if len(text) > 34:
                    text = text[:33] + "…"
                row.config(text=text,
                           color=FG if "启动" in text or "停止" in text else DIM)
            else:
                row.config(text="")

        self.hint1.config(
            text="A键 " + ("停止服务" if srv.running() else "启动服务"))
        self.hint2.config(text="B键 返回状态")

    def refresh(self, srv):
        if self.page == PAGE_STATUS:
            self.show_status(srv)
        else:
            self.show_log(srv)


# ---------------------------------------------------------------- 主循环

def main():
    try:
        sys.stdout.reconfigure(errors="replace")
        sys.stderr.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

    # --page log 直接开在日志页，方便截图核对排版
    start_page = PAGE_STATUS
    if "--page" in sys.argv:
        idx = sys.argv.index("--page")
        if idx + 1 < len(sys.argv) and sys.argv[idx + 1] == "log":
            start_page = PAGE_LOG

    panel = Panel()
    srv = Server()

    try:
        # 启动时服务没在跑就自动拉起来，省得每次手动点
        srv.start()
        time.sleep(0.4)

        if start_page == PAGE_LOG:
            panel.show_log(srv)
        else:
            panel.show_status(srv)

        tick = 0
        while True:
            # 点了右上角 ✕ 就退出。退出时会把网盘服务一起停掉
            # （见 finally），不留孤儿进程。
            if panel.quit_requested:
                break
            action = panel.poll_buttons()
            if action == "a":
                if srv.running():
                    srv.stop()
                else:
                    srv.start()
                time.sleep(0.3)
                panel.show_status(srv)
                tick = 0
            elif action == "b":
                if panel.page == PAGE_STATUS:
                    panel.show_log(srv)
                else:
                    panel.show_status(srv)
                tick = 0

            # 每 1 秒刷一次：够用，也不至于让屏幕闪
            tick += 1
            if tick >= 5:
                tick = 0
                panel.refresh(srv)
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        srv.cleanup()
        print("界面已退出")


if __name__ == "__main__":
    sys.exit(main())
