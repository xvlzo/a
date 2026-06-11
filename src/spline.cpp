#include "spline.h"
#include <fstream>
#include <algorithm>

// fast_lane.ai v7 binary layout
// Header:  version(i32) point_count(i32) lap_time(i32) sample_count(i32) = 16 bytes
// Points:  N × { x(f32) y(f32) z(f32) length(f32) id(i32) }             = N×20 bytes
// Extras:  extra_count(i32), then M × 72 bytes:
//   speed gas brake obs_latg radius side_l side_r camber dir_angle (9×f32)
//   normal (3×f32), length2 (f32), forward (3×f32), tag grade (2×f32)

static void readF32(std::ifstream& f, float& v) { f.read(reinterpret_cast<char*>(&v), 4); }
static void readI32(std::ifstream& f, int32_t& v) { f.read(reinterpret_cast<char*>(&v), 4); }

bool Spline::load(const std::string& path) {
    std::ifstream f(path, std::ios::binary);
    if (!f) return false;

    int32_t version, point_count, lap_time, sample_count;
    readI32(f, version);
    readI32(f, point_count);
    readI32(f, lap_time);
    readI32(f, sample_count);

    if (point_count <= 0 || point_count > 200000) return false;

    pts.resize(point_count);
    for (auto& p : pts) {
        readF32(f, p.x);
        readF32(f, p.y);
        readF32(f, p.z);
        readF32(f, p.arc_length);
        readI32(f, p.id);
    }

    int32_t extra_count = 0;
    readI32(f, extra_count);
    if (extra_count > 0 && extra_count <= point_count) {
        ext.resize(extra_count);
        for (auto& e : ext) {
            float speed, gas, brake, obs_latg, radius, side_l, side_r, camber, dir_angle;
            float nx, ny, nz, length2, fx, fy, fz, tag, grade;
            readF32(f, speed);   readF32(f, gas);   readF32(f, brake);
            readF32(f, obs_latg); readF32(f, radius);
            readF32(f, side_l);  readF32(f, side_r);
            readF32(f, camber);  readF32(f, dir_angle);
            readF32(f, nx);  readF32(f, ny);  readF32(f, nz);
            readF32(f, length2);
            readF32(f, fx);  readF32(f, fy);  readF32(f, fz);
            readF32(f, tag); readF32(f, grade);
            e.speed    = speed;
            e.radius   = radius;
            e.side_left  = side_l;
            e.side_right = side_r;
            e.nx = nx; e.ny = ny; e.nz = nz;
            e.fx = fx; e.fy = fy; e.fz = fz;
            e.grade = grade;
        }
    }

    if (!pts.empty())
        total_length = pts.back().arc_length;

    buildHeadings();
    return true;
}

void Spline::reverse() {
    std::reverse(pts.begin(), pts.end());
    // Recompute cumulative arc lengths from scratch
    float cum = 0.f;
    for (int i = 0; i < (int)pts.size(); ++i) {
        pts[i].arc_length = cum;
        if (i + 1 < (int)pts.size()) {
            float dx = pts[i+1].x - pts[i].x;
            float dz = pts[i+1].z - pts[i].z;
            cum += std::sqrt(dx*dx + dz*dz);
        }
    }
    total_length = cum;
    ext.clear(); // extras are no longer valid after reversal
    buildHeadings();
}

void Spline::buildHeadings() {
    int N = size();
    headings.resize(N);
    if (N == 0) return;

    if (!ext.empty() && (int)ext.size() == N) {
        for (int i = 0; i < N; ++i)
            headings[i] = std::atan2(ext[i].fx, ext[i].fz);
    } else {
        for (int i = 0; i < N - 1; ++i) {
            float dx = pts[i+1].x - pts[i].x;
            float dz = pts[i+1].z - pts[i].z;
            headings[i] = std::atan2(dx, dz);
        }
        headings[N-1] = headings[N-2];
    }
}

void Spline::projectToSegment(float px, float pz,
                               float ax, float az,
                               float bx, float bz,
                               float& t, float& cx, float& cz) {
    float abx = bx - ax, abz = bz - az;
    float apx = px - ax, apz = pz - az;
    float ab2 = abx*abx + abz*abz;
    if (ab2 < 1e-9f) { t = 0.f; cx = ax; cz = az; return; }
    t = (apx*abx + apz*abz) / ab2;
    t = t < 0.f ? 0.f : (t > 1.f ? 1.f : t);
    cx = ax + t*abx;
    cz = az + t*abz;
}

float Spline::signedOffset(float px, float pz,
                            float ax, float az,
                            float bx, float bz) {
    float abx = bx - ax, abz = bz - az;
    float apx = px - ax, apz = pz - az;
    float len = std::sqrt(abx*abx + abz*abz);
    if (len < 1e-9f) return 0.f;
    // cross product (AB × AP) in XZ → positive = LEFT of forward
    return (abx*apz - abz*apx) / len;
}

FrenetState Spline::project(float wx, float wz, int hint_idx, int window) const {
    int N = size();
    if (N < 2) return {};

    // Coarse: nearest point in window — wraps at start/finish line
    float best_d2 = 1e30f;
    int best_i = ((hint_idx % N) + N) % N;
    for (int di = -window; di <= window; ++di) {
        int i = ((hint_idx + di) % N + N) % N;
        float dx = pts[i].x - wx, dz = pts[i].z - wz;
        float d2 = dx*dx + dz*dz;
        if (d2 < best_d2) { best_d2 = d2; best_i = i; }
    }

    // Fine: segment projection (wrap last segment back to index 0)
    int i0 = best_i;
    int i1 = (i0 + 1) % N;

    float t, cx, cz;
    projectToSegment(wx, wz, pts[i0].x, pts[i0].z, pts[i1].x, pts[i1].z, t, cx, cz);

    float s1 = (i1 == 0) ? total_length : pts[i1].arc_length;
    float s   = pts[i0].arc_length + t * (s1 - pts[i0].arc_length);
    if (s >= total_length) s -= total_length;

    float d = signedOffset(wx, wz, pts[i0].x, pts[i0].z, pts[i1].x, pts[i1].z);
    float h = headings[i0];

    return { s, d, h, i0 };
}

void Spline::frenetToWorld(float s, float d, float& wx, float& wz) const {
    int N = size();
    if (N < 2) { wx = wz = 0; return; }

    // Binary search for segment containing s
    int lo = 0, hi = N - 2;
    while (lo < hi) {
        int mid = (lo + hi) / 2;
        if (pts[mid].arc_length <= s) lo = mid + 1;
        else hi = mid;
    }
    int i0 = std::max(0, lo - 1);
    int i1 = i0 + 1;

    float seg_len = pts[i1].arc_length - pts[i0].arc_length;
    float t = seg_len > 1e-9f ? (s - pts[i0].arc_length) / seg_len : 0.f;
    t = t < 0.f ? 0.f : (t > 1.f ? 1.f : t);

    // Interpolated centre-line position
    float cx = pts[i0].x + t * (pts[i1].x - pts[i0].x);
    float cz = pts[i0].z + t * (pts[i1].z - pts[i0].z);

    // Heading (interpolated)
    float h = headings[i0];

    // Left normal: CCW 90° from forward (sin(h), cos(h)) → (-cos(h), sin(h))
    float lx = -std::cos(h);
    float lz =  std::sin(h);

    wx = cx + d * lx;
    wz = cz + d * lz;
}

void Spline::getWidths(int idx, float& left, float& right) const {
    if (idx < 0 || idx >= (int)ext.size()) {
        left = right = 5.f;
        return;
    }
    left  = ext[idx].side_left;
    right = ext[idx].side_right;
}

