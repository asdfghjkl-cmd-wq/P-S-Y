
from base64 import b64encode
import io
import queue
from random import randint
import socket
from time import sleep
import os,json,threading,platform,logging
import traceback
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
    def set_music_file(self,name,file):
        self.player.set_mrl(file)
        self.load_message(name,file)
    def play(self):
        self.player.play()
    def pause(self):
        self.player.pause()
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

class server:
    def __init__(self):
        self.music_list = music_load()
        self.music_list.load(MUSIC_DIR)
        self.rp = None
        ny = 1
        while True:
            for _ in range(200):
                n = randint(1024,65535)
                if check_port('127.0.0.1',port=n) :
                    self.rp = n
                    break
            if not self.rp:print(f'refind {ny}')
            elif ny == 10:break
            else:break
            ny+=1
        
        self.a = socket.socket(socket.AF_INET,socket.SOCK_STREAM)
        self.a.bind(('127.0.0.1',self.rp))
        self.a.listen(10)

        self.stopevent = threading.Event()
        self.runthread = None
        self.recv_quene = queue.Queue(10)
        self.send_quene_list = {}
        self.dc = {}
        self.t = None
        self.c = {'pid':os.getpid(),'port':self.rp}
        json.dump(self.c,open(os.path.join(BASE_DIR,'run.pid'),'w',encoding='utf-8'))
        
    def recv_socket(self, sock, conn, event, address):
        buf = b''
        try:
            with sock:
                while not event.is_set():
                    try:
                        sock.settimeout(1)
                        chunk = sock.recv(4096)
                    except socket.timeout:
                        continue
                    except OSError:
                        return
                    if not chunk:
                        return
                    buf += chunk
                    while b'\x04' in buf:          # 一次取出所有完整指令
                        line, buf = buf.split(b'\x04', 1)
                        if line:
                            conn.put({'data': line, 'from': address})
        finally:
            self.dc.pop(address, None)    
    def send_socket(self,sock:socket.socket,conn:queue.Queue,event:threading.Event):
        
        with sock:
            while True:
                if event.is_set():
                    sock.shutdown(socket.SHUT_WR)
                    break
                try:x= conn.get(timeout=5)
                except queue.Empty:continue
                try:
                    sock.sendall(x)
                except OSError:
                    return
    def u_socket(self):
        while True:
            if self.stopevent.is_set():
                break
            try:
                self.a.settimeout(5)
                a,b = self.a.accept()
            except socket.timeout:
                continue
            n = threading.Event()
            c = queue.Queue(10)
            y = threading.Thread(target=self.recv_socket,args=(a,self.recv_quene,n,b),daemon=True)
            z = threading.Thread(target=self.send_socket,args=(a,c,n),daemon=True)
            self.dc[b] = {'rthread':y,"stop":n,'send':c,'sthread':z}
            z.start()
            y.start()
    def notice(self, mea, title, bag):
        data = json.dumps({'title': title, 'bag': bag}).encode('utf-8')
        targets = list(self.dc) if mea == ALL else ([mea] if mea in self.dc else [])
        for addr in targets:
            try:
                self.dc[addr]['send'].put_nowait(data)
            except (KeyError, queue.Full):
                pass
    def em(self):
        a = Player()
        n = []
        while True:
            if self.stopevent.is_set():
                a.release()
                self.notice(ALL,'bye',{'reason':'stop event is set'})
                sleep(1)
                break
            me =self.recv_quene.get()
            mel = me.get('data')
            mea = me.get('from')
            
            try:
                self.notice(n,'progress',{
                        'state': str(a.player.get_state()),
                        'time': p.get_time(),
                        'length': p.get_length(),
    
                        'isplaying':a.player.is_playing()
                    }))  
                    
                if mel == b'play':a.play()
                elif mel ==  b'pause':a.pause()
                elif bytes(mel).startswith(b'set_music '):
                    n = mel.decode().removeprefix('set_music').strip()
                    m = self.music_list.dict.get(n,{}).get('music',NOFILE)
                    if m == NOFILE:self.notice(mea,'error',{'message':"not found music"})
                    else:
                        a.set_music_file(n, m)
                        photo = a.get_picture_base64()          # 先算好，失败也不影响播放
                        self.notice(ALL, 'musicchange',
                                    {'now': n, 'message': a.music_message, 'photo': photo})
                        a.play()
                elif mel == b'get_music_list':
                    self.notice(mea,'pushlist',self.music_list.dict)
                elif mel== b'stop':
                    self.stopevent.set()
                elif mel == b'get_status':
                    p = a.player
                    self.notice(mea, 'status', {
                        'state': str(p.get_state()),
                        'time': p.get_time(),
                        'length': p.get_length(),
                        'photo': a.get_picture_base64(),   # mutagen 读到的标签（值里有 list，能序列化）
                        'message':a.music_message,
                        'isplaying':a.player.is_playing()
                    })
                elif mel == b'subscribe_progress':

            except Exception:
                traceback.print_exc()
                self.notice(mea,'error',{'message':traceback.format_exc()})

    def start(self):
        self.runthread = threading.Thread(target=self.u_socket,daemon=True)
        self.runthread.start()
        self.t=threading.Thread(target=self.em,daemon=True)
        self.t.start()
    def mainloop(self):
        self.stopevent.wait()
    def stop(self):self.stopevent.set()
if __name__ == "__main__":
    s = server()
    s.start()
    try:
        s.mainloop()
        sleep(2)
    except KeyboardInterrupt:
        print(threading.enumerate())
    finally:os.remove(os.path.join(BASE_DIR,'run.pid'))