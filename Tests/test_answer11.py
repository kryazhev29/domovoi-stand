"""Offline wire compatibility: never opens a COM port or sends a board command."""
import contextlib, ctypes, io, runpy, struct, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from protocol import frame, take_frames, parse_answer11

class Kernel:
    def __init__(self, old):
        self.old=old; self.pending=bytearray(); self.commands=[]; self.closed=False
        self.CreateFileW=lambda *args: 123
    def SetCommTimeouts(self,*args): return 1
    def CloseHandle(self,*args): self.closed=True; return 1
    def WriteFile(self,h,data,n,written,overlapped):
        written._obj.value=n
        (uid,cmd,payload),=take_frames(bytes(data[:n]))[0]; self.commands.append(cmd)
        if cmd==102: self.pending+=frame(102,b'\1',uid)
        elif cmd==19:
            body=struct.pack('<BIH',0,0x78563412,2048)
            if self.old:body=bytes([cmd])+body
            self.pending+=frame(11,body,(uid+1)&255) # unrelated sequence
            broken=bytearray(frame(11,body,uid));broken[-1]^=1;self.pending+=broken
            self.pending+=frame(11,body,uid)
        elif cmd==20:
            block=struct.unpack_from('<H',payload)[0]
            assert len(payload)==1026
            self.pending+=frame(4,bytes((50 if block==0 else 100,0)),uid)
        return 1
    def ReadFile(self,h,buf,n,received,overlapped):
        data=bytes(self.pending[:7]);del self.pending[:7]
        ctypes.memmove(buf,data,len(data));received._obj.value=len(data);return 1

class Answer11(unittest.TestCase):
    def test_formats(self):
        for code in (0,1,2,4,5,254,255):
            body=struct.pack('<BIH',code,0x78563412,2048)
            for payload in (body,b'\x15'+body):
                self.assertEqual(parse_answer11(payload,21),(code,0x78563412,2048))
        for payload in (b'',b'\x15\0',bytes(6),bytes(9)):
            with self.assertRaises(ValueError):parse_answer11(payload,21)
        with self.assertRaises(ValueError):parse_answer11(b'\x13'+bytes(7),21)
    def test_updater_old_and_new(self):
        for old in (True,False):
            with self.subTest(old=old),tempfile.TemporaryDirectory(prefix='domovoi-answer11-') as temp:
                image=Path(temp)/'139.71.1792045742.bin';image.write_bytes(bytes(1500))
                fake=Kernel(old);ticks=iter(i*.01 for i in range(10000));output=io.StringIO()
                with patch('ctypes.WinDLL',return_value=fake),patch('time.sleep'),patch('time.monotonic',side_effect=lambda:next(ticks)),patch.object(sys,'argv',['fw_usb_update.py',str(image),'MOCK']),contextlib.redirect_stdout(output):
                    runpy.run_path(str(ROOT/'fw_usb_update.py'),run_name='__main__')
                self.assertEqual(fake.commands,[102,108,19,20,20]);self.assertTrue(fake.closed)
                self.assertIn('block 2/2 100%',output.getvalue())
                self.assertIn('done:',output.getvalue())

if __name__=='__main__':unittest.main()
