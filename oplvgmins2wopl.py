#
# oplvgmins2wopl.py
#
# 사용법:
#   python oplvgmins2wopl.py input.vgm
#   python oplvgmins2wopl.py input.vgz
#
# 출력:
#   input_opli/*.opli
#   input.wopl
#
# 지원:
#   YM2413 / OPLL    : VGM cmd 0x51, 0xA1
#   YM3812 / OPL2    : VGM cmd 0x5A, 0xAA
#   YM3526 / OPL     : VGM cmd 0x5B, 0xAB
#   Y8950            : VGM cmd 0x5C, 0xAC
#   YMF262 / OPL3    : VGM cmd 0x5E/0x5F, 0xAE/0xAF
#   YMF278B / OPL4   : VGM cmd 0xD0, FM port 0/1만 처리
#
# 주의:
#   - OPLL 내장 preset은 공통 YM2413 기본 patch table을 사용.
#   - OPL3 4-op 악기는 0x104, 즉 port 1 reg 0x04 mask를 보고 하나의 4-op OPLI로 저장.
#   - OPL4의 PCM/wavetable part는 OPLI/WOPL로 표현할 수 없으므로 제외.

import sys
import gzip
import math
import struct
from pathlib import Path


# ------------------------------------------------------------
# OPL operator/channel mapping
# ------------------------------------------------------------

CH_OP_OFFSETS = [
    (0x00, 0x03),  # ch0  mod/car
    (0x01, 0x04),  # ch1
    (0x02, 0x05),  # ch2
    (0x08, 0x0B),  # ch3
    (0x09, 0x0C),  # ch4
    (0x0A, 0x0D),  # ch5
    (0x10, 0x13),  # ch6
    (0x11, 0x14),  # ch7
    (0x12, 0x15),  # ch8
]

OP_OFFSET_TO_CH_OP = {}

for ch, (mod_off, car_off) in enumerate(CH_OP_OFFSETS):
    OP_OFFSET_TO_CH_OP[mod_off] = (ch, 0)
    OP_OFFSET_TO_CH_OP[car_off] = (ch, 1)


# OPL3 4-op pair bits in port 1, register 0x04
OPL3_4OP_PAIRS = {
    0: (0, 3),
    1: (1, 4),
    2: (2, 5),
    3: (9, 12),
    4: (10, 13),
    5: (11, 14),
}

OPL3_SECONDARY_TO_PRIMARY = {
    3: 0,
    4: 1,
    5: 2,
    12: 9,
    13: 10,
    14: 11,
}


RHYTHM_FLAGS = {
    "BD":  0x08,
    "SD":  0x10,
    "TOM": 0x18,
    "CYM": 0x20,
    "HH":  0x28,
}

RHYTHM_MIDI_KEYS = {
    "BD":  35,
    "SD":  38,
    "TOM": 45,
    "CYM": 49,
    "HH":  42,
}


# Common YM2413 / OPLL default instrument table.
# 0번은 user/custom instrument.
OPLL_PATCHES = [
    [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
    [0x71, 0x61, 0x1E, 0x17, 0xD0, 0x78, 0x00, 0x17],
    [0x13, 0x41, 0x1E, 0x0D, 0xD8, 0xF7, 0x23, 0x13],
    [0x13, 0x01, 0x99, 0x00, 0xF2, 0xC4, 0x21, 0x23],
    [0x11, 0x61, 0x0E, 0x07, 0x8D, 0x64, 0x70, 0x27],
    [0x22, 0x21, 0x1E, 0x06, 0xF0, 0x76, 0x08, 0x28],
    [0x31, 0x22, 0x16, 0x05, 0xE0, 0x71, 0x00, 0x18],
    [0x21, 0x61, 0x1D, 0x07, 0x82, 0x80, 0x10, 0x17],
    [0x23, 0x21, 0x2D, 0x16, 0x90, 0x90, 0x00, 0x07],
    [0x61, 0x61, 0x1B, 0x06, 0x64, 0x65, 0x10, 0x17],
    [0x61, 0x61, 0x0C, 0x18, 0x85, 0xF0, 0x70, 0x07],
    [0x23, 0x21, 0x1A, 0x17, 0xA0, 0x72, 0x00, 0x17],
    [0x97, 0xC1, 0x28, 0x07, 0xFF, 0xF3, 0x22, 0x12],
    [0x61, 0x10, 0x0C, 0x05, 0xF2, 0xE4, 0x40, 0x44],
    [0x01, 0x01, 0x56, 0x03, 0xB4, 0xB2, 0x23, 0x23],
    [0x21, 0x01, 0x89, 0x03, 0xF1, 0xE4, 0xF0, 0x23],
]


# ------------------------------------------------------------
# Utility
# ------------------------------------------------------------

def read_vgm(path: Path) -> bytes:
    data = path.read_bytes()

    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)

    if data[:4] != b"Vgm ":
        raise ValueError("This file is not VGM/VGZ file.")

    return data


def u32le(data: bytes, off: int) -> int:
    if off + 4 > len(data):
        return 0
    return int.from_bytes(data[off:off + 4], "little")


def get_vgm_data_offset(data: bytes) -> int:
    rel = u32le(data, 0x34)

    if rel:
        return 0x34 + rel

    return 0x40


def safe_filename(name: str) -> str:
    out = ""

    for c in name:
        if c.isalnum() or c in ("_", "-"):
            out += c
        else:
            out += "_"

    return out or "INST"


def fit_name_32(name: str) -> bytes:
    b = name.encode("utf-8")[:31]
    return (b + b"\x00").ljust(32, b"\x00")


def blank_entry(name: str = "Blank") -> bytes:
    entry = bytearray()

    entry.extend(fit_name_32(name))
    entry.extend(struct.pack(">h", 0))  # key offset 1
    entry.extend(struct.pack(">h", 0))  # key offset 2
    entry.extend(struct.pack("b", 0))   # velocity offset
    entry.extend(struct.pack("b", 0))   # second voice detune
    entry.append(0)                     # percussion key
    entry.append(0x04)                  # blank instrument flag
    entry.append(0)                     # feedback/connection 1
    entry.append(0)                     # feedback/connection 2
    entry.extend(b"\x00" * 20)          # four operators * 5 bytes

    assert len(entry) == 62
    return bytes(entry)


def make_opli(entry: bytes, percussion: bool) -> bytes:
    assert len(entry) == 62

    out = bytearray()
    out.extend(b"WOPL3-INST\x00")
    out.extend(struct.pack("<H", 2))
    out.append(1 if percussion else 0)
    out.extend(entry)

    assert len(out) == 76
    return bytes(out)


def make_wopl(instruments):
    melodic_entries = [x["entry"] for x in instruments if not x["drum"]]
    percussion_entries = [x["entry"] for x in instruments if x["drum"]]

    melodic_banks = max(1, math.ceil(len(melodic_entries) / 128))
    percussion_banks = math.ceil(len(percussion_entries) / 128)

    melodic_total = melodic_banks * 128
    percussion_total = percussion_banks * 128

    melodic_entries += [blank_entry()] * (melodic_total - len(melodic_entries))
    percussion_entries += [blank_entry()] * (percussion_total - len(percussion_entries))

    out = bytearray()

    out.extend(b"WOPL3-BANK\x00")
    out.extend(struct.pack("<H", 2))
    out.extend(struct.pack(">H", melodic_banks))
    out.extend(struct.pack(">H", percussion_banks))
    out.append(0)  # global flags
    out.append(0)  # volume model: Generic

    assert len(out) == 19

    # melodic bank metadata, 34 bytes each
    for i in range(melodic_banks):
        out.extend(fit_name_32(f"Melodic {i}"))
        out.append(i & 0x7F)  # LSB bank index
        out.append(0)         # MSB bank index

    # percussion bank metadata, 34 bytes each
    for i in range(percussion_banks):
        out.extend(fit_name_32(f"Percussion {i}"))
        out.append(i & 0x7F)
        out.append(0)

    for entry in melodic_entries:
        assert len(entry) == 62
        out.extend(entry)

    for entry in percussion_entries:
        assert len(entry) == 62
        out.extend(entry)

    return bytes(out)


# ------------------------------------------------------------
# VGM command skipping
# ------------------------------------------------------------

def skip_vgm_command(data: bytes, pos: int, cmd: int, sample_pos: int):
    if cmd == 0x61:
        if pos + 2 > len(data):
            return pos, sample_pos, True
        sample_pos += data[pos] | (data[pos + 1] << 8)
        pos += 2
        return pos, sample_pos, False

    if cmd == 0x62:
        sample_pos += 735
        return pos, sample_pos, False

    if cmd == 0x63:
        sample_pos += 882
        return pos, sample_pos, False

    if cmd == 0x66:
        return pos, sample_pos, True

    if cmd == 0x67:
        if pos >= len(data):
            return pos, sample_pos, True

        if data[pos] == 0x66:
            pos += 1

        if pos + 5 > len(data):
            return pos, sample_pos, True

        size = u32le(data, pos + 1)
        pos += 5 + size
        return pos, sample_pos, False

    if cmd == 0x68:
        # PCM RAM write: 0x68 0x66 cc oo oo oo dd dd dd ss ss ss
        pos += 11
        return pos, sample_pos, False

    if 0x70 <= cmd <= 0x7F:
        sample_pos += (cmd & 0x0F) + 1
        return pos, sample_pos, False

    if 0x80 <= cmd <= 0x8F:
        sample_pos += cmd & 0x0F
        return pos, sample_pos, False

    if cmd in (0x4F, 0x50):
        pos += 1
        return pos, sample_pos, False

    # two-byte chip writes not handled here
    if cmd in (
        0x52, 0x53,
        0x54,
        0x55,
        0x56, 0x57,
        0x58, 0x59,
        0x5D,
        0xA0,
        0xA2, 0xA3, 0xA4, 0xA5,
        0xA6, 0xA7, 0xA8, 0xA9,
        0xAD,
        0xB0, 0xB1, 0xB2, 0xB3, 0xB4,
        0xB5, 0xB6, 0xB7, 0xB8, 0xB9,
        0xBA, 0xBB, 0xBC, 0xBD, 0xBE, 0xBF,
    ):
        pos += 2
        return pos, sample_pos, False

    if 0x90 <= cmd <= 0x95:
        if cmd == 0x90:
            pos += 4
        elif cmd == 0x91:
            pos += 4
        elif cmd == 0x92:
            pos += 5
        elif cmd == 0x93:
            pos += 10
        elif cmd == 0x94:
            pos += 1
        elif cmd == 0x95:
            pos += 4
        return pos, sample_pos, False

    if 0x30 <= cmd <= 0x3F:
        pos += 1
        return pos, sample_pos, False

    if 0x40 <= cmd <= 0x4E:
        pos += 2
        return pos, sample_pos, False

    if 0xA1 <= cmd <= 0xAF:
        pos += 2
        return pos, sample_pos, False

    if 0xC0 <= cmd <= 0xCF:
        pos += 3
        return pos, sample_pos, False

    if 0xD0 <= cmd <= 0xDF:
        pos += 3
        return pos, sample_pos, False

    if cmd == 0xE0:
        pos += 4
        return pos, sample_pos, False

    if 0xE1 <= cmd <= 0xFF:
        pos += 4
        return pos, sample_pos, False

    raise ValueError(f"알 수 없는 VGM command: 0x{cmd:02X}")


# ------------------------------------------------------------
# OPL / OPL2 / OPL3 / OPL4-FM state
# ------------------------------------------------------------

class OPLChipState:
    def __init__(self, name: str, channels: int, opl3: bool = False):
        self.name = name
        self.channels = [self._new_channel() for _ in range(channels)]
        self.key_on = [False] * channels

        self.rhythm_mode = False
        self.old_rhythm_bits = 0

        self.opl3 = opl3
        self.opl3_mode = False
        self.four_op_mask = 0

    def _new_operator(self):
        return {
            "am": 0,
            "vib": 0,
            "eg": 0,
            "ksr": 0,
            "mult": 0,
            "ksl": 0,
            "tl": 0,
            "ar": 0,
            "dr": 0,
            "sl": 0,
            "rr": 0,
            "ws": 0,
        }

    def _new_channel(self):
        return {
            "fb": 0,
            "conn": 0,
            "ops": [
                self._new_operator(),  # modulator
                self._new_operator(),  # carrier
            ],
        }

    def write(self, port: int, reg: int, val: int):
        reg &= 0xFF
        val &= 0xFF

        bank_base = port * 9

        # OPL3 4-op connection select: port 1, reg 0x04
        if self.opl3 and port == 1 and reg == 0x04:
            self.four_op_mask = val & 0x3F
            return

        # OPL3 enable: port 1, reg 0x05 bit 0
        if self.opl3 and port == 1 and reg == 0x05:
            self.opl3_mode = bool(val & 0x01)
            return

        # Rhythm mode: port 0, reg 0xBD bit 5
        if port == 0 and reg == 0xBD:
            self.rhythm_mode = bool(val & 0x20)
            return

        if 0xC0 <= reg <= 0xC8:
            ch = bank_base + (reg - 0xC0)
            if ch >= len(self.channels):
                return

            self.channels[ch]["fb"] = (val >> 1) & 0x07
            self.channels[ch]["conn"] = val & 0x01
            return

        base = reg & 0xE0
        off = reg & 0x1F

        if off not in OP_OFFSET_TO_CH_OP:
            return

        local_ch, op_i = OP_OFFSET_TO_CH_OP[off]
        ch = bank_base + local_ch

        if ch >= len(self.channels):
            return

        op = self.channels[ch]["ops"][op_i]

        if base == 0x20:
            op["am"] = (val >> 7) & 1
            op["vib"] = (val >> 6) & 1
            op["eg"] = (val >> 5) & 1
            op["ksr"] = (val >> 4) & 1
            op["mult"] = val & 0x0F

        elif base == 0x40:
            op["ksl"] = (val >> 6) & 0x03
            op["tl"] = val & 0x3F

        elif base == 0x60:
            op["ar"] = (val >> 4) & 0x0F
            op["dr"] = val & 0x0F

        elif base == 0x80:
            op["sl"] = (val >> 4) & 0x0F
            op["rr"] = val & 0x0F

        elif base == 0xE0:
            # OPL2 uses 0..3; OPL3 uses 0..7.
            op["ws"] = val & 0x07

    def key_write(self, port: int, reg: int, val: int):
        bank_base = port * 9
        ch = bank_base + (reg - 0xB0)

        if not (0 <= ch < len(self.channels)):
            return None

        new_key = bool(val & 0x20)
        old_key = self.key_on[ch]
        self.key_on[ch] = new_key

        if new_key and not old_key:
            return ch

        return None

    def rhythm_events(self, val: int):
        rhythm_mode = bool(val & 0x20)
        new_bits = val & 0x1F

        if not rhythm_mode:
            self.old_rhythm_bits = 0
            self.rhythm_mode = False
            return []

        self.rhythm_mode = True

        newly_on = new_bits & (~self.old_rhythm_bits)
        self.old_rhythm_bits = new_bits

        events = []

        if newly_on & 0x10:
            events.append((6, "BD"))
        if newly_on & 0x08:
            events.append((7, "SD"))
        if newly_on & 0x04:
            events.append((8, "TOM"))
        if newly_on & 0x02:
            events.append((8, "CYM"))
        if newly_on & 0x01:
            events.append((7, "HH"))

        return events

    def is_4op_primary(self, ch: int) -> bool:
        if not self.opl3:
            return False

        for bit, (a, b) in OPL3_4OP_PAIRS.items():
            if ch == a and (self.four_op_mask & (1 << bit)):
                return True

        return False

    def is_4op_secondary(self, ch: int) -> bool:
        if not self.opl3:
            return False

        primary = OPL3_SECONDARY_TO_PRIMARY.get(ch)

        if primary is None:
            return False

        return self.is_4op_primary(primary)

    def get_4op_pair(self, ch: int):
        if not self.opl3:
            return None

        for bit, (a, b) in OPL3_4OP_PAIRS.items():
            if ch == a and (self.four_op_mask & (1 << bit)):
                return a, b

        return None

    def fb_conn_byte(self, ch: int) -> int:
        c = self.channels[ch]
        return ((c["fb"] & 0x07) << 1) | (c["conn"] & 0x01)

    def op_to_bytes(self, op: dict) -> bytes:
        reg20 = (
            ((op["am"] & 1) << 7) |
            ((op["vib"] & 1) << 6) |
            ((op["eg"] & 1) << 5) |
            ((op["ksr"] & 1) << 4) |
            (op["mult"] & 0x0F)
        )

        reg40 = ((op["ksl"] & 0x03) << 6) | (op["tl"] & 0x3F)
        reg60 = ((op["ar"] & 0x0F) << 4) | (op["dr"] & 0x0F)
        reg80 = ((op["sl"] & 0x0F) << 4) | (op["rr"] & 0x0F)
        regE0 = op["ws"] & 0x07

        return bytes([reg20, reg40, reg60, reg80, regE0])

    def entry_2op(self, ch: int, name: str, drum_kind: str = "") -> bytes:
        c = self.channels[ch]
        mod = c["ops"][0]
        car = c["ops"][1]

        flags = 0x00
        percussion_key = 0

        if drum_kind:
            flags |= RHYTHM_FLAGS.get(drum_kind, 0)
            percussion_key = RHYTHM_MIDI_KEYS.get(drum_kind, 0)

        entry = bytearray()

        entry.extend(fit_name_32(name))
        entry.extend(struct.pack(">h", 0))
        entry.extend(struct.pack(">h", 0))
        entry.extend(struct.pack("b", 0))
        entry.extend(struct.pack("b", 0))
        entry.append(percussion_key & 0xFF)
        entry.append(flags & 0xFF)
        entry.append(self.fb_conn_byte(ch))
        entry.append(0)

        # WOPL/OPLI operator order:
        # op1 = Carrier1, op2 = Modulator1, op3/op4 unused
        entry.extend(self.op_to_bytes(car))
        entry.extend(self.op_to_bytes(mod))
        entry.extend(b"\x00" * 5)
        entry.extend(b"\x00" * 5)

        assert len(entry) == 62
        return bytes(entry)

    def entry_4op(self, ch_a: int, ch_b: int, name: str) -> bytes:
        c1 = self.channels[ch_a]
        c2 = self.channels[ch_b]

        mod1 = c1["ops"][0]
        car1 = c1["ops"][1]
        mod2 = c2["ops"][0]
        car2 = c2["ops"][1]

        entry = bytearray()

        entry.extend(fit_name_32(name))
        entry.extend(struct.pack(">h", 0))
        entry.extend(struct.pack(">h", 0))
        entry.extend(struct.pack("b", 0))
        entry.extend(struct.pack("b", 0))
        entry.append(0)
        entry.append(0x01)  # 4-op mode flag
        entry.append(self.fb_conn_byte(ch_a))
        entry.append(self.fb_conn_byte(ch_b))

        # WOPL/OPLI 4-op operator order:
        # op1 = Carrier1, op2 = Modulator1, op3 = Carrier2, op4 = Modulator2
        entry.extend(self.op_to_bytes(car1))
        entry.extend(self.op_to_bytes(mod1))
        entry.extend(self.op_to_bytes(car2))
        entry.extend(self.op_to_bytes(mod2))

        assert len(entry) == 62
        return bytes(entry)


# ------------------------------------------------------------
# OPLL / YM2413 state
# ------------------------------------------------------------

class OPLLState:
    def __init__(self, name: str):
        self.name = name
        self.custom = [0] * 8
        self.inst = [0] * 9
        self.volume = [0] * 9
        self.key_on = [False] * 9

    def write(self, reg: int, val: int):
        reg &= 0xFF
        val &= 0xFF

        if 0x00 <= reg <= 0x07:
            self.custom[reg] = val
            return

        if 0x30 <= reg <= 0x38:
            ch = reg - 0x30
            self.inst[ch] = (val >> 4) & 0x0F
            self.volume[ch] = val & 0x0F
            return

    def key_write(self, reg: int, val: int):
        ch = reg - 0x20

        if not (0 <= ch <= 8):
            return None

        new_key = bool(val & 0x10)
        old_key = self.key_on[ch]
        self.key_on[ch] = new_key

        if new_key and not old_key:
            return ch

        return None

    def get_patch_bytes(self, ch: int):
        inst_no = self.inst[ch]

        if inst_no == 0:
            return list(self.custom)

        return list(OPLL_PATCHES[inst_no])

    def op_from_patch(self, data, ch: int):
        vol = self.volume[ch] & 0x0F

        mod = {
            "am":   (data[0] >> 7) & 1,
            "vib":  (data[0] >> 6) & 1,
            "eg":   (data[0] >> 5) & 1,
            "ksr":  (data[0] >> 4) & 1,
            "mult": data[0] & 0x0F,
            "ksl":  (data[2] >> 6) & 0x03,
            "tl":   data[2] & 0x3F,
            "ar":   (data[4] >> 4) & 0x0F,
            "dr":   data[4] & 0x0F,
            "sl":   (data[6] >> 4) & 0x0F,
            "rr":   data[6] & 0x0F,
            "ws":   0,
        }

        car = {
            "am":   (data[1] >> 7) & 1,
            "vib":  (data[1] >> 6) & 1,
            "eg":   (data[1] >> 5) & 1,
            "ksr":  (data[1] >> 4) & 1,
            "mult": data[1] & 0x0F,
            "ksl":  (data[3] >> 6) & 0x03,
            # OPLL channel volume controls carrier level.
            "tl":   min(63, vol << 2),
            "ar":   (data[5] >> 4) & 0x0F,
            "dr":   data[5] & 0x0F,
            "sl":   (data[7] >> 4) & 0x0F,
            "rr":   data[7] & 0x0F,
            "ws":   0,
        }

        fb = data[3] & 0x07
        conn = 0

        return mod, car, fb, conn

    def op_to_bytes(self, op: dict) -> bytes:
        reg20 = (
            ((op["am"] & 1) << 7) |
            ((op["vib"] & 1) << 6) |
            ((op["eg"] & 1) << 5) |
            ((op["ksr"] & 1) << 4) |
            (op["mult"] & 0x0F)
        )

        reg40 = ((op["ksl"] & 0x03) << 6) | (op["tl"] & 0x3F)
        reg60 = ((op["ar"] & 0x0F) << 4) | (op["dr"] & 0x0F)
        reg80 = ((op["sl"] & 0x0F) << 4) | (op["rr"] & 0x0F)

        return bytes([reg20, reg40, reg60, reg80, 0])

    def entry(self, ch: int, name: str) -> bytes:
        data = self.get_patch_bytes(ch)
        mod, car, fb, conn = self.op_from_patch(data, ch)

        entry = bytearray()

        entry.extend(fit_name_32(name))
        entry.extend(struct.pack(">h", 0))
        entry.extend(struct.pack(">h", 0))
        entry.extend(struct.pack("b", 0))
        entry.extend(struct.pack("b", 0))
        entry.append(0)
        entry.append(0)
        entry.append(((fb & 0x07) << 1) | (conn & 1))
        entry.append(0)

        entry.extend(self.op_to_bytes(car))
        entry.extend(self.op_to_bytes(mod))
        entry.extend(b"\x00" * 5)
        entry.extend(b"\x00" * 5)

        assert len(entry) == 62
        return bytes(entry)


# ------------------------------------------------------------
# De-duplication
# ------------------------------------------------------------

def entry_key_ignore_tl(entry: bytes, drum: bool, key_extra: bytes):
    """
    WOPL/OPLI entry에서 name을 제외하고 비교하되,
    operator TL만 무시한다.

    entry layout:
      0..31  name
      32..41 misc
      42..46 op1
      47..51 op2
      52..56 op3
      57..61 op4

    TL is second byte of each 5-byte operator.
    """
    assert len(entry) == 62

    data = bytearray(entry[32:])

    for rel in (43 - 32, 48 - 32, 53 - 32, 58 - 32):
        if 0 <= rel < len(data):
            data[rel] &= 0xC0  # keep KSL, ignore TL

    return (bool(drum), key_extra, bytes(data))


def add_entry(instruments, seen, chip_name: str, name: str, entry: bytes,
              sample_pos: int, drum: bool = False, key_extra: bytes = b""):
    key = (
        chip_name.encode("ascii", errors="ignore"),
        entry_key_ignore_tl(entry, drum, key_extra),
    )

    if key in seen:
        return

    seen.add(key)

    instruments.append({
        "name": name,
        "entry": entry,
        "data": make_opli(entry, drum),
        "sample": sample_pos,
        "drum": drum,
    })


# ------------------------------------------------------------
# Main parser
# ------------------------------------------------------------

def parse_vgm(data: bytes):
    pos = get_vgm_data_offset(data)
    sample_pos = 0

    chips = {
        "OPLL":      OPLLState("OPLL"),
        "OPLL_2":    OPLLState("OPLL_2"),

        "YM3812":    OPLChipState("YM3812", 9),
        "YM3812_2":  OPLChipState("YM3812_2", 9),

        "YM3526":    OPLChipState("YM3526", 9),
        "YM3526_2":  OPLChipState("YM3526_2", 9),

        "Y8950":     OPLChipState("Y8950", 9),
        "Y8950_2":   OPLChipState("Y8950_2", 9),

        "YMF262":    OPLChipState("YMF262", 18, opl3=True),
        "YMF262_2":  OPLChipState("YMF262_2", 18, opl3=True),

        "YMF278B":   OPLChipState("YMF278B", 18, opl3=True),
    }

    instruments = []
    seen = set()

    while pos < len(data):
        cmd_pos = pos
        cmd = data[pos]
        pos += 1

        # ------------------------
        # OPLL / YM2413
        # ------------------------
        if cmd in (0x51, 0xA1):
            chip = chips["OPLL" if cmd == 0x51 else "OPLL_2"]

            if pos + 2 > len(data):
                break

            reg = data[pos]
            val = data[pos + 1]
            pos += 2

            if 0x20 <= reg <= 0x28:
                ch = chip.key_write(reg, val)

                if ch is not None:
                    inst_no = chip.inst[ch]

                    if inst_no == 0:
                        name = f"{chip.name}_CUSTOM_CH{ch:02d}_{len(instruments):03d}"
                    else:
                        name = f"{chip.name}_P{inst_no:02d}_CH{ch:02d}_{len(instruments):03d}"

                    entry = chip.entry(ch, name)

                    add_entry(
                        instruments,
                        seen,
                        chip.name,
                        name,
                        entry,
                        sample_pos,
                        drum=False,
                        key_extra=bytes([inst_no]),
                    )

                chip.write(reg, val)
            else:
                chip.write(reg, val)

            continue

        # ------------------------
        # OPL-family writes
        # ------------------------
        chip = None
        port = 0
        reg = None
        val = None

        if cmd == 0x5A:
            chip = chips["YM3812"]
            port = 0
        elif cmd == 0xAA:
            chip = chips["YM3812_2"]
            port = 0

        elif cmd == 0x5B:
            chip = chips["YM3526"]
            port = 0
        elif cmd == 0xAB:
            chip = chips["YM3526_2"]
            port = 0

        elif cmd == 0x5C:
            chip = chips["Y8950"]
            port = 0
        elif cmd == 0xAC:
            chip = chips["Y8950_2"]
            port = 0

        elif cmd == 0x5E:
            chip = chips["YMF262"]
            port = 0
        elif cmd == 0x5F:
            chip = chips["YMF262"]
            port = 1
        elif cmd == 0xAE:
            chip = chips["YMF262_2"]
            port = 0
        elif cmd == 0xAF:
            chip = chips["YMF262_2"]
            port = 1

        elif cmd == 0xD0:
            # YMF278B / OPL4:
            # VGM command: 0xD0 pp aa dd
            # pp 0/1 = FM-compatible ports.
            if pos + 3 > len(data):
                break

            pp = data[pos]
            reg = data[pos + 1]
            val = data[pos + 2]
            pos += 3

            if pp in (0, 1):
                chip = chips["YMF278B"]
                port = pp
            else:
                # OPL4 PCM/wavetable side ignored.
                continue

        if chip is not None:
            if reg is None:
                if pos + 2 > len(data):
                    break

                reg = data[pos]
                val = data[pos + 1]
                pos += 2

            # Rhythm mode / percussion keys
            if port == 0 and reg == 0xBD:
                events = chip.rhythm_events(val)

                for ch, kind in events:
                    name = f"{chip.name}_{kind}_CH{ch:02d}_{len(instruments):03d}"
                    entry = chip.entry_2op(ch, name, kind)

                    add_entry(
                        instruments,
                        seen,
                        chip.name,
                        name,
                        entry,
                        sample_pos,
                        drum=True,
                        key_extra=kind.encode("ascii"),
                    )

                chip.write(port, reg, val)
                continue

            # Key-on
            if 0xB0 <= reg <= 0xB8:
                ch = chip.key_write(port, reg, val)

                if ch is not None:
                    if chip.is_4op_secondary(ch):
                        chip.write(port, reg, val)
                        continue

                    pair = chip.get_4op_pair(ch)

                    if pair is not None:
                        ch_a, ch_b = pair
                        name = f"{chip.name}_4OP_CH{ch_a:02d}_{len(instruments):03d}"
                        entry = chip.entry_4op(ch_a, ch_b, name)

                        add_entry(
                            instruments,
                            seen,
                            chip.name,
                            name,
                            entry,
                            sample_pos,
                            drum=False,
                            key_extra=b"4OP",
                        )
                    else:
                        drum = bool(port == 0 and chip.rhythm_mode and 6 <= ch <= 8)

                        if drum:
                            name = f"{chip.name}_DRUM_CH{ch:02d}_{len(instruments):03d}"
                        else:
                            name = f"{chip.name}_CH{ch:02d}_{len(instruments):03d}"

                        # 0xBD event에서 이미 드럼 종류별로 잡는 게 기본이지만,
                        # 혹시 리듬 모드 중 B0 key-on으로 잡힌 경우도 percussion으로 분리.
                        drum_kind = "BD" if drum and ch == 6 else ""
                        entry = chip.entry_2op(ch, name, drum_kind)

                        add_entry(
                            instruments,
                            seen,
                            chip.name,
                            name,
                            entry,
                            sample_pos,
                            drum=drum,
                            key_extra=b"DRUM" if drum else b"",
                        )

                chip.write(port, reg, val)
                continue

            chip.write(port, reg, val)
            continue

        # ------------------------
        # Other VGM commands
        # ------------------------
        try:
            pos, sample_pos, should_break = skip_vgm_command(data, pos, cmd, sample_pos)
        except ValueError as e:
            raise ValueError(f"{e} at 0x{cmd_pos:X}")

        if should_break:
            break

    return instruments


# ------------------------------------------------------------
# Main
# ------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print("How to use:")
        print("python oplvgmins2wopl.py input.vgm")
        print("python oplvgmins2wopl.py input.vgz")
        sys.exit(1)

    input_path = Path(sys.argv[1])
    data = read_vgm(input_path)

    instruments = parse_vgm(data)

    if not instruments:
        print("There are no OPL instruments.")
        return

    output_dir = input_path.parent / f"{input_path.stem}_opli"
    output_dir.mkdir(parents=True, exist_ok=True)

    for inst in instruments:
        out_path = output_dir / (safe_filename(inst["name"]) + ".opli")
        out_path.write_bytes(inst["data"])

    wopl_path = input_path.with_suffix(".wopl")
    wopl_path.write_bytes(make_wopl(instruments))

    melodic = sum(1 for x in instruments if not x["drum"])
    perc = sum(1 for x in instruments if x["drum"])

    print(f"OPLI Extract complete : {output_dir}")
    print(f"WOPL Extract complete : {wopl_path}")
    print(f"Total instrument(s)   : {len(instruments)}")
    print(f"Melodic               : {melodic}")
    print(f"Percussion            : {perc}")


if __name__ == "__main__":
    main()