#!/usr/bin/env python3
"""Microsoft EXEPACK unpacker."""
import struct
import sys

def unpack_exepack(infile, outfile):
    with open(infile, 'rb') as f:
        data = bytearray(f.read())

    # Parse MZ header
    assert data[0:2] in [b'MZ', b'ZM'], "Not MZ"

    def w(off): return struct.unpack_from('<H', data, off)[0]

    header_paras = w(8)
    header_size = header_paras * 16
    last_page = w(2)
    pages = w(4)
    num_relocs = w(6)
    min_alloc = w(0x0A)
    max_alloc = w(0x0C)
    ss_init = w(0x0E)
    sp_init = w(0x10)
    ip_init = w(0x14)
    cs_init = w(0x16)
    reloc_ofs = w(0x18)

    print(f"CS:IP = {cs_init:04X}:{ip_init:04X}")
    print(f"SS:SP = {ss_init:04X}:{sp_init:04X}")
    print(f"Header: {header_size} bytes, Image starts at 0x{header_size:X}")

    image = data[header_size:]
    img_size = len(image)
    print(f"Image size: {img_size} bytes")

    # EXEPACK stub is at CS:IP in the image
    stub_start = cs_init * 16 + ip_init
    print(f"Stub starts at image offset: 0x{stub_start:X}")

    # The EXEPACK header is at CS:0000 (before the IP entry point)
    exepack_hdr = cs_init * 16

    # EXEPACK header format (at CS:0000):
    # +00: real IP
    # +02: real CS
    # +04: mem_start (or exepack data start)
    # +06: exepack_size (size of exepack stub + packed data area)
    # +08: real SP
    # +0A: real SS
    # +0C: dest_len (decompressed code size in paragraphs)
    # +0E: skip_len (bytes to skip? or another field)
    # But the exact layout varies by EXEPACK version.

    # Let me read the stub data and try to find the "Packed file is corrupt" string
    # to identify the exact version
    corrupt_str = b'Packed file is corrupt'
    corrupt_idx = image.find(corrupt_str)
    if corrupt_idx >= 0:
        print(f"Found error string at image offset 0x{corrupt_idx:X}")
        print(f"  Relative to CS: 0x{corrupt_idx - exepack_hdr:X}")

    # For this specific EXEPACK variant, let me read the header at CS:0000
    print(f"\nEXEPACK header at 0x{exepack_hdr:X}:")
    for i in range(0, 0x14, 2):
        val = w(header_size + exepack_hdr + i)
        print(f"  +{i:02X}: 0x{val:04X} ({val})")

    # Read header fields
    real_ip = w(header_size + exepack_hdr + 0)
    real_cs = w(header_size + exepack_hdr + 2)
    # Field at +4 is the exepack data start (mem_start)
    mem_start = w(header_size + exepack_hdr + 4)
    exepack_size = w(header_size + exepack_hdr + 6)
    real_sp = w(header_size + exepack_hdr + 8)
    real_ss = w(header_size + exepack_hdr + 0xA)
    dest_len = w(header_size + exepack_hdr + 0xC)

    print(f"\nReal CS:IP = {real_cs:04X}:{real_ip:04X}")
    print(f"Real SS:SP = {real_ss:04X}:{real_sp:04X}")
    print(f"Dest length (paragraphs): {dest_len} = {dest_len*16} bytes")
    print(f"EXEPACK size: {exepack_size}")

    # The packed data is everything in the image before CS:0000
    packed_data = image[:exepack_hdr]
    print(f"Packed data: {len(packed_data)} bytes")

    # EXEPACK decompression:
    # The packed data is read BACKWARD (from the end toward the beginning)
    # Output is written BACKWARD (from the end of the destination area)
    # Commands:
    #   0xB0: fill - read 1 byte, repeat it <count> times
    #   0xB1: fill (last command)
    #   0xB2: copy - copy <count> bytes from source
    #   0xB3: copy (last command)
    # Format per command (reading backward): fill_byte(if B0/B1), count_lo, count_hi, command

    dest_size = dest_len * 16
    output = bytearray(dest_size)

    # Source pointer: start at end of packed data, read backward
    src = len(packed_data) - 1
    # Destination pointer: start at end of output, write backward
    dst = dest_size - 1

    # Skip trailing 0xFF padding bytes
    while src >= 0 and packed_data[src] == 0xFF:
        src -= 1

    print(f"After skipping padding, src at 0x{src:X}")

    commands = 0
    while src >= 0 and dst >= 0:
        # Read command byte
        cmd = packed_data[src]
        src -= 1

        # Read count (16-bit, big-endian since we're reading backward)
        if src < 1:
            break
        count_hi = packed_data[src]
        src -= 1
        count_lo = packed_data[src]
        src -= 1
        count = (count_hi << 8) | count_lo

        cmd_type = cmd & 0xFE
        is_last = cmd & 0x01

        commands += 1
        if commands <= 10:
            print(f"  cmd=0x{cmd:02X} count={count} dst=0x{dst:X} src=0x{src:X}")

        if cmd_type == 0xB0:
            # Fill
            if src < 0:
                break
            fill_byte = packed_data[src]
            src -= 1
            for i in range(count):
                if dst < 0:
                    break
                output[dst] = fill_byte
                dst -= 1
        elif cmd_type == 0xB2:
            # Copy
            for i in range(count):
                if dst < 0 or src < 0:
                    break
                output[dst] = packed_data[src]
                dst -= 1
                src -= 1
        else:
            print(f"  Unknown command 0x{cmd:02X} at src=0x{src+3:X}")
            break

        if is_last:
            print(f"  Last command flag set after {commands} commands")
            break

    print(f"\nDecompressed: {dest_size - dst - 1} bytes, dst ended at 0x{dst:X}")

    # Handle relocations
    # The relocation table is stored after the EXEPACK header + stub code
    # It's in a compressed format: segments from 0x0000 to 0xF000 (step 0x1000)
    # For each segment, a count followed by offset values
    reloc_start = exepack_hdr + exepack_size  # This might not be right
    # Actually, in the stub disassembly at CS:0x98: mov si, 0x112, push cs, pop ds
    # The relocation table starts at CS:0x112
    reloc_table_off = exepack_hdr + 0x0112

    print(f"\nRelocation table at image offset 0x{reloc_table_off:X}")

    relocs = []
    pos = reloc_table_off
    seg_delta = 0
    while seg_delta <= 0xF000:
        if header_size + pos + 2 > len(data):
            break
        count = w(header_size + pos)
        pos += 2
        for i in range(count):
            if header_size + pos + 2 > len(data):
                break
            off = w(header_size + pos)
            pos += 2
            addr = seg_delta * 16 + off
            relocs.append((seg_delta, off))
        seg_delta += 0x1000

    print(f"Found {len(relocs)} relocations")

    # Write unpacked image as raw binary
    with open(outfile, 'wb') as f:
        f.write(bytes(output))

    print(f"\nWrote {len(output)} bytes to {outfile}")

    # Also write as a proper MZ EXE
    exe_outfile = outfile.replace('.bin', '.exe') if '.bin' in outfile else outfile + '.exe'

    # Build relocation table
    reloc_data = bytearray()
    for seg, off in relocs:
        reloc_data += struct.pack('<HH', off, seg)

    # MZ header: 28 bytes + reloc table, padded to paragraph boundary
    hdr_content_size = 28 + len(reloc_data)
    hdr_paras = (hdr_content_size + 15) // 16
    hdr_bytes = hdr_paras * 16

    total_size = hdr_bytes + len(output)
    total_pages = (total_size + 511) // 512
    last_page_bytes = total_size % 512

    new_header = struct.pack('<2sHHHHHHHHHHHHH',
        b'MZ',
        last_page_bytes,
        total_pages,
        len(relocs),  # num relocations
        hdr_paras,     # header paragraphs
        0,             # min alloc
        0xFFFF,        # max alloc
        real_ss,       # SS
        real_sp,       # SP
        0,             # checksum
        real_ip,       # IP
        real_cs,       # CS
        28,            # reloc table offset
        0              # overlay
    )

    with open(exe_outfile, 'wb') as f:
        f.write(new_header)
        f.write(reloc_data)
        f.write(b'\x00' * (hdr_bytes - 28 - len(reloc_data)))  # padding
        f.write(bytes(output))

    print(f"Wrote MZ EXE to {exe_outfile}")
    print(f"  Real entry: {real_cs:04X}:{real_ip:04X}")
    print(f"  Real stack: {real_ss:04X}:{real_sp:04X}")

    return output

if __name__ == '__main__':
    unpack_exepack(sys.argv[1], sys.argv[2])
