"""
Convert AssettoServer v-1 fast_lane.ai to native AC v7 format.

Usage:
    python convert_aip.py <input.ai> <output.ai>

Steps:
    1. Rename your fast_lane.aip -> fast_lane.zip
    2. Extract fast_lane.ai from the zip
    3. Run: python convert_aip.py fast_lane.ai fast_lane_converted.ai
    4. Copy fast_lane_converted.ai -> <repo>/data/fast_lane.ai
"""

import struct
import math
import sys


def read_v7(data):
    version, = struct.unpack_from('<i', data, 0)
    if version == 7:
        count, _, _ = struct.unpack_from('<iii', data, 4)
        points = []
        off = 16
        for i in range(count):
            x, y, z, length = struct.unpack_from('<ffff', data, off)
            points.append({'x': x, 'y': y, 'z': z})
            off += 20
        print(f"Input is already v7 ({count} points) — no conversion needed")
        return None
    return None


def read_vn1(data):
    version, = struct.unpack_from('<i', data, 0)
    if version != -1:
        print(f"Unknown format: version={version}")
        sys.exit(1)
    count, = struct.unpack_from('<i', data, 4)
    points = []
    off = 8
    for _ in range(count):
        x, y, z, radius, camber_enc = struct.unpack_from('<5f', data, off)
        camber = abs(camber_enc)
        direction = 1.0 if camber_enc >= 0 else -1.0
        points.append({'x': x, 'y': y, 'z': z,
                       'radius': radius, 'camber': camber, 'direction': direction})
        off += 20
    return points


def write_v7(points):
    N = len(points)
    out = bytearray()

    # Header
    out += struct.pack('<i', 7)   # version
    out += struct.pack('<i', N)   # detailCount
    out += struct.pack('<i', 0)   # lapTime
    out += struct.pack('<i', 0)   # sampleCount

    # Section 1: basic points (20 bytes each)
    for i, p in enumerate(points):
        nxt = points[(i + 1) % N]
        length = math.sqrt((nxt['x']-p['x'])**2 +
                           (nxt['y']-p['y'])**2 +
                           (nxt['z']-p['z'])**2)
        out += struct.pack('<fff', p['x'], p['y'], p['z'])
        out += struct.pack('<f',   length)
        out += struct.pack('<i',   i)

    # Section 2: extra data (72 bytes each)
    out += struct.pack('<i', N)
    for p in points:
        # Compute a rough forward vector from position delta
        out += struct.pack('<f', 0.0)              # speed
        out += struct.pack('<f', 0.0)              # gas
        out += struct.pack('<f', 0.0)              # brake
        out += struct.pack('<f', 0.0)              # obsoleteLatG
        out += struct.pack('<f', p['radius'])      # radius
        out += struct.pack('<f', 0.0)              # sideLeft
        out += struct.pack('<f', 0.0)              # sideRight
        out += struct.pack('<f', p['camber'])      # camber
        out += struct.pack('<f', p['direction'])   # direction
        out += struct.pack('<fff', 0.0, 1.0, 0.0) # normal (up)
        out += struct.pack('<f', 0.0)              # detailLength
        out += struct.pack('<fff', 0.0, 0.0, 1.0) # forward
        out += struct.pack('<f', 0.0)              # tag
        out += struct.pack('<f', 0.0)              # grade

    return bytes(out)


def main():
    if len(sys.argv) != 3:
        print("Usage: python convert_aip.py <input.ai> <output.ai>")
        sys.exit(1)

    with open(sys.argv[1], 'rb') as f:
        data = f.read()

    version, = struct.unpack_from('<i', data, 0)
    if version == 7:
        print("File is already v7 native AC format — copy it directly, no conversion needed.")
        sys.exit(0)

    if version != -1:
        print(f"Unrecognised format version: {version}")
        sys.exit(1)

    points = read_vn1(data)
    out = write_v7(points)

    with open(sys.argv[2], 'wb') as f:
        f.write(out)

    print(f"Converted {len(points)} points -> {sys.argv[2]}")


if __name__ == '__main__':
    main()
