"""
Scan a folder of SRP fast_lane*.ai files and find which one is closest
to where your car is currently positioned.

Usage (while AC is running):
    python find_spline.py "C:\\path\\to\\fast_lane folder" [--copy]

    --copy   automatically convert + copy the winner to data/fast_lane.ai

Or if AC isn't running, pass position manually:
    python find_spline.py "C:\\path\\to\\folder" --pos 1234.5 -678.9
"""

import struct
import math
import sys
import os
import mmap
import ctypes
import shutil


# ── Shared memory helpers ──────────────────────────────────────────────────────

def _open_mmap(name, size):
    """Open a named Windows shared memory and return bytes, or None."""
    try:
        FILE_MAP_READ = 4
        OpenFileMapping = ctypes.windll.kernel32.OpenFileMappingW
        OpenFileMapping.restype = ctypes.c_void_p
        MapViewOfFile = ctypes.windll.kernel32.MapViewOfFile
        MapViewOfFile.restype = ctypes.c_void_p
        UnmapViewOfFile = ctypes.windll.kernel32.UnmapViewOfFile
        CloseHandle = ctypes.windll.kernel32.CloseHandle

        h = OpenFileMapping(FILE_MAP_READ, False, name)
        if not h:
            return None
        view = MapViewOfFile(h, FILE_MAP_READ, 0, 0, size)
        if not view:
            CloseHandle(h)
            return None
        buf = (ctypes.c_char * size).from_address(view)
        data = bytes(buf)
        UnmapViewOfFile(view)
        CloseHandle(h)
        return data
    except Exception:
        return None


# ── Read car position ──────────────────────────────────────────────────────────

def read_car_pos():
    """
    Returns (x, z) of the player car, or None.
    Tries CSP Car0.v0 first, then AC's acpmf_graphics (always available).
    """
    # 1. CSP mmap (only exists after bot creates CarControls0.v0)
    data = _open_mmap("Car0.v0", 672)
    if data:
        x, y, z = struct.unpack_from('<fff', data, 88)  # CarData.position @ 88
        print(f"[source] Car0.v0")
        return (x, z)

    # 2. AC built-in graphics shared memory (always present when AC is running)
    # SPageFileGraphics layout (wchar_t strings → 2 bytes/char on Windows):
    #   0   packetId        int
    #   4   status          int
    #   8   session         int
    #  12   currentTime     wchar_t[15] = 30 bytes
    #  42   lastTime        wchar_t[15] = 30 bytes
    #  72   bestTime        wchar_t[15] = 30 bytes
    # 102   split           wchar_t[15] = 30 bytes
    # 132   completedLaps   int
    # 136   position        int
    # 140   iCurrentTime    int
    # 144   iLastTime       int
    # 148   iBestTime       int
    # 152   sessionTimeLeft float
    # 156   distanceTraveled float
    # 160   isInPit         int
    # 164   currentSectorIndex int
    # 168   lastSectorTime  int
    # 172   numberOfLaps    int
    # 176   tyreCompound    wchar_t[4][33] = 264 bytes
    # 440   replayTimeMultiplier float
    # 444   normalizedCarPosition float
    # 448   activeCars      int
    # 452   carCoordinates  float[60][3]  ← player = [0]
    data = _open_mmap("acpmf_graphics", 2048)
    if data:
        x, y, z = struct.unpack_from('<fff', data, 452)  # carCoordinates[0]
        print(f"[source] acpmf_graphics (AC built-in)")
        return (x, z)

    return None


# ── Parse .ai files ────────────────────────────────────────────────────────────

def parse_ai(path):
    """Returns list of (x, z) tuples from an .ai file (v7 or v-1)."""
    try:
        with open(path, 'rb') as f:
            data = f.read()

        if len(data) < 8:
            return []

        version, = struct.unpack_from('<i', data, 0)

        if version == 7:
            count, = struct.unpack_from('<i', data, 4)
            pts = []
            off = 16
            for _ in range(count):
                if off + 20 > len(data):
                    break
                x, y, z, length = struct.unpack_from('<ffff', data, off)
                pts.append((x, z))
                off += 20
            return pts

        elif version == -1:
            count, = struct.unpack_from('<i', data, 4)
            pts = []
            off = 8
            for _ in range(count):
                if off + 20 > len(data):
                    break
                x, y, z = struct.unpack_from('<fff', data, off)
                pts.append((x, z))
                off += 20
            return pts

        else:
            return []

    except Exception:
        return []


def min_dist(pts, cx, cz):
    """Minimum 2D distance from (cx, cz) to any point in pts."""
    if not pts:
        return float('inf')
    best = float('inf')
    for x, z in pts:
        d = math.sqrt((x - cx) ** 2 + (z - cz) ** 2)
        if d < best:
            best = d
    return best


# ── Convert v-1 to v7 in memory ───────────────────────────────────────────────

def convert_vn1_to_v7(pts_xz_raw):
    """pts_xz_raw is list of (x, y, z) from v-1 file. Returns v7 bytes."""
    pts = [{'x': x, 'y': y, 'z': z} for x, y, z in pts_xz_raw]
    N = len(pts)
    out = bytearray()
    out += struct.pack('<i', 7)
    out += struct.pack('<i', N)
    out += struct.pack('<i', 0)
    out += struct.pack('<i', 0)
    for i, p in enumerate(pts):
        nxt = pts[(i + 1) % N]
        length = math.sqrt((nxt['x']-p['x'])**2 +
                           (nxt['y']-p['y'])**2 +
                           (nxt['z']-p['z'])**2)
        out += struct.pack('<fff', p['x'], p['y'], p['z'])
        out += struct.pack('<f', length)
        out += struct.pack('<i', i)
    out += struct.pack('<i', 0)  # no extras
    return bytes(out)


def parse_ai_xyz(path):
    """Returns list of (x, y, z) tuples (needed for conversion)."""
    try:
        with open(path, 'rb') as f:
            data = f.read()
        if len(data) < 8:
            return None, None
        version, = struct.unpack_from('<i', data, 0)
        if version == 7:
            count, = struct.unpack_from('<i', data, 4)
            pts = []
            off = 16
            for _ in range(count):
                if off + 20 > len(data):
                    break
                x, y, z, _ = struct.unpack_from('<ffff', data, off)
                pts.append((x, y, z))
                off += 20
            return 7, pts
        elif version == -1:
            count, = struct.unpack_from('<i', data, 4)
            pts = []
            off = 8
            for _ in range(count):
                if off + 20 > len(data):
                    break
                x, y, z = struct.unpack_from('<fff', data, off)
                pts.append((x, y, z))
                off += 20
            return -1, pts
        else:
            return None, None
    except Exception:
        return None, None


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]

    if not args or args[0] in ('-h', '--help'):
        print(__doc__)
        sys.exit(0)

    folder = args[0]
    do_copy = '--copy' in args

    # Car position: from mmap or --pos x z
    cx, cz = None, None
    if '--pos' in args:
        idx = args.index('--pos')
        cx = float(args[idx + 1])
        cz = float(args[idx + 2])
        print(f"Using manual position: x={cx:.1f}  z={cz:.1f}")
    else:
        pos = read_car_pos()
        if pos:
            cx, cz = pos
            print(f"Car0.v0 position: x={cx:.1f}  z={cz:.1f}")
        else:
            print("ERROR: AC is not running (Car0.v0 not found).")
            print("Either start AC or pass --pos <x> <z>")
            sys.exit(1)

    # Scan folder
    if not os.path.isdir(folder):
        print(f"ERROR: folder not found: {folder}")
        sys.exit(1)

    files = sorted(f for f in os.listdir(folder) if f.endswith('.ai'))
    if not files:
        print("No .ai files found in folder")
        sys.exit(1)

    print(f"\nScanning {len(files)} files in: {folder}\n")
    print(f"{'File':<30}  {'Points':>7}  {'Min dist (m)':>13}")
    print("-" * 56)

    results = []
    for fname in files:
        path = os.path.join(folder, fname)
        pts = parse_ai(path)
        if not pts:
            continue
        d = min_dist(pts, cx, cz)
        results.append((d, fname, len(pts)))
        marker = " <-- BEST" if not results[1:] else ""
        print(f"{fname:<30}  {len(pts):>7}  {d:>13.2f}{marker}")

    # Sort and show top 5
    results.sort(key=lambda r: r[0])
    print("\n── Top 5 closest ──")
    for i, (d, fname, n) in enumerate(results[:5]):
        print(f"  {i+1}. {fname}  ({n} pts, {d:.2f} m away)")

    winner_fname = results[0][1]
    winner_d = results[0][0]
    print(f"\nWinner: {winner_fname}  (min dist = {winner_d:.2f} m)")

    if winner_d > 50:
        print("WARNING: winner is still >50 m away — wrong folder or no SRP AI pack loaded?")

    if do_copy:
        src = os.path.join(folder, winner_fname)
        # Find data/ dir relative to this script
        script_dir = os.path.dirname(os.path.abspath(__file__))
        data_dir = os.path.join(script_dir, '..', 'data')
        os.makedirs(data_dir, exist_ok=True)
        dst = os.path.join(data_dir, 'fast_lane.ai')

        version, pts_xyz = parse_ai_xyz(src)
        if version == -1:
            print(f"Converting {winner_fname} (v-1 -> v7)...")
            out = convert_vn1_to_v7(pts_xyz)
            with open(dst, 'wb') as f:
                f.write(out)
            print(f"Converted & copied -> {dst}")
        elif version == 7:
            shutil.copy2(src, dst)
            print(f"Copied (already v7) -> {dst}")
        else:
            print("Could not parse winner file for copying")


if __name__ == '__main__':
    main()
