"""Run replace_ram() of the IAR-built application against SPI/Flash register models.
The RAM copy is produced by the application's own __iar_data_init3. Model of the checks
from Bootloader/tests/test_manual_ram.py (Codex, GCC build), applied to the IAR build."""
import struct
from pathlib import Path
from elftools.elf.elffile import ELFFile
from unicorn import Uc, UC_ARCH_ARM, UC_MODE_THUMB, UC_MODE_MCLASS, UC_HOOK_MEM_READ, UC_HOOK_MEM_WRITE, UC_HOOK_CODE
from unicorn.arm_const import UC_ARM_REG_SP, UC_ARM_REG_R0, UC_ARM_REG_LR, UC_ARM_REG_PC, UC_ARM_REG_XPSR

R = Path(r"C:\Users\kryaz\Desktop\мк\СофтЭксперт\Для новой системы\Domovoi Next_IAR95\Domovoi Next_IAR95")
elf = ELFFile(open(R / "Release/Exe/DomovoiNext.out", "rb"))
sym = {s.name: s["st_value"] for s in elf.get_section_by_name(".symtab").iter_symbols()}
app = (R / "Release/Exe/DomovoiNext.bin").read_bytes()
image = (R / "Bootloader/build/Release/Exe/DomovoiBoot.bin").read_bytes()
assert len(image) == 32768 and image[0x7FFC] == 0x32

def crc32(data):
    c = 0xFFFFFFFF
    for (w,) in struct.iter_unpack("<I", data):
        c ^= w
        for _ in range(32):
            c = ((c << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if c & 0x80000000 else (c << 1) & 0xFFFFFFFF
    return c

STOP = 0x0801FFF0
def ram_after_init():
    u = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
    u.mem_map(0x08000000, 0x20000); u.mem_write(0x08000000, b"\xa5" * 0x8200); u.mem_write(0x08008200, app)
    u.mem_map(0x20000000, 0x5000)
    u.reg_write(UC_ARM_REG_SP, 0x20005000); u.reg_write(UC_ARM_REG_LR, STOP | 1); u.reg_write(UC_ARM_REG_XPSR, 0x01000000)
    u.emu_start(sym["__iar_data_init3"] | 1, STOP, count=5000000)
    assert u.reg_read(UC_ARM_REG_PC) == STOP
    return bytes(u.mem_read(0x20000000, 0x5000)), bytes(u.mem_read(0x08000000, 0x20000))

RAM, FLASH = ram_after_init()
fn = sym["replace_ram"] & ~1
assert RAM[fn - 0x20000000:fn - 0x20000000 + 8] != bytes(8), "replace_ram was not copied to RAM"

def run(corrupt_page=None, cut=None):
    u = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
    u.mem_map(0x20000000, 0x5000); u.mem_write(0x20000000, RAM)
    u.mem_map(0x08000000, 0x20000); u.mem_write(0x08000000, FLASH)
    u.mem_map(0x40000000, 0x30000); u.mem_map(0xE000E000, 0x2000)
    checks = b"".join(struct.pack("<I", crc32(image[i:i + 1024])) for i in range(0, 32768, 1024))
    u.mem_write(0x20004000, checks)
    st = dict(index=0, address=0, rx=0, flash_address=0, reset=False, mutations=0, erased=[])
    def write(uc, access, address, size, value, _):
        if address == 0x40010C14 and value & 4096:
            st["index"] = 0; st["address"] = 0
        elif address == 0x4001300C:
            i = st["index"]; st["index"] += 1
            if i == 0: assert value == 3
            if 1 <= i <= 3: st["address"] = (st["address"] << 8) | (value & 255)
            if i >= 4:
                off = st["address"]; assert 0 <= off < 32768
                st["rx"] = image[off] ^ (1 if off // 1024 == corrupt_page else 0); st["address"] += 1
            else:
                st["rx"] = 0
        elif address == 0x40022014: st["flash_address"] = value
        elif address == 0x40022010 and value == 0x42:
            page = st["flash_address"]; assert 0x08000000 <= page < 0x08008000
            st["erased"].append(page); uc.mem_write(page, b"\xff" * 1024); st["mutations"] += 1
        elif 0x08000000 <= address < 0x08020000:
            assert address + size <= 0x08008000, "Application overwritten"
            st["mutations"] += 1
        elif address == 0xE000ED0C:
            assert value == 0x05FA0004; st["reset"] = True; uc.emu_stop()
        if cut and st["mutations"] >= cut: uc.emu_stop()
    def read(uc, access, address, size, value, _):
        if address == 0x40013008: uc.mem_write(address, struct.pack("<I", 3))
        elif address == 0x4001300C: uc.mem_write(address, struct.pack("<I", st["rx"]))
        elif address == 0x4002200C: uc.mem_write(address, bytes(4))
    def code(uc, address, size, _):
        assert 0x20000000 <= address < 0x20005000, f"Execution escaped RAM at 0x{address:08x}"
    u.hook_add(UC_HOOK_MEM_READ, read, begin=0x40000000, end=0x4002FFFF)
    u.hook_add(UC_HOOK_MEM_WRITE, write)
    u.hook_add(UC_HOOK_CODE, code)
    u.reg_write(UC_ARM_REG_SP, 0x20005000); u.reg_write(UC_ARM_REG_R0, 0x20004000); u.reg_write(UC_ARM_REG_XPSR, 0x01000000)
    u.emu_start(sym["replace_ram"] | 1, 0, count=20000000)
    assert bytes(u.mem_read(0x08008000, 0x18000)) == FLASH[0x8000:], "application area changed"
    if cut:
        assert st["mutations"] >= cut
    else:
        assert st["reset"], "writer did not reset"
        if corrupt_page is None: assert bytes(u.mem_read(0x08000000, 32768)) == image
        else: assert len(st["erased"]) == corrupt_page, (len(st["erased"]), corrupt_page)
    return st["mutations"]

total = run()
print(f"complete: new bootloader 0x32 written byte-exact, reset requested, {total} Flash operations, execution stayed in RAM")
run(corrupt_page=0); run(corrupt_page=7)
print("SPI corruption on page 0 and 7: stops before erasing the damaged page")
for cut in (1, 2, 513, 514, total - 1): run(cut=cut)
print("5 interruption points: application area untouched")
