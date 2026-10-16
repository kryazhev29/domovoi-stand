"""Run the IAR-built bootloader binary in Unicorn with register models of RCC, GPIO,
SPI1 + W25 external Flash and the internal Flash controller. Never touches hardware."""
import struct, sys
from pathlib import Path
from unicorn import Uc, UcError, UC_ARCH_ARM, UC_MODE_THUMB, UC_MODE_MCLASS, UC_HOOK_MEM_READ, UC_HOOK_MEM_WRITE, UC_HOOK_CODE
from unicorn.arm_const import UC_ARM_REG_SP, UC_ARM_REG_PC, UC_ARM_REG_MSP

R = Path(r"C:\Users\kryaz\Desktop\мк\СофтЭксперт\Для новой системы\Domovoi Next_IAR95\Domovoi Next_IAR95")
BOOT = (R / "Bootloader/build/Release/Exe/DomovoiBoot.bin").read_bytes()
APP = (R / "Release/Exe/DomovoiNext.bin").read_bytes()
APP += b"\xff" * ((-len(APP)) % 4)
APP51 = APP + bytes(4)  # a different image: same code, 4 more bytes
assert len(BOOT) == 32768

def crc32(data):
    c = 0xFFFFFFFF
    for (w,) in struct.iter_unpack("<I", data):
        c ^= w
        for _ in range(32):
            c = ((c << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if c & 0x80000000 else (c << 1) & 0xFFFFFFFF
    return c

def header(magic, hw, version, body, stamp):
    h = bytearray(b"\xff" * 512)
    struct.pack_into("<IBHBIIII", h, 0, magic, hw, version, 0 if magic == 0x4E465744 else 0xFF, len(body), crc32(body), 0 if magic == 0x4E465744 else 0xFFFFFFFF, stamp)
    if magic == 0x4E465744:
        h[24:32] = bytes(8)
    return bytes(h)

LEGACY, NEW = 0x57464C53, 0x4E465744
JOURNAL, BACKUP = 0x20000, 0x21000

class Board:
    def __init__(self):
        self.flash = bytearray(b"\xff" * 0x20000)
        self.flash[:32768] = BOOT
        self.ext = bytearray(b"\xff" * 0x41000)

    def boot(self, limit_s=600):
        u = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
        u.mem_map(0x08000000, 0x20000); u.mem_write(0x08000000, bytes(self.flash))
        u.mem_map(0x20000000, 0x5000)
        u.mem_map(0x40000000, 0x30000)
        u.mem_map(0xE0000000, 0x100000)
        u.mem_map(0x1FFFF000, 0x1000); u.mem_write(0x1FFFF7E0, struct.pack("<H", 128))
        st = dict(sel=False, buf=[], wel=False, resp=0xFF, ticks_reads=0, erases=0, programs=0, app=None)
        ext = self.ext
        TICKS = 0x20000FB0

        def rd(uc, access, addr, size, value, _):
            if addr == 0x40021000: uc.mem_write(addr, struct.pack("<I", struct.unpack("<I", uc.mem_read(addr, 4))[0] | 0x02020002))
            elif addr == 0x40021004:
                v = struct.unpack("<I", uc.mem_read(addr, 4))[0]; uc.mem_write(addr, struct.pack("<I", (v & ~0xC) | ((v & 3) << 2)))
            elif addr == 0x40013008: uc.mem_write(addr, struct.pack("<I", 3))
            elif addr == 0x4001300C: uc.mem_write(addr, struct.pack("<I", st["resp"]))
            elif addr == 0x40010808: uc.mem_write(addr, struct.pack("<I", 1 << 13))   # PA13 high: button released
            elif addr == 0x4002200C: uc.mem_write(addr, struct.pack("<I", 0))          # FLASH_SR: never busy
            elif addr == TICKS:
                t = struct.unpack("<I", uc.mem_read(TICKS, 4))[0]; uc.mem_write(TICKS, struct.pack("<I", (t + 1) & 0xFFFFFFFF))

        def spi(b):
            s = st["buf"]; s.append(b); cmd = s[0]; n = len(s); r = 0xFF
            if cmd == 5 and n >= 2: r = 0x02 if st["wel"] else 0x00
            elif cmd == 3 and n >= 5: a = (s[1] << 16 | s[2] << 8 | s[3]) + n - 5; r = ext[a] if a < len(ext) else 0xFF
            elif cmd == 2 and n >= 5:
                a = (s[1] << 16 | s[2] << 8 | s[3]) + n - 5; ext[a] &= b; st["programs"] += 1
            st["resp"] = r

        def deselect():
            s = st["buf"]
            if s:
                if s[0] == 6: st["wel"] = True
                elif s[0] == 0x20 and len(s) >= 4 and st["wel"]:
                    a = s[1] << 16 | s[2] << 8 | s[3]; ext[a:a + 4096] = b"\xff" * 4096; st["wel"] = False; st["erases"] += 1
                elif s[0] == 2: st["wel"] = False
            st["buf"] = []

        def wr(uc, access, addr, size, value, _):
            if addr == 0x4001300C: spi(value & 0xFF)
            elif addr == 0x40010C14 and value & (1 << 12): st["buf"] = []; st["sel"] = True
            elif addr == 0x40010C10 and value & (1 << 12): deselect(); st["sel"] = False
            elif addr == 0x40022010 and (value & 0x42) == 0x42:                        # PER|STRT
                page = struct.unpack("<I", uc.mem_read(0x40022014, 4))[0]
                assert 0x08008000 <= page < 0x08020000 and not page & 1023, hex(page)
                uc.mem_write(page, b"\xff" * 1024)
            elif 0x08000000 <= addr < 0x08008000:
                raise AssertionError(f"write into bootloader area 0x{addr:08x}")

        def app_code(uc, addr, size, _):
            st["app"] = addr; uc.emu_stop()

        u.hook_add(UC_HOOK_MEM_READ, rd, begin=0x20000FB0, end=0x20000FB3)
        u.hook_add(UC_HOOK_MEM_READ, rd, begin=0x40000000, end=0x4002FFFF)
        u.hook_add(UC_HOOK_MEM_WRITE, wr, begin=0x40000000, end=0x4002FFFF)
        u.hook_add(UC_HOOK_MEM_WRITE, wr, begin=0x08000000, end=0x08007FFF)
        u.hook_add(UC_HOOK_CODE, app_code, begin=0x08008000, end=0x0801FFFF)
        sp, pc = struct.unpack_from("<II", BOOT)
        u.reg_write(UC_ARM_REG_SP, sp)
        try:
            u.emu_start(pc | 1, 0x0801FFFF, timeout=limit_s * 1000000)
        except UcError as e:
            raise AssertionError(f"emulation fault {e} at pc=0x{u.reg_read(UC_ARM_REG_PC):08x}")
        self.flash[:] = u.mem_read(0x08000000, 0x20000)
        vtor = struct.unpack("<I", u.mem_read(0xE000ED08, 4))[0]
        msp = u.reg_read(UC_ARM_REG_MSP)
        return st, vtor, msp

    def word(self, off):
        return struct.unpack_from("<I", self.ext, JOURNAL + off)[0]

    def app_version(self):
        magic, hw, ver = struct.unpack_from("<IBH", self.flash, 0x8000)
        return magic, hw, ver

def expect_start(board, st, vtor, msp):
    sp, pc = struct.unpack_from("<II", board.flash, 0x8200)
    assert st["app"] == pc & ~1, (hex(st["app"] or 0), hex(pc))
    assert vtor == 0x08008200, hex(vtor)
    assert msp == sp, (hex(msp), hex(sp))

stamp = 1789400000
# 1. Plain start: the installed legacy FW 50 (as the old loader stores it), empty external Flash.
b = Board()
b.flash[0x8000:0x8000 + 512 + len(APP)] = header(LEGACY, 139, 50, APP, stamp) + APP
st, vtor, msp = b.boot()
expect_start(b, st, vtor, msp)
assert st["erases"] == 0 and st["programs"] == 0
print(f"1 plain start: jumped to app reset 0x{st['app']:08x}, VTOR 0x{vtor:08x}, no external writes")

# 2. Published candidate FW 51 with the trial marker: backup, trial armed, install, first launch.
b.ext[0:512 + len(APP)] = header(NEW, 139, 51, APP51, stamp + 1) + APP51
st, vtor, msp = b.boot()
expect_start(b, st, vtor, msp)
assert b.app_version() == (NEW, 139, 51), b.app_version()
assert struct.unpack_from("<IBH", b.ext, BACKUP) == (LEGACY, 139, 50)
assert b.word(0) == 0x494E5354 and b.word(4) == 0 and b.word(24) == 0 and b.word(32) == 0xFFFFFFFE and b.word(12) == 0xFFFFFFFF
assert struct.unpack_from("<I", b.ext, 0)[0] == 0
print(f"2 install: FW 51 installed, backup FW 50, trial armed, launch mask {b.word(32):08x}")

# 3. Two more unconfirmed starts.
for i in range(2):
    st, vtor, msp = b.boot(); expect_start(b, st, vtor, msp)
assert b.app_version() == (NEW, 139, 51) and b.word(32) == 0xFFFFFFF8
print(f"3 launches 2 and 3: FW 51 started, mask {b.word(32):08x}")

# 4. Fourth start: rollback to FW 50, error 8, start of the restored FW.
st, vtor, msp = b.boot(); expect_start(b, st, vtor, msp)
assert b.app_version() == (LEGACY, 139, 50) and b.word(12) == 8, (b.app_version(), hex(b.word(12)))
st, vtor, msp = b.boot(); expect_start(b, st, vtor, msp)
assert b.app_version() == (LEGACY, 139, 50) and st["erases"] == 0 and st["programs"] == 0
print("4 rollback: FW 50 restored, +12=8, next start runs FW 50 without Flash writes")

# 5. Confirmed trial stays.
c = Board()
c.flash[0x8000:0x8000 + 512 + len(APP)] = header(LEGACY, 139, 50, APP, stamp) + APP
c.ext[0:512 + len(APP)] = header(NEW, 139, 51, APP51, stamp + 1) + APP51
st, vtor, msp = c.boot(); expect_start(c, st, vtor, msp)
struct.pack_into("<I", c.ext, JOURNAL + 28, 0)
for i in range(4):
    st, vtor, msp = c.boot(); expect_start(c, st, vtor, msp)
assert c.app_version() == (NEW, 139, 51) and c.word(32) == 0xFFFFFFFE
print("5 confirmed: FW 51 kept over 4 more starts, no launches counted")
print("ALL PASSED")
