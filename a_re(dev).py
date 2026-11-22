
from base64 import b64encode
import io
import queue

from random import randint
import socket
from time import sleep
import os,json,threading,platform,logging,sys
import traceback
import warnings

class ProtoError(Exception):pass
start_music_message = True

try:
    from PIL import Image
    import mutagen
    import mutagen.flac
    import mutagen.id3

except ModuleNotFoundError:
    traceback.print_exc()
    logging.warning('mutagen不存在,无法加载音乐信息')
    start_music_message = False

def _close_quietly(obj,what):
    """关掉 mutagen/PIL 对象(Windows 上不关会一直占着文件句柄)。

    关闭失败只记 debug:收尾动作不能把播放流程带挂。
    """
    close = getattr(obj,'close',None)
    if close is None:
        return
    try:
        close()
    except Exception as e:
        logging.debug(f'关闭 {what} 失败(忽略):{e}')
_DEFAULT_PHOTO = None
_DEFAULT_PHOTO_TRIED = False
#2!
def _default_photo():
    """a.png 的模块级惰性单例:只 Image.open 一次,之后复用同一个对象。

    返回 None 表示封面不可用(文件缺失/损坏)。这里绝不抛异常 —— 原来
    load_message 里的裸调用一旦抛出去,set_media_path 就走不到 return True,
    调用方以为失败,表现成"点了不播"。单例也顺手避免了每次切歌都新开一个
    句柄又只关掉上一个的泄漏。
    """
    global _DEFAULT_PHOTO, _DEFAULT_PHOTO_TRIED
    if _DEFAULT_PHOTO_TRIED:
        return _DEFAULT_PHOTO
    _DEFAULT_PHOTO_TRIED = True
    try:
        _DEFAULT_PHOTO = Image.open(os.path.join(BASE_DIR,'a.png')) # type: ignore

        _DEFAULT_PHOTO.load()
    except Exception:
        logging.exception('打开默认封面 a.png 失败,本次按无封面处理')
        _DEFAULT_PHOTO = None
    return _DEFAULT_PHOTO

NOFILE="!@#$%^&*&*()(&%^%$^*)"
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
MUSIC_DIR = os.path.join(BASE_DIR,'music')
ALL = 'all'
DEFAULT_PORT = 33550
DELIM = b'\x04'
ENCODING = 'utf-8'
MAX_FRAME = 4 << 20     # 残帧超过这个长度就判坏:对端一直不吐分隔符时别把内存吃满

# ---------------- 协议 ----------------
# 帧格式:JSON + b'\x04' 结尾
#   c->s  {"command": "set_music", "bag": {"name": "雨爱"}}
#   s->c  {"title": "version", "bag": {"name": "mserver", ...}}
SERVER_NAME = 'mserver'
VERSION = 1
VNAME = 'alpha'
SUPPORTS = {'plugin': False, 'seek': True, 'cover': True, 'progress': True}
plugin_supports = []
# 命令 -> 参数名列表,get_command_list 直接把这张表发给客户端
COMMANDS = {
    'get_version': [],          # 客户端握手:服务器名/版本/能力
    'get_command_list': [],     # 客户端握手:命令表
    'play': [],
    'pause': [],
    'stop': [],
    'set_music': ['name'],
    'seek_forward': [],
    'seek_back': [],
    'set_time': ['time'],       # 毫秒,进度条直接拖到某个位置
    'set_volume': ['level'],    # 0-100
    'get_music_list': [],
    'get_status': [],
    'subscribe_progress': [],
    'unsubscribe_progress': [],
    'get_volume':[],

}
if platform.system() == 'Windows':
    os.environ['path'] += ';'+BASE_DIR
    os.environ['PYTHON_VLC_MODULE_PATH'] = f"{BASE_DIR}/pvlc"
    os.environ['PYTHON_VLC_LIB_PATH'] = os.path.join(BASE_DIR, 'pvlc', 'libvlc.dll')
    os.environ['PYTHON_VLC_MODULE_PATH'] = os.path.join(BASE_DIR, 'pvlc', 'plugins')
import vlc

def check_port(host='127.0.0.1', port=8000):

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            
            return True
        except OSError:
            return False
class Msg:
    """一帧解出来的东西:name 是命令名或推送标题,bag 是参数,path是路径。

    legacy=True 表示对端发的是老的纯文本指令(方便 telnet 调试),
    这时 bag 里只有 name 一个键。
    """

    __slots__ = ('name', 'bag', 'path','legacy')

    def __init__(self, name, bag=None,path='/main', legacy=False):
        self.name = '' if name is None else str(name)
        self.bag = bag if isinstance(bag, dict) else {}
        # path 也必须是字符串:旧版/写错的帧会把 dict 塞进 path,别让它传染下去
        self.path = path if isinstance(path, str) and path else '/main'
        self.legacy = bool(legacy)

    def get(self, key, default=None):
        return self.bag.get(key, default)

    def __getitem__(self, key):
        return self.bag[key]

    def __contains__(self, key):
        return key in self.bag

    def __eq__(self, other):
        return (isinstance(other, Msg) and self.name == other.name and self.path == other.path
                and self.bag == other.bag and self.legacy == other.legacy)

    def __repr__(self):
        return 'Msg(%r, %r, path=%r%s)' % (self.name, self.bag, self.path,
                                           ', legacy' if self.legacy else '')
class music_load:
    def __init__(self):
        self.dict = {}
    def load(self,dir):
        if os.path.isdir(dir):
            for y in os.listdir(dir):
                x = os.path.join(dir,y)

                if os.path.isfile(os.path.join(x,'info.json')):

                    i=open(os.path.join(x,'info.json'),'r',encoding="utf-8")
                    f = json.load(i)
                    i.close()
                    y = f.get("file",NOFILE)
                    if not y == NOFILE:y = os.path.join(x,y)
                    z = f.get('mv',NOFILE)
                    if not z == NOFILE:z = os.path.join(x,z)
                    self.dict[f.get("name",os.path.basename(x))] = {'music':y,"video":z}
class Player:
    def __init__(self):
        params = [
                                "--network-caching=3000",  
                                "--avcodec-hw=any",       
                                "--codec=avcodec",         
                                "--verbose=-1"
                            ]
        self.instance= vlc.Instance(" ".join(params))
        self.player = vlc.MediaPlayer(self.instance)
        self.music_message={}
        self.music_photo = _default_photo()
    def time_add_ten(self) -> None:

        if self.player.get_state() in (vlc.State.Playing,vlc.State.Paused): # type: ignore
            t = self.player.get_time()+10000
            length = self.player.get_length()
            
            if length > 0:
                t = min(t,length)
            self.player.set_time(t)
    def time_minus_ten(self) -> None:
        if self.player.get_state() in (vlc.State.Playing,vlc.State.Paused):self.player.set_time(max(self.player.get_time()-10000,0)) # type: ignore
    def set_music_file(self,name,file):
        self.player.set_mrl(file)
        self.load_message(name,file)
    def play(self):
        self.player.play()
    def pause(self):
        self.player.pause()



    def get_picture_base64(self):
        photo = self.music_photo
        if photo is None:
            return None
        try:
            if isinstance(photo, (bytes, bytearray)):
                return b64encode(photo).decode('ascii')
            buf = io.BytesIO()
            photo.save(buf, format='PNG')
            return b64encode(buf.getvalue()).decode('ascii')
        except Exception:
            logging.exception('封面转 base64 失败')
            return None
    def _set_cover(self,pic_data=None):
            """统一设置封面:pic_data 为 None 时回到 a.png 的惰性单例。
        
            这里绝不抛异常:封面拿不到最多是没图,不能连带把"点了不播"带出来。
            注意不再 close 上一张封面:原来在 load_message 里无条件
            `self.music_photo.close()`,而切换失败时 self.music_photo 又会被赋成
            默认封面单例,下一次就把它关掉了 —— 关掉的是一个全局缓存对象,
            之后再没人能重建它(PIL 的 close 不会抛,所以失败是静默的)。
            旧封面交给 GC 回收即可,显示用的 PhotoImage 由 Tkapp 自己持有。
            """
            if pic_data is None:
                self.music_photo = _default_photo()
                return
            try:

                self.music_photo = pic_data
            except Exception:
                logging.exception('解码内嵌封面失败,回退到默认封面')
                self.music_photo = _default_photo()
    def load_message(self,name:str,path:"str | None"):
            """读标签/封面。**本方法保证不抛**:任何失败都退化成"没有信息+默认封面"。
    
            约束来自调用方:set_media_path 走完这里才会 return True,一旦这里抛出去,
            调用方就以为切歌失败而不播放,用户看到的就是"点了却不播"。
            """
            self.music_message = {'name': name}
            self._set_cover(None)
            try:
                if not path or path == NOFILE:
                    # 没有标签来源:只保留默认封面与空信息,不做无意义的标签读取
                    return
                if not start_music_message:
                    return
                self._read_tags(path)
            except Exception:
                logging.exception('读取标签或封面失败')
                traceback.print_exc()
                try:
                    self.music_message = {'name': name}
                    self._set_cover(None)
                except Exception:
                    logging.exception('重置音乐信息失败(忽略)')
    
    def _read_tags(self,path):
            """真正的标签读取。mutagen 的对象一律 try/finally 显式关闭:
            Windows 上不关就锁着文件,重命名/删除都会失败。
            """
            a = None
            try:
                a = mutagen.File(path,easy=True) # type: ignore
                if a is not None:
                    for aa,b in a.items():
                        self.music_message[aa] = b
            finally:
                # mutagen.File 返回的对象也可能持有文件句柄,别赌它有 __del__ 会关
                _close_quietly(a,'mutagen.File')
            _,b = os.path.splitext(path)
            b:str
            if b.lower() == '.flac':
                audio = None
                try:
                    audio = mutagen.flac.FLAC(path) # pyright: ignore[reportPossiblyUnboundVariable]
                    for pic in audio.pictures:
                        self.music_message['pic_mine'] = pic.mime
                        self._set_cover(pic.data)
                finally:
                    _close_quietly(audio,'mutagen.flac.FLAC')
            elif b.lower() == '.mp3':
                tags = None
                try:
                    tags = mutagen.id3.ID3(path) # pyright: ignore[reportPossiblyUnboundVariable]
                    for pic in tags.getall("APIC"):
                        self.music_message['pic_mine'] = pic.mime
                        self._set_cover(pic.data)
                finally:
                    _close_quietly(tags,'mutagen.id3.ID3')
    def release(self):
        self.player.stop()
        self.player.release()
        self.instance.release()


class Switch:
    """不管错误处理"""
    def __init__(self,enable_path_value) -> None:
        
        class pathisno(FileNotFoundError):pass
        class nobranch(FileNotFoundError):pass
        self.path_is_no = pathisno
        self.no_branch = nobranch
        self.enable_path_value = enable_path_value
        self.ruletable = {'/':{'default':{'pipe':None},'data':{},'type':'branch'}}
    def addbranch(self,path:str,default=None): # type: ignore
        if default is None:default = self.ef
        if path.strip() == '':raise  self.path_is_no()
        
        
        x = path.split('/')
        for d in x:
            if d.strip() == '':x.remove(d)
        temp = self.ruletable['/']['data']
        for i, d in enumerate(x):
            if i == len(x)-1 :
                ax = queue.Queue(20)
                temp[d] = {'type':'branch','default':{'pipe':ax},'data':{}} # pyright: ignore[reportIndexIssue, reportArgumentType]
                return d,ax
            else:
                tempt = temp[d]
                if tempt.get('type') != 'branch':raise self.no_branch()
                temp = tempt['data'] # pyright: ignore[reportIndexIssue]
    
    def addleave(self,path:str,func=None):
        
        if path.strip() == '':raise  self.path_is_no()
        if func is None:func = self.ef
        x = path.split('/')
        for d in x:
            if d.strip() == '':x.remove(d)
        temp = self.ruletable['/']['data']
        ax = queue.Queue(20)
        for i, d in enumerate(x):
            if i == len(x)-1 :
                temp[d] = {'type':'leave','pipe':ax} # pyright: ignore[reportIndexIssue, reportArgumentType]
                return d,ax
            else:
                tempt = temp[d]
                if tempt.get('type') != 'branch':raise self.no_branch()
                temp = tempt['data']

    def get_func(self,path:str):
        x = path.split('/')
        if path.strip() == '':raise  self.path_is_no()
        for d in x:
            if d.strip() == '':x.remove(d)
        temp = self.ruletable['/']['data']
        for i, d in enumerate(x):
            if i == len(x)-1 :
                temp = temp[d]
                n =  temp.get('type')
                if n == 'branch':
                    return temp.get('default').get('pipe'),''
                elif n == 'leave':
                    return temp.get('pipe'),''
                else:return None,''
            else:
                tempt = temp[d]
                if tempt.get('type') != 'branch':
                    if self.enable_path_value:
                        return temp.get('pipe'),'/'.join(x[i:])
                    else:raise self.no_branch()
                temp = tempt['data']
    
    def input(self,path,data):
        n = self.get_func(path)
        n(data) 


class a_cilent:
    def __init__(self,socket:socket.socket,rpipe:queue.Queue,spipe:queue.Queue,address,encoding=ENCODING):
        self.address = address
        self.stopevent = threading.Event(
        )
        self.encoding = encoding
        self.sock =socket
        self.rpipe = rpipe
        self.spipe = spipe
        self.bad = 0
        self.buf = b''
    def _legacy(self, text):
            cmd, _, arg = text.partition(' ')
            arg = arg.strip()
            return Msg(cmd.strip(), {'name': arg} if arg else {}, legacy=True)
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
            if 'path' in obj:
                path = obj.get('path')
            else:
                warnings.warn('old frame',UserWarning)
                path = '/main'
            return Msg(name, bag if isinstance(bag, dict) else {},path)
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
    def listen(self):
        try:
            self.sock.settimeout(5)
            x=self.sock.listen(4096)
        except OSError:return False
        except socket.timeout:return True
        if x == b'':return False
        for f in self.feed(x):self.rpipe.put({'data':f,'from':self.address})
        return True
    def listen_async(self):
        while True:
            if not self.listen():break
            if self.stopevent.is_set():break
        if not getattr(self.sock,'_closed')
    def send_async(self):
        while True:
            if not self.send():return
            if self.stopevent.is_set():return
    def send(self):
        try:self.sock.sendall(self.spipe.get(timeout=3))
        except queue.Empty:return True
        except OSError:return False
        return True
    def start(self):
        threading.Thread(target=self.listen_async,daemon=True).start()
        threading.Thread(target=self.send_async,daemon=True).start()
    

class link_manager:
    def __init__(self):
        self.rp = None
        for n in [DEFAULT_PORT] + [randint(1024, 65535) for _ in range(200)]:
            if check_port('127.0.0.1', port=n):
                self.rp = n
                break
        if not self.rp:
            raise RuntimeError('找不到可用端口')
        self.cilent_list = {}
        self.bi = socket.socket(socket.AF_INET6,socket.SOCK_STREAM)
        self.bi.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        self.bi.bind(('::',self.rp))
        self.bi.listen(5)
        self.__r_pipe =queue.Queue(20)
        self.callback =[]
        self.listen_thread = threading.Thread(target=self,name='listen',daemon=True)
    def add_callback(self,func):
        self.callback.append(func)
    def u_socket(self):
        while True:
            try:
                self.bi.settimeout(5)
                c,a = self.bi.accept()
            except socket.timeout:
                continue
            rpipe = queue.Queue(10)
            x = a_cilent(c,self.__r_pipe,rpipe,a)
            self.cilent_list[a] = x
            for func in self.callback:
                try:
                    func(a,x)
                except Exception:pass
            x.start()
    def start(self):self.listen_thread.start()
            


class music_cilent_abs:
    def __init__(self,link_man:link_manager):
        link_man.add_callback(self.link_man_callback)
        self.mapping_table = {}
    def link_man_callback(self,address,cilent):
        for i in range(0,)


class server:
    def __init__(self):
        self.link_man = link_manager()
        self.musis_cilent_abs = music_cilent_abs(self.link_man)
