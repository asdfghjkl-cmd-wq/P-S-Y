"""mserver 的终端客户端。

协议(TCP,帧以 0x04 结尾,负载是 JSON):
  c->s  {"command": "get_version", "bag": {}}
  s->c  {"title": "version", "bag": {"name": "mserver", "version": 0.5, "supports": {...}}}
  c->s  {"command": "set_music", "bag": {"name": "雨爱"}}
"""
import platform
import socket
import os, json, threading, shutil, psutil, sys,subprocess
import proto

D = proto.DELIM
BASE_DIR = os.path.abspath(os.path.dirname(__file__))

# ---------------- 顶部状态行 ----------------
if os.name == 'nt':
    import ctypes
    from ctypes import wintypes as _wt

    _k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    _k32.GetStdHandle.restype = _wt.HANDLE
    _STDOUT = _k32.GetStdHandle(-11)

    class _COORD(ctypes.Structure):
        _fields_ = [('X', _wt.SHORT), ('Y', _wt.SHORT)]

    class _SMALL_RECT(ctypes.Structure):
        _fields_ = [('Left', _wt.SHORT), ('Top', _wt.SHORT),
                    ('Right', _wt.SHORT), ('Bottom', _wt.SHORT)]

    class _CHAR_INFO(ctypes.Structure):
        _fields_ = [('UnicodeChar', _wt.WCHAR), ('Attributes', _wt.WORD)]

    class _CSBI(ctypes.Structure):
        _fields_ = [('dwSize', _COORD), ('dwCursorPosition', _COORD),
                    ('wAttributes', _wt.WORD), ('srWindow', _SMALL_RECT),
                    ('dwMaximumWindowSize', _COORD)]

    _k32.GetConsoleScreenBufferInfo.argtypes = [_wt.HANDLE, ctypes.POINTER(_CSBI)]
    _k32.GetConsoleScreenBufferInfo.restype = _wt.BOOL
    _k32.WriteConsoleOutputW.argtypes = [_wt.HANDLE, ctypes.POINTER(_CHAR_INFO),
                                         _COORD, _COORD, ctypes.POINTER(_SMALL_RECT)]
    _k32.WriteConsoleOutputW.restype = _wt.BOOL

    def _write_top_line(text):
        """把 text 写到可见窗口第一行。直接改屏幕缓冲区、不移动光标,
        所以光标停在哪(包括正在输入的提示符)都不会被打断。"""
        sys.stdout.flush()
        info = _CSBI()
        if not _k32.GetConsoleScreenBufferInfo(_STDOUT, ctypes.byref(info)):
            # 没有控制台句柄(输出被重定向/IDE 窗口/pythonw):退回 ANSI
            sys.stdout.write('\0337\033[1;1H\033[2K' + text + '\0338')
            sys.stdout.flush()
            return
        win = info.srWindow
        width = win.Right - win.Left + 1
        if width <= 0:
            return
        body = text[:width].ljust(width)
        cells = (_CHAR_INFO * width)()
        for i, ch in enumerate(body):
            cells[i].UnicodeChar = ch
            cells[i].Attributes = info.wAttributes
        rect = _SMALL_RECT(win.Left, win.Top, win.Right, win.Top)
        _k32.WriteConsoleOutputW(_STDOUT, cells, _COORD(width, 1),
                                 _COORD(0, 0), ctypes.byref(rect))
else:
    def _write_top_line(text):
        """用 DECSC/DECRC(ESC 7 / ESC 8)存取光标,兼容性比 CSI s/u 好。"""
        sys.stdout.write('\0337\033[1;1H\033[2K' + text + '\0338')
        sys.stdout.flush()

# ---------------- 按键表 ----------------
# 统一成:('char', 'a') 或 'enter'/'backspace'/'left'/... 
_PLAIN = {
    '\r': 'enter', '\n': 'enter',
    '\x08': 'backspace', '\x7f': 'backspace',
    '\x03': 'ctrl-c',
    '\x04': 'ctrl-d', '\x1a': 'ctrl-d',
    '\x15': 'ctrl-u', '\x17': 'ctrl-w',
    '\x01': 'ctrl-a', '\x05': 'ctrl-e',
    '\t': 'tab',
}

_WIN_SPECIAL = {
    'H': 'up', 'P': 'down', 'K': 'left', 'M': 'right',
    'G': 'home', 'O': 'end', 'S': 'delete', 'R': 'insert',
    'I': 'pgup', 'Q': 'pgdn',
}

_POSIX_SEQ = {
    '[A': 'up', '[B': 'down', '[C': 'right', '[D': 'left',
    '[H': 'home', '[F': 'end', 'OH': 'home', 'OF': 'end',
    'OA': 'up', 'OB': 'down', 'OC': 'right', 'OD': 'left',
    '[1~': 'home', '[4~': 'end', '[7~': 'home', '[8~': 'end',
    '[3~': 'delete', '[2~': 'insert', '[5~': 'pgup', '[6~': 'pgdn',
}


def _read_key_win():
    import msvcrt
    ch = msvcrt.getwch()
    if ch in ('\x00', '\xe0'):
        return _WIN_SPECIAL.get(msvcrt.getwch(), '')
    if ch in _PLAIN:
        return _PLAIN[ch]
    if ch >= ' ':
        return ('char', ch)
    return ''


def _decode_posix_char(fd, first):
    """把首字节补成完整 UTF-8 字符(中文是 3 字节)。"""
    b0 = first[0]
    if b0 < 0x80:
        return first.decode('latin1')
    if b0 >> 5 == 0b110:
        need = 2
    elif b0 >> 4 == 0b1110:
        need = 3
    elif b0 >> 3 == 0b11110:
        need = 4
    else:
        need = 1
    buf = first
    while len(buf) < need:
        more = os.read(fd, 1)
        if not more:
            break
        buf += more
    return buf.decode('utf-8', 'replace')


def _posix_seq_to_key(seq):
    return _POSIX_SEQ.get(seq, '')


def _read_key_posix(fd):
    b = os.read(fd, 1)
    if not b:
        return 'ctrl-d'
    if b == b'\x1b':
        seq = ''
        while True:
            nb = os.read(fd, 1)
            if not nb:
                return ''
            seq += nb.decode('latin1')
            if seq[-1].isalpha() or seq[-1] == '~':
                break
        return _posix_seq_to_key(seq)
    ch = _decode_posix_char(fd, b)
    if ch in _PLAIN:
        return _PLAIN[ch]
    if ch >= ' ':
        return ('char', ch)
    return ''


# ---------------- 行编辑器 ----------------
class _Editor:
    """维护一行缓冲 + 光标位置 + 历史,负责把这一行画出来。"""

    def __init__(self, cil):
        self.cil = cil
        self.buf = ''
        self.pos = 0
        self.history = []
        self.hist_idx = None
        self.stash = ''

    def render(self):
        c = self.cil
        sys.stdout.write('\r\033[2K' + c.word + self.buf)
        back = len(self.buf) - self.pos
        if back > 0:
            sys.stdout.write('\033[%dD' % back)
        sys.stdout.flush()

    def _set(self, buf, pos):
        self.buf = buf
        self.pos = max(0, min(len(buf), pos))
        self.render()

    def _history_move(self, delta):
        if not self.history:
            return
        if self.hist_idx is None:
            if delta > 0:
                return
            self.stash = self.buf
            self.hist_idx = len(self.history)
        idx = self.hist_idx + delta
        if idx < 0:
            idx = 0
        if idx >= len(self.history):
            self.hist_idx = None
            self._set(self.stash, len(self.stash))
            return
        self.hist_idx = idx
        self._set(self.history[idx], len(self.history[idx]))

    def handle(self, key):
        """返回 None 表示继续编辑;('submit', line) / ('eof', None) / ('abort', None)。"""
        if key == 'enter':
            line = self.buf
            self.buf, self.pos, self.hist_idx = '', 0, None
            if line.strip():
                self.history.append(line)
                if len(self.history) > 200:
                    del self.history[0]
            return ('submit', line)
        if key == 'eof':
            return ('eof', None)
        if key == 'ctrl-c':
            return ('abort', None)
        if key == 'backspace':
            if self.pos > 0:
                self._set(self.buf[:self.pos - 1] + self.buf[self.pos:], self.pos - 1)
        elif key == 'delete':
            if self.pos < len(self.buf):
                self._set(self.buf[:self.pos] + self.buf[self.pos + 1:], self.pos)
        elif key == 'left':
            if self.pos > 0:
                self._set(self.buf, self.pos - 1)
        elif key == 'right':
            if self.pos < len(self.buf):
                self._set(self.buf, self.pos + 1)
        elif key in ('home', 'ctrl-a'):
            self._set(self.buf, 0)
        elif key in ('end', 'ctrl-e'):
            self._set(self.buf, len(self.buf))
        elif key == 'ctrl-u':
            self._set('', 0)
        elif key == 'ctrl-w':
            head = self.buf[:self.pos]
            i = len(head.rstrip())
            j = i
            while j > 0 and not head[j - 1].isspace():
                j -= 1
            self._set(head[:j] + self.buf[self.pos:], j)
        elif key == 'up':
            self._history_move(-1)
        elif key == 'down':
            self._history_move(1)
        elif isinstance(key, tuple) and key and key[0] == 'char':
            ch = key[1]
            self._set(self.buf[:self.pos] + ch + self.buf[self.pos:], self.pos + 1)
        return None


# ---------------- 输出封装 ----------------
class cil:
    def __init__(self, word, char_mode=None) -> None:
        self.word = word
        self.lock = threading.Lock()
        self.prompt_shown = False
        self.pending = []
        self.editor = _Editor(self)
        # 真终端里自己收键:这样"正在输入"和"只是闲着"能区分开。
        # 重定向/管道下退回 input(),走 pending 队列。
        if char_mode is None:
            char_mode = (sys.stdin.isatty() and sys.stdout.isatty())
        self.char_mode = bool(char_mode)
        print('\033[?25l', end='')
        x = shutil.get_terminal_size()
        print('\n' * int(x[1] / 2), end='')
        print('loading...'.center(x[0] - 10))
        print('\n' * int(x[1] / 2), end='')
        self.error_a = False

    def top_print(self, message):
        """顶部状态行:写终端第 1 行,写完还原光标,不影响下面滚动的消息区。"""
        x = shutil.get_terminal_size()
        width = max(0, x[0] - 10)
        text = str(message).replace('\r', ' ').replace('\n', ' ')
        if len(text) > width:
            text = text[:width]
        with self.lock:
            _write_top_line(text.center(width))

    def print(self, data):
        """持久消息:打字中也不丢输入——消息插到上方,再把提示符和已输入内容重画。"""
        with self.lock:
            if self.prompt_shown and self.char_mode:
                sys.stdout.write('\r\033[2K' + str(data) + '\n')
                self.editor.render()
                return
            if self.prompt_shown:
                self.pending.append(str(data))
                return
            sys.stdout.write(str(data) + '\n')
            sys.stdout.flush()

    def _flush_pending(self):
        """把输入期间积压的消息补显出来(调用方必须持有 self.lock)。"""
        for item in self.pending:
            sys.stdout.write(item + '\n')
        self.pending.clear()

    def input(self):
        if self.char_mode:
            return self._input_char()
        with self.lock:
            sys.stdout.write('\r\033[2K')
            self._flush_pending()
            sys.stdout.write(self.word)
            sys.stdout.flush()
            self.prompt_shown = True
        try:
            return input()
        finally:
            with self.lock:
                sys.stdout.write('\r\033[2K')
                self.prompt_shown = False
                self._flush_pending()
                sys.stdout.flush()

    def _read_key(self):
        if os.name == 'nt':
            return _read_key_win()
        return _read_key_posix(sys.stdin.fileno())

    def _input_char(self):
        """自己收键:支持方向键/Home/End/Delete/历史,且推送不会冲掉输入。"""
        fd = old = None
        if os.name != 'nt':
            import termios, tty
            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            tty.setcbreak(fd)
        with self.lock:
            sys.stdout.write('\r\033[2K')
            self._flush_pending()
            self.editor.render()
            self.prompt_shown = True
        try:
            while True:
                key = self._read_key()
                with self.lock:
                    if not key:
                        continue
                    res = self.editor.handle(key)
                    if res is None:
                        continue
                    kind, line = res
                    if kind == 'submit':
                        sys.stdout.write('\n')
                        sys.stdout.flush()
                        return line
                    if kind == 'eof':
                        raise EOFError
                    if kind == 'abort':
                        raise KeyboardInterrupt
        finally:
            with self.lock:
                self.prompt_shown = False
            if old is not None and os.name != 'nt':
                termios.tcsetattr(fd, termios.TCSADRAIN, old)

    def clear_error(self):
        self.error_a = False

    def load_ok(self):
        print("\033[1J", end='')
        print('\033[?25h', end='')

    def error(self, message,a=['press any key to exit']):
        self.error_a = True
        print("\033[1J", end='')
        print('\033[?25l', end='')
        x = shutil.get_terminal_size()
        fuc = 2
        if len(a)>x[0]/8:fuc =4
        print('\n' * int(x[1] / fuc), end='')
        print('\x1b[31m',end='')
        n = 'error'.center(x[0] - 10)
        m = 'message:%s' % message
        print(n)
        print(m.center(x[0] - 10) if len(m) < x[0] - 10 else m)
        for t in a:
            print(t.center(x[0] - 10))
        print('\n' * int(x[1] / 2 - 1-len(a)), end='')
        print('\033[39m', end='')
        try:
            return sys.stdin.read(1)
        except Exception:
            return '\n'
            


# ---------------- 协议 ----------------
def _mmss(ms):
    s = int(ms or 0) // 1000
    return '%02d:%02d' % (s // 60, s % 60)


def send_msg(conn, command, bag=None):
    """发一帧命令;conn 是 proto.Conn。"""
    conn.send_command(command, bag)


def build_command(line, commands):
    """按服务器给的命令表把 "set_music 雨爱" 变成 {"command":..., "bag":{...}}。

    拆参数的规则在 proto.Proto.parse_line,这里只保留老接口。
    """
    r = proto.Proto.parse_line(line, commands)
    if r is None:
        return None
    return {'command': r[0], 'bag': r[1]}


def _print_commands(c, meta):
    commands = meta.get('commands') or {}
    if not commands:
        c.print('命令表还没到(等服务端 get_command_list 回包)')
        return
    parts = []
    for name in sorted(commands):
        args = commands[name]
        parts.append(name + (' <%s>' % '> <'.join(args) if args else ''))
    c.print('commands: ' + ', '.join(parts))


def in_a_send(conn, c, e, meta):
    with conn:
        while True:
            try:
                line = c.input()
            except (EOFError, KeyboardInterrupt):
                e.set()
                break
            low = line.strip().lower()
            if low in ('quit', 'exit', 'q'):
                e.set()
                try:
                    conn.sock.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
                os._exit(0)
            if low in ('help', '?', 'commands'):
                _print_commands(c, meta)
                continue
            msg = build_command(line, meta.get('commands') or {})
            if msg is None:
                continue
            try:
                send_msg(conn, msg['command'], msg['bag'])
            except proto.Closed:
                e.set()
                break


def recv_a_print(conn, c, a, meta):
    while not a.is_set():
        try:
            m = conn.recv()
        except proto.Closed:
            return
        if m is None:                 # 1 秒没消息,回去看停止标志
            continue
        title, bag = m.name or 'unknown', m.bag
        if title == 'bye':
            a.set()
            conn.close()
            os._exit(0)
        if title == 'version':
            meta['version'] = bag
            c.print('server: %s v%s  supports=%s' % (
                bag.get('name'), bag.get('version'),
                json.dumps(bag.get('supports'), ensure_ascii=False)))
        elif title == 'command_list':
            meta['commands'] = {k: list(val) for k, val in bag.items()}
            c.print('命令表: %d 条(help 查看)' % len(meta['commands']))
        elif title == 'musicchange':
            c.print('musicchange:%s' % json.dumps(
                {'now': bag.get('now'), 'message': bag.get('message')},
                ensure_ascii=False))
        elif title == 'progress':
            now = max(0, bag.get('time') or 0)
            total = max(0, bag.get('length') or 0)
            avail = max(20, shutil.get_terminal_size()[0] - 10)
            bw = max(10, min(50, avail - 34))
            n = int(now / total * bw) if total else 0
            n = max(0, min(bw, n))
            bar = '#' * n + '-' * (bw - n)
            state = str(bag.get('state', '')).replace('State.', '')
            fu = 'playing' if bag.get('isplaying') else 'paused'
            c.top_print('s:%s [%s] %s/%s %s' % (state, bar, _mmss(now), _mmss(total), fu))
        elif title == 'status':
            bag.pop('photo', None)
            c.print('status:%s' % json.dumps(bag, ensure_ascii=False))
        else:
            c.print('%s:%s' % (title, json.dumps(bag, ensure_ascii=False)))


if __name__ == '__main__':
    y = cil('>')
    meta = {'commands': {}, 'version': {}}
    x = threading.Event()
    pid_path = os.path.join(BASE_DIR, 'run.pid')
    if not os.path.isfile(pid_path):
        for a in range(10):
            if not os.path.isfile(pid_path):
                c = y.error('run.pid 不存在,服务器没启动',['press any key to exit','press "r" key to retaste','press "s" key to start server',f'no.{a}'])
                if c == 'r':continue
                
                else:        
                    sys.exit(255)
            else:break
    with open(pid_path, 'r', encoding='utf-8') as f:
        info = json.load(f)
    if not psutil.pid_exists(info.get('pid', -1)):


                    y.error('服务器进程不在(pid %s)' % info.get('pid'))
                    
                          
                    sys.exit(255)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.connect(('127.0.0.1', info.get('port', 0)))
        except OSError as exc:
            y.error(str(exc))
            sys.exit(255)
        y.load_ok()
        p = proto.Conn(s, timeout=1.0)      # 协议收发都走它
        # 握手:版本 + 命令表
        send_msg(p, 'get_version')
        send_msg(p, 'get_command_list')
        a = threading.Thread(target=in_a_send, args=(p, y, x, meta), daemon=True)
        b = threading.Thread(target=recv_a_print, args=(p, y, x, meta), daemon=True)
        a.start()
        b.start()
        a.join()
        b.join()
