#pragma once
#include <vector>
#include <string>
#include <cmath>

// fast_lane.ai v7 parser + Frenet frame math.
// AC world space: X = right, Y = up, Z = forward.
// Frenet convention: d > 0 = LEFT of direction of travel.

struct SplinePoint {
    float x, y, z;
    float arc_length;   // cumulative from start (m)
    int   id;
};

struct SplineExtra {
    float speed;        // optimal speed (m/s)
    float radius;       // curvature radius (m)
    float side_left;    // track width to the LEFT  (m, d > 0 direction)
    float side_right;   // track width to the RIGHT (m, d < 0 direction)
    float nx, ny, nz;   // road normal
    float fx, fy, fz;   // road forward unit vector
    float grade;
};

struct FrenetState {
    float s;            // along-track distance (m)
    float d;            // lateral offset (m, + = left)
    float road_heading; // road tangent heading (radians)
    int   idx;          // nearest spline index
};

class Spline {
public:
    std::vector<SplinePoint> pts;
    std::vector<SplineExtra> ext;
    float total_length = 0.f;

    // Precomputed for fast lookup
    std::vector<float> headings;  // per-point road heading (rad)

    bool load(const std::string& path);
    void buildHeadings();
    void reverse(); // flip point order + rebuild — call if spline runs opposite to car

    // Project world (x, z) onto spline. Returns FrenetState.
    // hint_idx: start search near this index (avoids full scan)
    FrenetState project(float wx, float wz, int hint_idx = 0, int window = 80) const;

    // Convert Frenet (s, d) → world (x, z)
    void frenetToWorld(float s, float d, float& wx, float& wz) const;

    // Track half-widths at index (left, right)
    void getWidths(int idx, float& left, float& right) const;

    int size() const { return static_cast<int>(pts.size()); }

private:
    // Segment-level closest point: returns (t, cx, cz)
    static void projectToSegment(float px, float pz,
                                 float ax, float az,
                                 float bx, float bz,
                                 float& t, float& cx, float& cz);

    // Signed lateral offset of (px,pz) from segment (ax,az)→(bx,bz)
    // + = left of forward direction
    static float signedOffset(float px, float pz,
                               float ax, float az,
                               float bx, float bz);
};
