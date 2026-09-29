"""Small dependency-free LZ4 block decoder for Tuya vacuum map payloads.

Based on Jan Kneschke's MIT-licensed pure-Python LZ4 block decoder:
https://github.com/jaidenlabelle/tuya-vacuum/blob/main/tuya_vacuum/lz4.py

Copyright 2016, 2020 Jan Kneschke <jan@kneschke.de>
SPDX-License-Identifier: MIT
"""
from __future__ import annotations


class LZ4BlockError(ValueError):
    """The input is not a valid raw LZ4 block."""


def _read_length(source: bytes, position: int, length: int) -> tuple[int, int]:
    if length != 0x0F:
        return length, position
    while True:
        if position >= len(source):
            raise LZ4BlockError("unexpected end of LZ4 length")
        part = source[position]
        position += 1
        length += part
        if part != 0xFF:
            return length, position


def decompress_block(source: bytes, expected_size: int | None = None) -> bytes:
    """Decompress one raw LZ4 block and optionally validate its output size."""
    position = 0
    output = bytearray()

    while position < len(source):
        token = source[position]
        position += 1
        literal_length, position = _read_length(source, position, token >> 4)
        literal_end = position + literal_length
        if literal_end > len(source):
            raise LZ4BlockError("truncated LZ4 literal data")
        output.extend(source[position:literal_end])
        position = literal_end

        # The final LZ4 sequence contains literals only and no match offset.
        if position == len(source):
            if token & 0x0F:
                raise LZ4BlockError("final LZ4 sequence has a match length but no offset")
            break
        if position + 2 > len(source):
            raise LZ4BlockError("truncated LZ4 match offset")
        offset = source[position] | (source[position + 1] << 8)
        position += 2
        if offset == 0 or offset > len(output):
            raise LZ4BlockError(f"invalid LZ4 match offset: {offset}")

        match_length, position = _read_length(source, position, token & 0x0F)
        match_length += 4
        if expected_size is not None and len(output) + match_length > expected_size:
            raise LZ4BlockError("LZ4 output exceeds the declared size")
        for _ in range(match_length):
            output.append(output[-offset])

    if expected_size is not None and len(output) != expected_size:
        raise LZ4BlockError(
            f"LZ4 output size {len(output)} does not match declared {expected_size}"
        )
    return bytes(output)
