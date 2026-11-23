"""Key-free HK app-My validation and HK coordinate-item delivery.

Only protocol permutations, radix encodings and the native numeric integrity
formula are present. No QR encryption/signing key or user fixture is included.
Functions keep the supplied data in memory and perform no file or network I/O.
"""

# Public protocol permutation constants, expressed with native 1-based offsets.
# These swaps are format obfuscation, not encryption-key material.
_SWAPS = (
    ((41, 58), (39, 37), (3, 19), (40, 50), (46, 98), (55, 17), (112, 80), (105, 69), (111, 87), (43, 10), (38, 84), (117, 6), (108, 99), (8, 56), (116, 30), (12, 51), (13, 63), (86, 49), (91, 32), (61, 21)),
    ((98, 45), (6, 30), (43, 29), (100, 14), (3, 10), (13, 115), (70, 74), (50, 83), (91, 42), (68, 89), (52, 73), (71, 55), (66, 87), (105, 113), (110, 97), (11, 90), (62, 38), (107, 20), (85, 35), (18, 25)),
    ((50, 76), (70, 58), (101, 63), (57, 25), (97, 69), (67, 6), (22, 68), (26, 109), (88, 79), (83, 66), (18, 107), (110, 35), (54, 82), (62, 64), (44, 39), (15, 108), (8, 40), (23, 48), (36, 4), (104, 45)),
    ((68, 55), (28, 104), (84, 64), (96, 116), (79, 83), (20, 8), (107, 112), (100, 77), (37, 53), (14, 26), (9, 69), (105, 111), (117, 6), (81, 35), (72, 45), (41, 51), (71, 70), (90, 39), (99, 38), (21, 78)),
    ((90, 32), (26, 57), (18, 17), (73, 46), (61, 76), (67, 66), (111, 20), (96, 119), (54, 7), (55, 14), (107, 34), (81, 80), (4, 58), (75, 33), (10, 118), (116, 60), (48, 85), (69, 30), (56, 92), (49, 113)),
    ((39, 103), (12, 8), (37, 53), (42, 3), (66, 112), (14, 27), (34, 48), (33, 110), (7, 76), (29, 9), (13, 43), (74, 99), (88, 50), (97, 114), (85, 87), (41, 31), (109, 19), (6, 101), (58, 63), (68, 119)),
    ((26, 74), (12, 7), (25, 100), (62, 115), (89, 66), (30, 43), (82, 83), (46, 103), (81, 27), (56, 76), (32, 36), (10, 73), (63, 21), (49, 38), (119, 64), (3, 104), (57, 84), (101, 19), (117, 58), (45, 52)),
    ((9, 45), (114, 40), (92, 62), (78, 80), (32, 31), (49, 63), (65, 119), (37, 12), (85, 23), (57, 68), (58, 7), (46, 61), (98, 60), (30, 91), (83, 88), (50, 79), (28, 10), (11, 5), (77, 89), (64, 101)),
    ((112, 47), (38, 6), (117, 107), (9, 62), (11, 49), (85, 36), (63, 72), (91, 75), (90, 88), (73, 37), (74, 77), (96, 33), (103, 23), (60, 53), (20, 64), (27, 66), (50, 3), (71, 70), (41, 14), (51, 4)),
    ((41, 49), (68, 77), (39, 118), (55, 45), (65, 40), (90, 19), (112, 60), (85, 33), (113, 117), (98, 17), (97, 102), (64, 106), (3, 72), (116, 43), (57, 4), (56, 47), (109, 36), (70, 82), (67, 30), (9, 86)),
)


def read_radix255(body, offset, width):
    """Read the native nonzero-byte radix-255 numeric encoding."""
    if type(offset) is not int or type(width) is not int or offset < 0 or width <= 0:
        raise ValueError("Invalid numeric field")
    part = body[offset:offset + width]
    if len(part) != width or any(value == 0 for value in part):
        raise ValueError("Invalid numeric field")
    value = 0
    for digit in part:
        value = value * 255 + digit - 1
    return value


def _write_radix255(body, offset, width, value):
    if type(value) is not int or not 0 <= value < 255 ** width:
        raise ValueError("Numeric field is out of range")
    for index in range(offset + width - 1, offset - 1, -1):
        body[index] = value % 255 + 1
        value //= 255


def decode_app_my(raw):
    """Return the validated unshuffled 121-byte v3 HK app My body.

    The returned bytes retain the original profile privately in memory.
    This function neither changes purpose nor interprets user identifiers.
    """
    if not isinstance(raw, (bytes, bytearray, memoryview)):
        raise ValueError("Invalid HK My data")
    body = bytearray(raw)
    if len(body) != 121 or 0 in body:
        raise ValueError("Invalid HK My length or encoding")
    selector = body[58] - 2
    family = (body[selector] - 1) % 10 if 92 <= selector <= 94 else 0
    for left, right in reversed(_SWAPS[family]):
        body[left - 1], body[right - 1] = body[right - 1], body[left - 1]
    if body[119] != sum(body[:60]) % 255 + 1 or body[120] != sum(body[60:119]) % 255 + 1:
        raise ValueError("Invalid HK My checksum")
    if bytes(body[:4]) != b"UI\x04\x03":
        raise ValueError("Unsupported HK My format")
    # Validate all four native item-ID/auxiliary fields explicitly.
    for offset in (4, 7, 10, 13):
        read_radix255(body, offset, 2)
        read_radix255(body, offset + 2, 1)
    return bytes(body)


def coordinate_checksum(seed):
    """Native 80D0E0: first 31-bit MT-style result modulo 10000, plus 3517.

    The original seed recurrence and signed shifts differ from random.Random
    and ordinary 32-bit MT19937. Only the first generated value is required.
    """
    if type(seed) is not int or not 0 <= seed <= 0xFFFFFFFF:
        raise ValueError("Invalid checksum seed")
    state0 = seed & 0x7FFFFFFF
    value = state0
    state1 = None
    for index in range(1, 398):
        value = (index - (value ^ (value >> 30)) * 0x13F8769B) & 0x7FFFFFFF
        if index == 1:
            state1 = value
    # Native first twist: the state0 high bit is zero after 31-bit seeding.
    value = (value ^ (state1 >> 1) ^ ((state1 & 1) * 0x1908B0DF)) & 0x7FFFFFFF
    value ^= value >> 11
    value = (value ^ ((value & 0xFF3A58AD) << 7)) & 0xFFFFFFFF
    value = (value ^ ((value & 0xFFFFDF8C) << 15)) & 0xFFFFFFFF
    signed = value if value < 0x80000000 else value - 0x100000000
    value = (value ^ (signed >> 18)) & 0x7FFFFFFF
    return value % 10000 + 3517


def build_coordinate_item(item, aux=0):
    """Build a normalized 43-byte HK UI v1/type10 body for native mode1.

    This is the Reader's already-decoded body, not an encrypted printable QR.
    It must be sent with mode1. HK magic0x53F4 is accepted independently of the
    game's alternate-region marker selector. Reserved bytes are radix-zero.
    """
    if type(item) is not int or not 1 <= item < 255 ** 2:
        raise ValueError("Item ID is out of range")
    if type(aux) is not int or not 0 <= aux < 255 ** 2:
        raise ValueError("Item auxiliary value is out of range")
    body = bytearray([1] * 43)
    body[:4] = b"UI\x02\x0b"
    _write_radix255(body, 9, 2, item)
    _write_radix255(body, 13, 2, aux)
    _write_radix255(body, 39, 4, coordinate_checksum(item + aux))
    return bytes(body)
