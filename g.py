"""mserver 的图形客户端:界面照 b.py 的样子做 —— ttkbootstrap + solarized-light。

    .venv\\Scripts\\python.exe g.py

a.py 不在运行时会先问一句"要不要拉起来",起来了再连。协议细节在 proto.py,
本文件只负责界面:列表/封面/标签来自 get_music_list、musicchange,
进度来自 subscribe_progress(服务端每 0.5s 推一次),进度条/音量走
set_time / set_volume。
"""
import base64
import io
import json
import logging
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import tkinter

import psutil
import ttkbootstrap
from PIL import Image, ImageTk
from ttkbootstrap.dialogs import Messagebox

import proto

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PID_PATH = os.path.join(BASE_DIR, 'run.pid')

DEFAULT_THEME = 'solarized-light'

_DEFAULT_PHOTO = None
_DEFAULT_TRIED = False


def default_photo():
    """a.png 的惰性单例,口径和 a.py/b.py 一致:拿不到就返回 None,绝不抛。"""
    global _DEFAULT_PHOTO, _DEFAULT_TRIED
    if _DEFAULT_TRIED:
        return _DEFAULT_PHOTO
    _DEFAULT_TRIED = True
    try:
        img = Image.open(os.path.join(BASE_DIR, 'a.png'))
        img.load()
        _DEFAULT_PHOTO = img
    except Exception:
        logging.exception('打开默认封面 a.png 失败,本次按无封面处理')
        _DEFAULT_PHOTO = None
    return _DEFAULT_PHOTO


def fmt_time(ms):
    s = max(int(ms or 0), 0) // 1000
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return '%d:%02d:%02d' % (h, m, s) if h else '%02d:%02d' % (m, s)


def _tag_text(v):
    """标签值可能是列表(mutagen 的 easy 标签),压成一行;空值按 b.py 的口径写 unknow。"""
    if isinstance(v, (list, tuple)):
        v = [str(x) for x in v if x]
        return ','.join(v) or 'unknow'
    return str(v) if v else 'unknow'


class SeekBar(ttkbootstrap.Frame):
    """进度条:和 b.py 的同名类一样是 Scale + 时间标签,拖完才提交。

    区别只有一个:数据不再从 vlc 里轮询,而是由服务端推过来的进度喂 set_times()。
    """
    SCALE_MAX = 1000

    def __init__(self, master, on_seek=None, **kw):
        super().__init__(master, **kw)
        self.on_seek = on_seek
        self._dragging = False
        self._total = 0

        self.scale = ttkbootstrap.Scale(self, from_=0, to=self.SCALE_MAX, orient='horizontal')
        self.scale.pack(side='left', fill='x', expand=True)
        self.label = ttkbootstrap.Label(self, text='00:00 / 00:00', width=13, anchor='e')
        self.label.pack(side='right', padx=(6, 0))

        self.scale.bind('<ButtonPress-1>', self._on_press)
        self.scale.bind('<ButtonRelease-1>', self._on_release)
        self.scale.configure(command=self._on_scale_move)

    def _on_press(self, _event):
        self._dragging = True

    def _on_release(self, _event):
        self._dragging = False
        if self._total > 0 and self.on_seek is not None:
            self.on_seek(int(self._total * self.scale.get() / self.SCALE_MAX))

    def _on_scale_move(self, value):
        if not self._dragging or self._total <= 0:
            return
        cur = int(self._total * float(value) / self.SCALE_MAX)
        self.label.config(text='%s / %s' % (fmt_time(cur), fmt_time(self._total)))

    def set_times(self, cur, total):
        self._total = total
        if self._dragging:
            return
        if total > 0:
            self.scale.set(max(0, min(self.SCALE_MAX, cur / total * self.SCALE_MAX)))
            self.label.config(text='%s / %s' % (fmt_time(cur), fmt_time(total)))
        else:
            self.label.config(text='00:00 / 00:00')


class App:
    def __init__(self):
        self.music_dict = {}          # 名字 -> {'music':..., 'video':...},和 b.py 同构
        self.music_message = {}
        self.index = None
        self._cover_src = None
        self._cover_size = -1
        self.music_keep_not_clean = None
        self._resize_after = None

        self.q = queue.Queue()
        self.stop_evt = threading.Event()
        self.conn = None
        self.alive = True
        self.cmds = {}
        self.names = []
        self.cur_name = None
        self.playing = False
        self.length = 0
        self._vol_after = None
        self._pump_after = None

        self.app = ttkbootstrap.Tk('mserver')
        self.app.protocol('WM_DELETE_WINDOW', self.on_closing)
        self.app.title('mserver')
        self.app.geometry('800x600+20+60')
        self.app.minsize(460, 360)
        self.app.rowconfigure(0, weight=10)
        self.app.rowconfigure(1, weight=1)
        self.app.rowconfigure(2, weight=10)
        self.app.columnconfigure(0, weight=3)
        self.app.columnconfigure(1, weight=1)

        self.style = ttkbootstrap.Style()
        names = self.style.theme_names()
        self.theme = DEFAULT_THEME if DEFAULT_THEME in names else 'litera'
        self.style.theme_use(self.theme)

        try:
            src = default_photo()
            self._tk_image_obj = ImageTk.PhotoImage(src.resize((16, 16))) if src else None
            if self._tk_image_obj is not None:
                self.app.iconphoto(True, self._tk_image_obj) # type: ignore
        except Exception:
            logging.exception('设置窗口图标失败(忽略)')

        self.information_frame = ttkbootstrap.Frame(self.app)
        self.information_frame.grid(row=0, column=0, sticky='nsew')

        self.Control_frame = ttkbootstrap.Frame(self.app)
        self.Control_frame.grid(row=0, column=1)

        self.volume_var = tkinter.IntVar(value=100)
        self.music_label = ttkbootstrap.Label(self.Control_frame, text='音量:100')
        self.music_volume = ttkbootstrap.Scale(self.Control_frame, from_=0, to=100, value=100,
                                               variable=self.volume_var, command=self.set_volume)

        self.music_label.pack()
        self.music_volume.pack()
        self.play_status_label = ttkbootstrap.Label(self.Control_frame, text='无')
        self.play_status_label.pack()


        self.pause_button = ttkbootstrap.Button(self.Control_frame, text='播放/暂停', command=self.pause)

        self.pause_button.pack(side='top')

        self.server_label = ttkbootstrap.Label(self.Control_frame, text='未连接',
                                               bootstyle='secondary')
        self.server_label.pack(side='bottom')

        self.Progress_bar = ttkbootstrap.Frame(self.app)
        self.Progress_bar.grid(row=1, column=0, columnspan=2, sticky='ew', padx=8, pady=4)
        self.Progress_bar.columnconfigure([0, 2], weight=1)
        self.Progress_bar.columnconfigure(1, weight=10)

        self.seek = SeekBar(self.Progress_bar, on_seek=self.seek_to)
        self.seek.grid(row=0, column=1, sticky='ew')
        self.music_minus_ten_button = ttkbootstrap.Button(
            self.Progress_bar, text='⏪', command=lambda: self.send('seek_back'))
        self.music_minus_ten_button.grid(row=0, column=0)
        self.music_add_ten_button = ttkbootstrap.Button(
            self.Progress_bar, text='⏩️', command=lambda: self.send('seek_forward'))
        self.music_add_ten_button.grid(row=0, column=2)

        self.information_frame.rowconfigure([0, 1], weight=1)
        self.information_frame.columnconfigure([0, 1], weight=1)

        ph = None
        src = default_photo()
        if src is not None:
            try:
                ph = ImageTk.PhotoImage(src.resize((115, 115)))   # 与 460 的窗口高匹配
            except Exception:
                logging.exception('默认封面缩放失败,本次按无封面处理')
                ph = None
        self.music_image = ttkbootstrap.Label(self.information_frame, image=ph) # type: ignore
        self.music_keep_not_clean = ph
        self.music_name = ttkbootstrap.Label(self.information_frame)
        self.music_maker_name = ttkbootstrap.Label(self.information_frame)

        self.music_image.grid(row=0, column=0, rowspan=2, sticky='nsew')
        self.music_name.grid(row=0, column=1, sticky='nsew')
        self.music_maker_name.grid(row=1, column=1, sticky='nsew')

        self.down_frame = ttkbootstrap.Frame(self.app)
        self.down_frame.rowconfigure(0, weight=1)
        self.down_frame.columnconfigure(0, weight=40)
        self.down_frame.columnconfigure(1, weight=1)
        self.down_frame.grid(row=2, column=0, columnspan=2, sticky='nsew')

        self.scrollbar = ttkbootstrap.Scrollbar(self.down_frame)
        self.down_frame_listbox = ttkbootstrap.Listbox(
            self.down_frame, selectmode=tkinter.SINGLE, exportselection=False,
            yscrollcommand=self.scrollbar.set)
        self.scrollbar.config(command=self.down_frame_listbox.yview)
        self.down_frame_listbox.grid(row=0, column=0, sticky='nsew')
        self.scrollbar.grid(row=0, column=1, sticky='n')

        self.menu = ttkbootstrap.Menu(self.app)
        self.menu.add_command(label='重加载音乐列表', command=self.flush_music)
        self.menu.add_command(label='同步状态', command=lambda: self.send('get_status'))
        self.menu.add_command(label='设置', command=self.setting)
        self.menu.add_separator()
        self.menu.add_command(label='关闭服务端', command=self.stop_server)
        self.app.config(menu=self.menu)

        # 和 b.py 一样的键位:空格播放/暂停,左右 ±10s,上下调音量
        self.app.bind_class('Button', '<space>', self.play_stop)
        self.app.bind('<space>', self.play_stop)
        self.down_frame_listbox.bind('<space>', self.play_stop)
        self.app.bind('<Left>', self.time_minus_ten)
        self.app.bind('<Right>', self.time_add_ten)
        self.app.bind('<Up>', self.volume_up)
        self.app.bind('<Down>', self.volume_down)
        self.app.bind('<Configure>', self.on_resize)
        self.down_frame_listbox.bind('<<ListboxSelect>>', self.change_music)

        if not self._connect():
            self.alive = False
            self.app.destroy()
            return
        self.app.after(80, self._pump)

    # ---------------- 网络 ----------------
    def _read_pid(self):
        try:
            with open(PID_PATH, encoding='utf-8') as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def _live_server(self):
        info = self._read_pid()
        if not isinstance(info, dict):
            return None
        try:
            return info if psutil.pid_exists(int(info.get('pid', -1))) else None
        except (TypeError, ValueError):
            return None

    def _connect(self):
        info = self._live_server()
        if info is None:
            r = Messagebox.yesno('没找到运行中的 a.py,现在启动它吗?', '启动服务端',
                                 parent=self.app, buttons=['启动', '不了'])
            if r != '启动':
                return False
            info = self._start_server()
            if info is None:
                Messagebox.show_warning('服务端没起来,去控制台看 a.py 的输出', '启动失败',
                                        parent=self.app)
                return False
        try:
            sock = socket.create_connection(('127.0.0.1', int(info.get('port', 0))), timeout=5)
        except (OSError, TypeError, ValueError) as e:
            Messagebox.show_warning(str(e), '连接失败', parent=self.app)
            return False
        self.conn = proto.Conn(sock, timeout=1.0)
        threading.Thread(target=self._recv_loop, daemon=True).start()
        for cmd in ('get_version', 'get_command_list', 'get_music_list', 'get_status'):
            self.conn.send_command(cmd)
        self.conn.send_command('subscribe_progress')
        return True

    def _start_server(self):
        """拉起 a.py 并等它写出 run.pid;启动期间转一下 Tk 事件循环,别让窗口白屏。"""
        try:
            flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            subprocess.Popen([sys.executable, os.path.join(BASE_DIR, 'a.py')],
                             cwd=BASE_DIR, creationflags=flags,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            logging.exception('启动 a.py 失败')
            return None
        for _ in range(150):
            info = self._live_server()
            if info is not None:
                return info
            try:
                self.app.update()
            except tkinter.TclError:
                return None
            time.sleep(0.2)
        return None

    def _recv_loop(self):
        try:
            assert self.conn is not None
            for m in self.conn.messages(self.stop_evt):
                self.q.put(m)
        except Exception:
            logging.exception('收消息线程异常')
        finally:
            self.q.put(None)        # 断开信号

    def _pump(self):
        try:
            while True:
                m = self.q.get_nowait()
                if m is None:
                    self._set_server('与服务端的连接已断开', bad=True)
                    self.conn = None
                    return
                self._handle(m)
        except queue.Empty:
            pass
        except tkinter.TclError:
            return
        if self.alive:
            try:
                self._pump_after = self.app.after(80, self._pump)
            except tkinter.TclError:
                self._pump_after = None

    def _handle(self, m):
        t, bag = m.name, m.bag
        if t == 'version':
            self._set_server('%s v%s' % (bag.get('name'), bag.get('version')))
            self.play_status_label.config(text='已连接')
        elif t == 'command_list':
            self.cmds = {k: list(v) for k, v in bag.items()}
        elif t == 'pushlist':
            self._fill_list(bag)
        elif t == 'musicchange':
            self.cur_name = bag.get('now')
            self.music_message = bag.get('message') or {'name': self.cur_name}
            self.playing = True
            self.flush_display()
            self._cover(bag.get('photo'))
            self._select_name(self.cur_name)
            self.play_status_label.config(text='播放中')
        elif t == "volumechange":
            self.set_volume_display(bag.get('volume',100))
        elif t == 'statuschange':
            self.playing = bool(bag.get('is_playing'))
            self.play_status_label.config(text='播放中' if self.playing else '已暂停')
        elif t in ('progress', 'status'):
            self._apply_progress(bag)
            if t == 'status':
                if bag.get('message'):
                    self.music_message = bag['message']
                    self.flush_display()
                self._cover(bag.get('photo'))
        elif t == 'error':
            self._set_server(bag.get('message', 'error'), bad=True)
        elif t == 'bye':
            self._set_server('服务端已退出', bad=True)
            self.play_status_label.config(text='无')

    # ---------------- 数据 -> 界面 ----------------
    def _fill_list(self, bag):
        self.music_dict = dict(bag)
        self.names = sorted(self.music_dict)
        self.down_frame_listbox.delete(0, tkinter.END)
        for name in self.names:
            self.down_frame_listbox.insert(tkinter.END, name)
        if self.cur_name:
            self._select_name(self.cur_name)

    def _select_name(self, name):
        # 程序化 selection_set 不会触发 <<ListboxSelect>>,所以不会弹确认框
        if name not in self.names:
            self.index = None
            return
        self.index = self.names.index(name)
        self.down_frame_listbox.selection_clear(0, tkinter.END)
        self.down_frame_listbox.selection_set(self.index)
        self.down_frame_listbox.see(self.index)

    def _restore_selection(self):
        self.down_frame_listbox.selection_clear(0, tkinter.END)
        if self.index is not None and 0 <= self.index < self.down_frame_listbox.size():
            self.down_frame_listbox.selection_set(self.index)

    def _cover(self, b64):
        img = None
        if b64:
            try:
                img = Image.open(io.BytesIO(base64.b64decode(b64)))
                img.load()
            except Exception:
                logging.exception('解码封面失败,回退默认封面')
                img = None
        if img is None:
            img = default_photo()
        self._cover_src = img
        self._cover_size = -1        # 换了图就必须重画
        self.flush_display()

    def flush_display(self):
        """和 b.py 的同名方法一样:封面按窗口高度缩放,标题/作者取标签。"""
        src = self._cover_src
        if src is not None:
            size = max(self.app.winfo_height() // 4, 32)
            # 只有封面换了或者目标尺寸变了才重建 PhotoImage
            if src is not self._cover_src or size != self._cover_size or self.music_keep_not_clean is None:
                self._cover_src, self._cover_size = src, size
                self.music_keep_not_clean = ImageTk.PhotoImage(src.resize((size, size)))
                self.music_image.config(image=self.music_keep_not_clean)

        msg = self.music_message
        self.music_name.config(text='标题:%s' % _tag_text(msg.get('title') or msg.get('name')))
        self.music_maker_name.config(text='作者:%s' % _tag_text(msg.get('artist')))

    def _state_text(self, bag):
        """VLC 的 isplaying 在 Buffering/Opening 时是 0,光看它会误报"已暂停"。"""
        st = str(bag.get('state') or '').replace('State.', '')
        if st in ('Playing', 'Buffering', 'Opening'):
            return '播放中'
        if st == 'Paused':
            return '已暂停'
        if st in ('Stopped', 'Ended', 'NothingSpecial', 'Error'):
            return '无'
        return '播放中' if bag.get('isplaying') else '已暂停'

    def _apply_progress(self, bag):
        cur = max(0, int(bag.get('time') or 0))
        total = max(0, int(bag.get('length') or 0))
        self.length = total
        text = self._state_text(bag)
        self.playing = text == '播放中'
        self.seek.set_times(cur, total)
        self.play_status_label.config(text=text)

    def _set_server(self, text, bad=False):
        self.server_label.config(text=str(text),
                                 bootstyle='danger' if bad else 'secondary')

    # ---------------- 交互 ----------------
    def send(self, cmd, bag=None):
        if self.conn is None or self.conn.closed:
            self._set_server('未连接服务端', bad=True)
            return False
        try:
            self.conn.send_command(cmd, bag)
            return True
        except proto.Closed as e:
            self._set_server('发送失败:%s' % e, bad=True)
            return False



    def pause(self):
        return self.send('pause')

    def play_stop(self, _event=None):
        
        self.pause()

        return 'break'

    def time_add_ten(self, _event=None):
        self.send('seek_forward')
        return 'break'

    def time_minus_ten(self, _event=None):
        self.send('seek_back')
        return 'break'

    def volume_up(self, _event=None):
        self.music_volume.set(min(100, int(self.music_volume.get()) + 5))
        return 'break'

    def volume_down(self, _event=None):
        self.music_volume.set(max(0, int(self.music_volume.get()) - 5))
        return 'break'
    def set_volume_display(self,a):
        b = round(float(a))
        self.music_label.config(text='音量:%d' % b)
        self.volume_var.set(b)


        
    def set_volume(self, a):
        b = round(float(a))
        self.music_label.config(text='音量:%d' % b)
        if self._vol_after is not None:
            try:
                self.app.after_cancel(self._vol_after)
            except tkinter.TclError:
                pass
        self._vol_after = self.app.after(150, lambda: self._flush_volume(b))

    def _flush_volume(self, v):
        self._vol_after = None
        self.send('set_volume', {'level': v})

    def seek_to(self, ms):
        self.send('set_time', {'time': ms})

    def change_music(self, _event=None):
        sel = self.down_frame_listbox.curselection()
        if not sel:
            return
        idx = int(sel[0])
        a = self.down_frame_listbox.get(idx)
        aaa = Messagebox.yesno('是否切换音乐', 'music player', parent=self.app, buttons=['是', '否'])
        if aaa == '是':
            if self.send('set_music', {'name': a}):
                self.index = idx
        else:
            self._restore_selection()
        self.down_frame_listbox.focus_set()

    def flush_music(self):
        self.send('get_music_list')

    def setting(self):
        a = ttkbootstrap.Toplevel('setting', size=(260, 160))
        v = tkinter.StringVar(value=self.style.theme_use())
        ttkbootstrap.Label(a, text='主题').pack(anchor='w', padx=8, pady=(8, 0))
        cb = ttkbootstrap.Combobox(a, textvariable=v, values=self.style.theme_names(),
                                   state='readonly')
        cb.pack(anchor='w', padx=8, fill='x')

        def save():
            self.style.theme_use(v.get())
            a.destroy()

        ttkbootstrap.Button(a, text='保存', command=save).pack(pady=10)

    def stop_server(self):
        r = Messagebox.yesno('会停掉 a.py(播放服务一起退出),继续?', '关闭服务端',
                             parent=self.app, buttons=['是', '否'])
        if r == '是':
            self.send('stop')

    def on_resize(self, event):
        # 子控件的 <Configure> 也会冒到 toplevel,不判断 event.widget 会疯狂重画封面
        if event.widget is not self.app:
            return
        if self._resize_after is not None:
            try:
                self.app.after_cancel(self._resize_after)
            except tkinter.TclError:
                pass
        self._resize_after = self.app.after(120, self._do_resize)

    def _do_resize(self):
        self._resize_after = None
        self.flush_display()

    def on_closing(self):
        self.alive = False
        self.stop_evt.set()
        # 先把排队的 after 撤掉:destroy 之后再触发就是满屏的 bgerror
        for aid in (self._pump_after, self._resize_after, self._vol_after):
            if aid is None:
                continue
            try:
                self.app.after_cancel(aid)
            except tkinter.TclError:
                pass
        self._pump_after = self._resize_after = self._vol_after = None
        if self.conn is not None:
            try:
                self.conn.send_command('unsubscribe_progress')
            except proto.Closed:
                pass
            self.conn.close()
        self.app.destroy()


def main():
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s')
    app = App()
    if not app.alive:
        return 1
    app.app.mainloop()
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception:
        logging.exception('程序运行期间发生未捕获异常')
        raise
