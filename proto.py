"""mserver 的通信协议:帧编解码 + 带 socket 的连接对象。

帧 = JSON(UTF-8) + b'\x04' 结尾,两个方向共用:
    c->s  {"command": "set_music", "bag": {"name": "雨爱"}}
    s->c  {"title": "musicchange", "bag": {"now": "雨爱"}}

一次 recv 可能只读到半帧,也可能一次读到好几帧,所以解码统一走
Proto.feed():它只吐完整的帧,残片留在缓冲里等下一次。
"""
import json
import socket
import threading

DELIM = b'\x04'
ENCODING = 'utf-8'
MAX_FRAME = 4 << 20     # 残帧超过这个长度就判坏:对端一直不吐分隔符时别把内存吃满

_UNSET = object()


class ProtoError(Exception):
    """帧不合规矩。"""


class Closed(ProtoError):
    """对端把连接关了。"""


class Msg:
    """一帧解出来的东西:name 是命令名或推送标题,bag 是参数。

    legacy=True 表示对端发的是老的纯文本指令(方便 telnet 调试),
    这时 bag 里只有 name 一个键。
    """

    __slots__ = ('name', 'bag', 'legacy')

    def __init__(self, name, bag=None, legacy=False):
        self.name = '' if name is None else str(name)
        self.bag = bag if isinstance(bag, dict) else {}
        self.legacy = bool(legacy)

    def get(self, key, default=None):
        return self.bag.get(key, default)

    def __getitem__(self, key):
        return self.bag[key]

    def __contains__(self, key):
        return key in self.bag

    def __eq__(self, other):
        return (isinstance(other, Msg) and self.name == other.name
                and self.bag == other.bag and self.legacy == other.legacy)

    def __repr__(self):
        return 'Msg(%r, %r%s)' % (self.name, self.bag, ', legacy' if self.legacy else '')


class Proto:
    """纯编解码:组帧 + 增量分帧。队列/线程里递帧也可以只用这里的静态方法。"""

    def __init__(self, encoding=ENCODING):
        self.encoding = encoding
        self.buf = b''
        self.bad = 0        # 被丢掉的坏帧数,排查时看它

    # ---------------- 组帧 ----------------
    @staticmethod
    def frame(name, bag=None, key='command'):
        return json.dumps({key: str(name), 'bag': dict(bag or {})},
                          ensure_ascii=False).encode(ENCODING) + DELIM

    @classmethod
    def command(cls, name, bag=None):
        """c->s 方向。"""
        return cls.frame(name, bag, 'command')

    @classmethod
    def push(cls, title, bag=None):
        """s->c 方向。"""
        return cls.frame(title, bag, 'title')

    @classmethod
    def parse_line(cls, line, commands=None):
        """一行 "set_music 雨爱" -> (command, bag);空行返回 None。

        commands 是服务端 get_command_list 给的表,没有就按单参数兜底。
        """
        line = str(line or '').strip()
        if not line:
            return None
        cmd, _, arg = line.partition(' ')
        cmd, rest = cmd.strip(), arg.strip()
        bag = {}
        args = (commands or {}).get(cmd)
        if args is None:
            if rest:
                bag['name'] = rest
        elif len(args) == 1:
            if rest:
                bag[args[0]] = rest
        elif args:
            for name, val in zip(args, rest.split(None, len(args) - 1)):
                bag[name] = val
        return cmd, bag

    # ---------------- 解帧 ----------------
    def decode(self, line):
        """一帧 -> Msg。JSON 但不像话的抛 ProtoError;纯文本按老指令兜底。"""
        if isinstance(line, (bytes, bytearray)):
            try:
                text = bytes(line).decode(self.encoding)
            except UnicodeDecodeError:
                return self._legacy(bytes(line).decode(self.encoding, 'replace'))
        else:
            text = str(line)
        text = text.strip()
        if not text:
            raise ProtoError('空帧')
        try:
            obj = json.loads(text)
        except ValueError:
            return self._legacy(text)
        if not isinstance(obj, dict):
            raise ProtoError('帧不是 JSON 对象: %s' % text[:80])
        if 'title' in obj:
            name = obj.get('title')
        else:
            name = obj.get('command', obj.get('comand'))
        bag = obj.get('bag')
        return Msg(name, bag if isinstance(bag, dict) else {})

    def _legacy(self, text):
        cmd, _, arg = text.partition(' ')
        arg = arg.strip()
        return Msg(cmd.strip(), {'name': arg} if arg else {}, legacy=True)

    def feed(self, chunk):
        """喂一块刚收到的字节,返回这次凑齐的所有 Msg(0~n 条)。

        坏帧只记数不抛异常:一条烂消息不该把整条连接弄死。
        """
        if chunk:
            self.buf += bytes(chunk)
        out = []
        while True:
            i = self.buf.find(DELIM)
            if i < 0:
                break
            line, self.buf = self.buf[:i], self.buf[i + 1:]
            if not line.strip():
                continue
            try:
                out.append(self.decode(line))
            except ProtoError:
                self.bad += 1
        if len(self.buf) > MAX_FRAME:
            self.buf = b''
            self.bad += 1
        return out

    def reset(self):
        self.buf = b''


class Conn(Proto):
    """一条连接:socket + 收缓冲 + 发送锁。

    recv():读满一帧返回 Msg,超时返回 None,对端关闭抛 Closed。
    timeout=None 表示死等;默认 1 秒轮询,方便外层检查停止标志。
    注意:settimeout 是 socket 级的,和读线程共用同一个 socket 的发送方
    也会继承这个超时(和原来 a.py 里的做法一致)。
    """

    def __init__(self, sock, timeout=1.0, encoding=ENCODING):
        super().__init__(encoding)
        self.sock = sock
        self.timeout = timeout
        self.closed = False
        self._queue = []
        self._lock = threading.Lock()

    # ---------------- 发 ----------------
    def send_bytes(self, data):
        """data 必须是完整帧(Proto.command / Proto.push 的返回值)。"""
        if self.closed:
            raise Closed('连接已关闭')
        with self._lock:
            try:
                self.sock.sendall(data)
            except OSError as e:
                self.closed = True
                raise Closed(str(e)) from e

    def send_command(self, name, bag=None):
        self.send_bytes(self.command(name, bag))

    def send_push(self, title, bag=None):
        self.send_bytes(self.push(title, bag))

    # ---------------- 收 ----------------
    def recv(self, timeout=_UNSET):
        while True:
            if self._queue:
                return self._queue.pop(0)
            if self.closed:
                raise Closed('连接已关闭')
            t = self.timeout if timeout is _UNSET else timeout
            try:
                if self.sock.gettimeout() != t:
                    self.sock.settimeout(t)
                chunk = self.sock.recv(65536)
            except socket.timeout:
                return None
            except OSError as e:
                self.closed = True
                raise Closed(str(e)) from e
            if not chunk:
                self.closed = True
                raise Closed('对端关闭')
            self._queue.extend(self.feed(chunk))

    def messages(self, stop=None):
        """一直收,直到对端关闭或 stop(threading.Event)被置位。"""
        while not self.closed and not (stop is not None and stop.is_set()):
            try:
                m = self.recv()
            except Closed:
                return
            if m is not None:
                yield m

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.sock.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def __repr__(self):
        return 'Conn(%r, timeout=%r)' % (self.sock, self.timeout)
