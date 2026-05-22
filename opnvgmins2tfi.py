#
# opnvgmins2tfi.py
#
# 사용법:
#   python opnvgmins2tfi.py input.vgm
#   python opnvgmins2tfi.py input.vgz
#
# 출력:
#   input파일과 같은 폴더에:
#     input파일명_tfi/inst_000_ch0.tfi
#     input파일명_tfi/inst_001_ch1.tfi
#     ...
#
# 지원:
#   YM2203 / OPN

import sys
import gzip
from pathlib import Path

TFI_OP_ORDER = [0, 1, 2, 3]

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
    if rel != 0:
        return 0x34 + rel
    return 0x40

class YM2203State:
    def __init__(self):
        self.channels = [
            self._new_channel(),
            self._new_channel(),
            self._new_channel(),
        ]

    def _new_channel(self):
        return {
            "algo": 0,
            "fb": 0,
            "ops": [
                {
                    "mul": 0,
                    "dt": 0,
                    "tl": 0,
                    "rs": 0,
                    "ar": 0,
                    "dr": 0,
                    "sr": 0,
                    "rr": 0,
                    "sl": 0,
                    "ssg": 0,
                }
                for _ in range(4)
            ],
        }

    def write(self, reg: int, val: int):
        reg &= 0xFF
        val &= 0xFF

        if 0xB0 <= reg <= 0xB2:
            ch = reg - 0xB0
            self.channels[ch]["algo"] = val & 0x07
            self.channels[ch]["fb"] = (val >> 3) & 0x07
            return

        base = reg & 0xF0
        sub = reg & 0x0F

        if base not in (0x30, 0x40, 0x50, 0x60, 0x70, 0x80, 0x90):
            return

        ch = sub & 0x03
        op_offset = sub & 0x0C

        if ch > 2:
            return

        op_index_by_offset = {
            0x00: 0,
            0x04: 1,
            0x08: 2,
            0x0C: 3,
        }

        if op_offset not in op_index_by_offset:
            return

        op = self.channels[ch]["ops"][op_index_by_offset[op_offset]]

        if base == 0x30:
            op["mul"] = val & 0x0F
            op["dt"] = (val >> 4) & 0x07

        elif base == 0x40:
            op["tl"] = val & 0x7F

        elif base == 0x50:
            op["rs"] = (val >> 6) & 0x03
            op["ar"] = val & 0x1F

        elif base == 0x60:
            op["dr"] = val & 0x1F

        elif base == 0x70:
            op["sr"] = val & 0x1F

        elif base == 0x80:
            op["sl"] = (val >> 4) & 0x0F
            op["rr"] = val & 0x0F

        elif base == 0x90:
            op["ssg"] = val & 0x0F

    def make_tfi(self, ch: int) -> bytes:
        c = self.channels[ch]

        out = bytearray()
        out.append(c["algo"] & 0x07)
        out.append(c["fb"] & 0x07)

        for op_i in TFI_OP_ORDER:
            op = c["ops"][op_i]

            dt_raw = op["dt"] & 0x07
            dt_signed_table = {
                0: 0,
                1: 1,
                2: 2,
                3: 3,
                4: 0,
                5: -3,
                6: -2,
                7: -1,
            }
            dt_tfi = dt_signed_table[dt_raw] + 3

            out.extend([
                op["mul"] & 0x0F,
                dt_tfi & 0x07,
                op["tl"] & 0x7F,
                op["rs"] & 0x03,
                op["ar"] & 0x1F,
                op["dr"] & 0x1F,
                op["sr"] & 0x1F,
                op["rr"] & 0x0F,
                op["sl"] & 0x0F,
                op["ssg"] & 0x0F,
            ])

        assert len(out) == 42
        return bytes(out)

    def make_tfi_compare_key_ignore_tl(self, ch: int) -> bytes:
        """
        TL만 다른 악기를 같은 악기로 취급하기 위한 비교용 키.

        TFI 구조:
          0: algorithm
          1: feedback
          operator마다 10 bytes:
            0 mul
            1 detune
            2 tl      <- 이 값만 무시
            3 rs
            4 ar
            5 dr
            6 sr
            7 rr
            8 sl
            9 ssg

        즉 42바이트 TFI에서 각 오퍼레이터의 TL 위치를 0으로 만든다.
        """
        tfi = bytearray(self.make_tfi(ch))

        for i in range(4):
            op_start = 2 + i * 10
            tl_index = op_start + 2
            tfi[tl_index] = 0

        return bytes(tfi)


def parse_vgm_and_extract_tfi(data: bytes):
    pos = get_vgm_data_offset(data)
    ym = YM2203State()

    instruments = []
    seen = set()
    sample_pos = 0

    while pos < len(data):
        cmd = data[pos]
        pos += 1

        if cmd == 0x55:
            if pos + 2 > len(data):
                break

            reg = data[pos]
            val = data[pos + 1]
            pos += 2

            if reg == 0x28:
                ch = val & 0x03
                key_bits = (val >> 4) & 0x0F

                if ch <= 2 and key_bits != 0:
                    tfi = ym.make_tfi(ch)

                    # 여기서 TL만 다른 악기는 같은 악기로 처리
                    compare_key = ym.make_tfi_compare_key_ignore_tl(ch)

                    if compare_key not in seen:
                        seen.add(compare_key)
                        instruments.append({
                            "channel": ch,
                            "sample": sample_pos,
                            "data": tfi,
                        })
            else:
                ym.write(reg, val)

        elif cmd == 0x61:
            if pos + 2 > len(data):
                break
            n = data[pos] | (data[pos + 1] << 8)
            pos += 2
            sample_pos += n

        elif cmd == 0x62:
            sample_pos += 735

        elif cmd == 0x63:
            sample_pos += 882

        elif cmd == 0x66:
            break

        elif cmd == 0x67:
            if pos >= len(data):
                break

            if data[pos] == 0x66:
                pos += 1

            if pos + 5 > len(data):
                break

            block_type = data[pos]
            size = u32le(data, pos + 1)
            pos += 5 + size

        elif 0x70 <= cmd <= 0x7F:
            sample_pos += (cmd & 0x0F) + 1

        elif cmd == 0x50:
            pos += 1

        elif cmd in (
            0x51, 0x52, 0x53, 0x54,
            0x56, 0x57, 0x58, 0x59,
            0x5A, 0x5B, 0x5C, 0x5D,
            0x5E, 0x5F,
            0xA0, 0xB0, 0xB1, 0xB3, 0xB4,
            0xB5, 0xB6, 0xB7, 0xB8, 0xB9,
            0xBA, 0xBB, 0xBC, 0xBD, 0xBE, 0xBF,
        ):
            pos += 2

        elif cmd == 0xA5:
            pos += 2

        elif cmd == 0x4F:
            pos += 1

        elif 0x80 <= cmd <= 0x8F:
            sample_pos += cmd & 0x0F

        elif 0x90 <= cmd <= 0x95:
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

        elif 0xC0 <= cmd <= 0xC8:
            pos += 3

        elif 0xD0 <= cmd <= 0xD6:
            pos += 3

        elif cmd == 0xE0:
            pos += 4

        elif 0x30 <= cmd <= 0x3F:
            pos += 1

        elif 0x41 <= cmd <= 0x4E:
            pos += 2

        elif 0xA1 <= cmd <= 0xAF:
            pos += 2

        elif 0xC9 <= cmd <= 0xCF:
            pos += 3

        elif 0xD7 <= cmd <= 0xDF:
            pos += 3

        elif 0xE1 <= cmd <= 0xFF:
            pos += 4

        else:
            raise ValueError(
                f"unknown VGM command: 0x{cmd:02X} at 0x{pos - 1:X}"
            )

    return instruments


def main():
    if len(sys.argv) < 2:
        print("How to use:")
        print("python opnvgmins2tfi.py input.vgm")
        print("python opnvgmins2tfi.py input.vgz")
        sys.exit(1)

    input_path = Path(sys.argv[1])
    data = read_vgm(input_path)

    # 출력 경로를 VGM 파일과 같은 폴더로 설정
    output_dir = input_path.parent / f"{input_path.stem}_tfi"
    output_dir.mkdir(parents=True, exist_ok=True)

    instruments = parse_vgm_and_extract_tfi(data)

    for i, inst in enumerate(instruments):
        ch = inst["channel"]
        out_path = output_dir / f"inst_{i:03d}_ch{ch}.tfi"
        out_path.write_bytes(inst["data"])

    print(f"Extract complete : {len(instruments)} instrument(s)")
    print(f"Output folder    : {output_dir}")


if __name__ == "__main__":
    main()