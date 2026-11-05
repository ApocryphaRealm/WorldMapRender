"""World Map Render: orthographic top-down pictures of a worldspace, from the list's own LOD files.

Reads xLODGen Output (terrain and water, WIN season), DynDOLOD Output (objects, trees, atlases) and draws them straight
down on the GPU: x and y are world coordinates, z is only the depth test. Two pictures come out of one pass, day and
night. Nothing is written into the modlist.

    render_map.py --world Tamriel --rect -16 -28 32 20 --ppc 64 --out-day day.png --out-night night.png

--rect is x0 y0 x1 y1 in cells; --ppc is pixels per cell (a cell is 4096 units). --raw writes .npy for dds conversion.
"""
import argparse
import glob
import io
import json
import os
import re
import sys
import time

import moderngl
import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import bsa as bsa_mod  # noqa: E402
import lanterns as lanterns_mod  # noqa: E402
import land_direct  # noqa: E402
import markers as markers_mod  # noqa: E402
import roads as roads_mod  # noqa: E402
import nifread  # noqa: E402

CELL = 4096.0
ZMIN, ZMAX = -60000.0, 120000.0
MODS = r"D:\Modlists\Njordlinger\mods"
PROFILE = os.path.join(r"D:\Modlists\Njordlinger\profiles", os.environ.get("WMR_PROFILE", "Njordlinger Test"), "modlist.txt")
MARKER_POOLS = False  # True brings back a pool of light at settlement map markers. Off: they show sites with no lights and give away undiscovered places.

VS = """
#version 330
uniform vec4 rect;      // x0 y0 x1 y1 in world units
uniform vec3 origin;
in vec3 in_pos;
in vec2 in_uv;
in vec4 in_col;
out vec3 vW;
out vec2 vUV;
out vec4 vC;
void main() {
    vec3 w = in_pos + origin;
    vW = w; vUV = in_uv; vC = in_col;
    float depth = 1.0 - 2.0 * (w.z - (%f)) / (%f);
    gl_Position = vec4(2.0 * (w.x - rect.x) / (rect.z - rect.x) - 1.0,
                       2.0 * (w.y - rect.y) / (rect.w - rect.y) - 1.0, depth, 1.0);
}
""" % (ZMIN, ZMAX - ZMIN)

LIGHT = """
uniform int night;
uniform vec3 ambientDay;
uniform vec3 tone;
uniform sampler2D outsideMask;
uniform vec4 rect;
float outsideAt(vec2 w) { return texture(outsideMask, (w - rect.xy) / (rect.zw - rect.xy)).r; }
const vec3 SNOW = vec3(0.86, 0.89, 0.93);
float hash(vec2 p) { return fract(sin(dot(p, vec2(12.9898, 78.233))) * 43758.5453); }
float noise(vec2 p) {
    vec2 i = floor(p), f = fract(p);
    f = f * f * (3.0 - 2.0 * f);
    return mix(mix(hash(i), hash(i + vec2(1, 0)), f.x), mix(hash(i + vec2(0, 1)), hash(i + vec2(1, 1)), f.x), f.y);
}
// Permanent winter: anything facing up lies under snow, whatever its LOD colour; steep faces keep their own.
uniform float winter;
float snowcover(vec3 n, vec2 w, float lo, float hi) {
    if (winter < 0.5) return 0.0;
    float k = noise(w / 170.0) * 0.5 + noise(w / 900.0) * 0.5;
    return smoothstep(lo, hi, n.z + (k - 0.5) * 0.28);
}
vec3 light(vec3 albedo, vec3 n, float glow) {
    vec3 L = normalize(vec3(-0.55, 0.60, 0.58));
    float d = max(dot(n, L), 0.0);
    if (night == 1) {
        vec3 c = albedo * (vec3(0.20, 0.25, 0.38) + vec3(0.26, 0.31, 0.44) * d);
        return c * tone + glow * vec3(1.0, 0.72, 0.42);
    }
    return albedo * (ambientDay + vec3(0.62, 0.58, 0.50) * d) * tone;
}
"""

FS_LAND = """
#version 330
uniform sampler2D diffuse;
uniform sampler2D normalmap;
uniform sampler2D heights;
uniform sampler2D roadmap;
uniform float upp;
uniform float roadsOn;
uniform float relief;
uniform float voidBelow;
uniform vec3 landColour;  // above 0: the ground keeps its pattern but not its colour, and no brighter than this; for a cave, whose LOD ground is bare dirt
uniform float landSoft;  // 0 on Skyrim. Above it, shading comes from the map's heights: the LOD's normal maps ring on gentle hills
in vec3 vW; in vec2 vUV; in vec4 vC;
out vec4 f;
""" + LIGHT + """
void main() {
    vec3 a = texture(diffuse, vUV).rgb;
    if (landColour.g > 0.0) a = landColour * (0.8 + 0.4 * min(dot(a, vec3(0.333)), 0.5));
    vec3 t = texture(normalmap, vUV).rgb * 2.0 - 1.0;
    vec3 n = normalize(vec3(t.r, t.b, t.g));  // the LOD normal maps store X, Z, Y
    n = normalize(n);
    float outside = outsideAt(vW.xy);
    if (landSoft > 0.0) {
        ivec2 at = ivec2(gl_FragCoord.xy);
        int r = max(4, int(260.0 / upp));
        float hx = texelFetch(heights, at + ivec2(r, 0), 0).r - texelFetch(heights, at - ivec2(r, 0), 0).r;
        float hy = texelFetch(heights, at + ivec2(0, r), 0).r - texelFetch(heights, at - ivec2(0, r), 0).r;
        if (abs(hx) > 2.0e5) hx = 0.0;
        if (abs(hy) > 2.0e5) hy = 0.0;
        vec3 gentle = normalize(vec3(-hx, -hy, 2.0 * float(r) * upp));
        n = normalize(mix(n, gentle, landSoft));
        if (winter > 0.5) a = mix(a, SNOW * 0.97, smoothstep(0.74, 0.90, gentle.z) * landSoft);
    }
    if (outside > 0.0) {
        // Whole-map heights, not the tile's normal map: no seams at tile edges, and the LOD's blocky detail stays out.
        ivec2 at = ivec2(gl_FragCoord.xy);
        int r = max(6, int(420.0 / upp));
        float hx = texelFetch(heights, at + ivec2(r, 0), 0).r - texelFetch(heights, at - ivec2(r, 0), 0).r;
        float hy = texelFetch(heights, at + ivec2(0, r), 0).r - texelFetch(heights, at - ivec2(0, r), 0).r;
        if (abs(hx) > 2.0e5) hx = 0.0;
        if (abs(hy) > 2.0e5) hy = 0.0;
        vec3 soft = normalize(vec3(-hx, -hy, 2.0 * float(r) * upp));
        float k = noise(vW.xy / 1400.0) * 0.6 + noise(vW.xy / 5200.0) * 0.4;
        vec3 surround = mix(vec3(0.27, 0.285, 0.31), SNOW * 0.97, smoothstep(0.62, 0.84, soft.z + (k - 0.5) * 0.30));
        a = mix(a, surround, outside);
        n = normalize(mix(n, soft, outside));
    }
    // LOD ground the winter swap missed (grass, dirt): warm colours go under the snow, grey roads and rock stay.
    float warm = (a.r + a.g) * 0.5 - a.b;
    float cover = smoothstep(0.012, 0.075, warm) * snowcover(n, vW.xy, 0.55, 0.80) * (1.0 - outside);
    a = mix(a, SNOW * 0.97, cover);
    // Roads and paths as they are painted on the land, not as the LOD texture kept them.
    vec4 road = texture(roadmap, (vW.xy - rect.xy) / (rect.zw - rect.xy));
    a = mix(a, road.rgb, roadsOn * smoothstep(0.26, 0.55, road.a) * (1.0 - outside));
    n.xy *= mix(1.6, 1.0, landSoft) * relief;  // the height shows better on a flat picture
    f = vec4(light(a, normalize(n), 0.0), 1.0);
    // A land's unused ground: the flat default height the engine fills cells with, hidden in play. It melts into the map's surround.
    if (voidBelow > -1.0e6) f.rgb = mix(night == 1 ? vec3(0.039, 0.051, 0.078) : vec3(0.129, 0.161, 0.204), f.rgb, smoothstep(voidBelow + 10.0, voidBelow + 140.0, vW.z));
}
"""

# Water is decided per pixel. The LOD's water is one flat square per cell, and xLODGen leaves some sea cells without one, so taken
# as it comes it has square steps at shores and square holes in the sea. Instead every pixel knows the terrain's height (HEIGHT
# pass) and a water level (LEVEL pass: a real square first, then squares grown by a cell, then the world's default water
# height), and is water exactly where the terrain lies below that level. Depth sets the colour, so shores grade into deep water.
FS_HEIGHT = """
#version 330
in vec3 vW; in vec2 vUV; in vec4 vC;
out vec4 f;
void main() { f = vec4(vW.z, 0.0, 0.0, 1.0); }
"""

EFFECT_SHAPE = re.compile(r"effects\\\\|fxwater|fxmist|fxfog|fxsteam|fxcloud", re.I)
ROAD_MODEL = re.compile(r"landscape[/\\]roads[/\\]", re.I)
ROCK = re.compile(r"mountain|rock|cliff|boulder|ice|glacier|snowdrift|landscape", re.I)

VS_FULL_EARLY = """
#version 330
in vec2 in_corner;
void main() { gl_Position = vec4(in_corner, 0.0, 1.0); }
"""

VS_LEVEL = """
#version 330
uniform vec4 rect;
uniform float band;     // 0 a real square, 0.5 a grown one, 1 the world's default level
in vec3 in_pos;
out float vZ;
void main() {
    vZ = in_pos.z;
    float n = clamp((in_pos.z - (%f)) / (%f), 0.0, 1.0);
    gl_Position = vec4(2.0 * (in_pos.x - rect.x) / (rect.z - rect.x) - 1.0,
                       2.0 * (in_pos.y - rect.y) / (rect.w - rect.y) - 1.0, -1.0 + band * 2.0 * 0.5 + n * 0.45, 1.0);
}
""" % (ZMIN, ZMAX - ZMIN)

FS_LEVEL = """
#version 330
uniform float band;
in float vZ;
out vec4 f;
void main() { f = vec4(vZ, band, 0.0, 1.0); }
"""

# A land drawn from its records rather than its LOD: heights and colours come from whole-map images.
FS_LANDIMG_H = """
#version 330
uniform sampler2D landHeight;
uniform vec2 canvas;
out vec4 f;
void main() {
    vec2 uv = gl_FragCoord.xy / canvas;
    float z = texture(landHeight, uv).r;
    if (z < -30000.0) discard;
    f = vec4(z, 0.0, 0.0, 1.0);
    gl_FragDepth = 0.5 * (1.0 - 2.0 * (z - (%f)) / (%f)) + 0.5;
}
""" % (ZMIN, ZMAX - ZMIN)

FS_LANDIMG = """
#version 330
uniform sampler2D landHeight;
uniform sampler2D landColour;
uniform sampler2D roadmap;
uniform vec2 canvas;
uniform float upp;
uniform float roadsOn;
out vec4 f;
""" + LIGHT + """
void main() {
    vec2 uv = gl_FragCoord.xy / canvas;
    vec2 w = rect.xy + uv * (rect.zw - rect.xy);
    vec2 e = vec2(1.0) / vec2(textureSize(landHeight, 0));
    float z = texture(landHeight, uv).r;
    if (z < -30000.0) discard;
    float hx = texture(landHeight, uv + vec2(e.x, 0)).r - texture(landHeight, uv - vec2(e.x, 0)).r;
    float hy = texture(landHeight, uv + vec2(0, e.y)).r - texture(landHeight, uv - vec2(0, e.y)).r;
    vec3 n = normalize(vec3(-hx, -hy, 2.0 * 128.0));
    vec3 a = texture(landColour, uv).rgb;
    a *= 0.96 + 0.08 * noise(w / 90.0);
    float outside = outsideAt(w);
    if (winter > 0.5) {
        // snow by slope and the ground's warmth, as on the other winter lands; steep faces stay rock
        float warm = (a.r + a.g) * 0.5 - a.b;
        a = mix(a, SNOW * 0.97, snowcover(n, w, 0.46, 0.72) * (0.70 + 0.30 * smoothstep(0.0, 0.03, warm)));
    }
    vec4 road = texture(roadmap, uv);
    a = mix(a, road.rgb, roadsOn * smoothstep(0.26, 0.55, road.a));
    f = vec4(light(a, n, 0.0), 1.0);
    gl_FragDepth = 0.5 * (1.0 - 2.0 * (z - (%f)) / (%f)) + 0.5;
}
""".replace("%f", repr(ZMIN), 1).replace("%f", repr(ZMAX - ZMIN), 1)

VS_FULL = """
#version 330
in vec2 in_corner;
void main() { gl_Position = vec4(in_corner, 0.0, 1.0); }
"""

FS_WATER = """
#version 330
uniform sampler2D heights;
uniform sampler2D levels;
uniform int night;
uniform float sea;
uniform float winter;
uniform vec3 shallowDay;
uniform vec3 farDay;
uniform vec3 shallowNight;
uniform vec3 farNight;
uniform int waterStyle;
uniform int useTrue;
uniform int allWater;
uniform sampler2D trueHeight;
uniform vec4 worldRect;
uniform vec2 canvas;
out vec4 f;
float hash(vec2 p) { return fract(sin(dot(p, vec2(12.9898, 78.233))) * 43758.5453); }
float noise(vec2 p) {
    vec2 i = floor(p), g = fract(p);
    g = g * g * (3.0 - 2.0 * g);
    return mix(mix(hash(i), hash(i + vec2(1, 0)), g.x), mix(hash(i + vec2(0, 1)), hash(i + vec2(1, 1)), g.x), g.y);
}
float fbm(vec2 p) {
    float v = 0.0, a = 0.5;
    for (int i = 0; i < 5; i++) { v += a * noise(p); p = p * 2.03 + vec2(17.0, 9.0); a *= 0.5; }
    return v;
}
// Apocrypha's water: swirling greens, oily highlights, dark tentacle-like streaks. Scales are in world units, so they read at map scale.
vec3 apocrypha(vec2 w) {
    vec2 q = vec2(fbm(w / 9000.0), fbm(w / 9000.0 + vec2(5.2, 1.3)));
    float s = fbm(w / 5200.0 + q * 3.0);
    vec3 c = mix(vec3(0.04, 0.17, 0.11), vec3(0.17, 0.42, 0.21), smoothstep(0.25, 0.75, s));
    c = mix(c, vec3(0.02, 0.11, 0.12), smoothstep(0.42, 0.72, q.x));
    float h = fbm(w / 2400.0 + q * 4.0);
    c += vec3(0.34, 0.56, 0.30) * pow(smoothstep(0.60, 0.92, h), 3.0);
    float t = abs(fbm(w / 4200.0 + q * 2.5 + vec2(9.0, 2.0)) - 0.5);
    float line = 1.0 - smoothstep(0.0, 0.016, t);
    float where = smoothstep(0.42, 0.66, noise(w / 15000.0));
    return c * (1.0 - 0.78 * line * where);
}
void main() {
    ivec2 at = ivec2(gl_FragCoord.xy);
    vec2 l = texelFetch(levels, at, 0).rg;
    float level = l.r;
    if (level < -1.0e6) discard;
    float h0 = texelFetch(heights, at, 0).r;
    int q = 4;
    float h1 = (texelFetch(heights, at + ivec2(q, 0), 0).r + texelFetch(heights, at - ivec2(q, 0), 0).r + texelFetch(heights, at + ivec2(0, q), 0).r + texelFetch(heights, at - ivec2(0, q), 0).r) * 0.25;
    float h2 = (texelFetch(heights, at + ivec2(q, q), 0).r + texelFetch(heights, at - ivec2(q, q), 0).r + texelFetch(heights, at + ivec2(q, -q), 0).r + texelFetch(heights, at + ivec2(-q, q), 0).r) * 0.25;
    float d = level - (min(h0, min(h1, h2)) < -1.0e6 ? h0 : h0 * 0.3 + h1 * 0.4 + h2 * 0.3);
    float drawAt = level;
    if (allWater == 1) {
        d = 1000.0;
        drawAt = max(level, h0 + 1.0);
    } else if (useTrue == 1) {
        // The land's own records decide the coast. The LOD's sea bed wanders around the water line in cells with little or no
        // land data, in fans of triangles, and those were the geometric shapes in the sea.
        // A frozen shore creeps a little up the bank, over the dark wet ground the LOD paints at the water line: those were black crescents.
        d = level - texture(trueHeight, gl_FragCoord.xy / canvas).r + (winter > 0.5 ? 60.0 : 0.0);
        if (d <= 0.0) discard;
        drawAt = max(level, h0 + 1.0);
    } else if (d <= 0.0) discard;
    // A grown square only finishes a shore: ground far below its level is a valley beside the lake, not more lake.
    if (l.g > 0.25 && l.g < 0.75 && d > 50.0 && h0 > -1.0e6 && abs(level - sea) > 1.0) discard;
    // Thin on purpose: the sea bed's LOD drops sharply at cells with no land data, and a wide shading would draw them as squares.
    float deep = smoothstep(0.0, 45.0, d);
    vec3 shallow = night == 1 ? shallowNight : shallowDay;
    vec3 far = night == 1 ? farNight : farDay;
    if (waterStyle == 1) {
        vec3 ap = apocrypha(worldRect.xy + gl_FragCoord.xy / canvas * (worldRect.zw - worldRect.xy));
        if (night == 1) ap = ap * vec3(0.36, 0.40, 0.52);
        far = ap;
        shallow = ap;
        deep = 1.0;
    }
    vec3 c = mix(shallow, far, deep);
    float a = mix(0.55, 1.0, deep);
    if (winter > 0.5) {
        // Lakes and rivers freeze inward from the shore; the sea keeps a thin rim. Rim width is water depth, so its edge follows the terrain.
        float rim = level > sea + 50.0 ? 330.0 : 70.0;
        float w = noise(gl_FragCoord.xy / 18.0) * 0.5 + noise(gl_FragCoord.xy / 70.0) * 0.5;
        float ice = 1.0 - smoothstep(rim * 0.55, rim * 0.95, d + (w - 0.5) * rim * 0.7);
        vec3 pale = night == 1 ? vec3(0.17, 0.22, 0.34) : vec3(0.70, 0.79, 0.87);
        c = mix(c, pale, ice);
        a = mix(a, 1.0, ice);
    }
    f = vec4(c, 1.0);
    gl_FragDepth = 0.5 * (1.0 - 2.0 * (drawAt - (%f)) / (%f)) + 0.5;
}
""" % (ZMIN, ZMAX - ZMIN)

FS_OBJ = """
#version 330
uniform sampler2D diffuse;
uniform int alphatest;
uniform sampler2D heights;
uniform float upp;      // world units a pixel
uniform float ceilingGap;  // a cave land's roof: anything this far above the floor is left out, so the map shows the cave
uniform float snowLo;
uniform float snowHi;
uniform float soften;   // for lands whose mountains' LOD is coarse: 0 leaves the facets alone
in vec3 vW; in vec2 vUV; in vec4 vC;
out vec4 f;
""" + LIGHT + """
void main() {
    vec4 t = texture(diffuse, vUV);
    if (alphatest == 1 && t.a < 0.45) discard;
    vec3 n = normalize(cross(dFdx(vW), dFdy(vW)));
    if (n.z < 0.0) n = -n;
    if (ceilingGap > 0.0) {
        float floorZ = texelFetch(heights, ivec2(gl_FragCoord.xy), 0).r;
        if (floorZ > -1.0e6 && vW.z - floorZ > ceilingGap) discard;
    }
    vec3 a = t.rgb * vC.rgb;
    float outside = outsideAt(vW.xy);
    float soft = max(outside, soften);
    float fade = 1.0;
    if (soft > 0.0) {
        ivec2 at = ivec2(gl_FragCoord.xy);
        int r = max(4, int(320.0 / upp));
        float hx = texelFetch(heights, at + ivec2(r, 0), 0).r - texelFetch(heights, at - ivec2(r, 0), 0).r;
        float hy = texelFetch(heights, at + ivec2(0, r), 0).r - texelFetch(heights, at - ivec2(0, r), 0).r;
        if (abs(hx) > 2.0e5) hx = 0.0;
        if (abs(hy) > 2.0e5) hy = 0.0;
        vec3 ground = normalize(vec3(-hx, -hy, 2.0 * float(r) * upp));
        n = normalize(mix(n, ground, soft * 0.85));
        // meets the ground rather than standing on it
        float above = vW.z - texelFetch(heights, at, 0).r;
        fade = mix(1.0, smoothstep(0.0, 260.0, above), soft);
    }
    a = mix(a, SNOW, snowcover(n, vW.xy, snowLo, snowHi) * 0.96);
    if (outside > 0.0) {
        float k = noise(vW.xy / 1400.0) * 0.6 + noise(vW.xy / 5200.0) * 0.4;
        a = mix(a, mix(vec3(0.27, 0.285, 0.31), SNOW * 0.97, smoothstep(0.62, 0.84, n.z + (k - 0.5) * 0.30)), outside);
        fade *= 1.0 - smoothstep(0.30, 0.95, outside);
        if (fade <= 0.01) discard;
    }
    f = vec4(light(a, n, 0.0), fade);
}
"""

# Structures: a height map of buildings and walls only, not rock, so their edges can be drawn without touching the mountains.
FS_STRUCT = """
#version 330
uniform sampler2D diffuse;
uniform int alphatest;
in vec3 vW; in vec2 vUV; in vec4 vC;
out vec4 f;
void main() {
    if (alphatest == 1 && texture(diffuse, vUV).a < 0.45) discard;
    f = vec4(vW.z, 0.0, 0.0, 1.0);
}
"""

# A thin dark line where a structure's height jumps, so white roofs and walls show against white ground.
FS_OUTLINE = """
#version 330
uniform sampler2D structures;
uniform sampler2D heights;
uniform int terrainLines;
uniform float lineScale;
out vec4 f;
float st(ivec2 p) { return texelFetch(structures, p, 0).r; }
float at(ivec2 p) { return max(st(p), texelFetch(heights, p, 0).r); }
void main() {
    ivec2 p = ivec2(gl_FragCoord.xy);
    // Sobel over height. Only where a structure stands in the 3x3, so terrain and rock never draw lines.
    float s = terrainLines == 1 ? 0.0 : -2.0e6;
    for (int y = -1; y <= 1; y++) for (int x = -1; x <= 1; x++) s = max(s, st(p + ivec2(x, y)));
    if (s < -1.0e6) discard;
    float tl = at(p + ivec2(-1, 1)), t = at(p + ivec2(0, 1)), tr = at(p + ivec2(1, 1));
    float l = at(p + ivec2(-1, 0)), r = at(p + ivec2(1, 0));
    float bl = at(p + ivec2(-1, -1)), b = at(p + ivec2(0, -1)), br = at(p + ivec2(1, -1));
    if (min(min(min(tl, t), min(tr, l)), min(min(r, bl), min(b, br))) < -1.0e6) discard;
    float gx = (tr + 2.0 * r + br) - (tl + 2.0 * l + bl);
    float gy = (tl + 2.0 * t + tr) - (bl + 2.0 * b + br);
    float e = smoothstep(150.0, 520.0, sqrt(gx * gx + gy * gy) * lineScale);
    f = vec4(vec3(1.0 - e * 0.68), 1.0);
}
"""

# Road meshes: flat stone, laid over the land before anything else stands on it.
FS_ROADMESH = """
#version 330
uniform int night;
uniform vec3 stone;
in vec3 vW; in vec2 vUV; in vec4 vC;
out vec4 f;
float hash(vec2 p) { return fract(sin(dot(p, vec2(12.9898, 78.233))) * 43758.5453); }
void main() {
    vec3 c = stone * (0.92 + 0.16 * hash(floor(vW.xy / 96.0)));
    f = vec4(night == 1 ? c * vec3(0.40, 0.45, 0.60) : c, 1.0);
}
"""

# Full models placed by a land's references, drawn as instances.
VS_INST = """
#version 330
uniform vec4 rect;
in vec3 in_pos;
in vec2 in_uv;
in vec4 in_col;
in vec3 in_m0;
in vec3 in_m1;
in vec3 in_m2;
in vec3 in_t;
out vec3 vW;
out vec2 vUV;
out vec4 vC;
void main() {
    vec3 w = in_m0 * in_pos.x + in_m1 * in_pos.y + in_m2 * in_pos.z + in_t;
    vW = w; vUV = in_uv; vC = in_col;
    float depth = 1.0 - 2.0 * (w.z - (%f)) / (%f);
    gl_Position = vec4(2.0 * (w.x - rect.x) / (rect.z - rect.x) - 1.0,
                       2.0 * (w.y - rect.y) / (rect.w - rect.y) - 1.0, depth, 1.0);
}
""" % (ZMIN, ZMAX - ZMIN)

# A tree seen from above is a soft dome in the billboard's own colour.
VS_TREE = """
#version 330
uniform vec4 rect;
in vec2 in_corner;
in vec4 in_inst;     // x y z radius
in vec3 in_rgb;
out vec2 vP; out vec3 vRGB; out float vZ; out float vR;
void main() {
    vP = in_corner; vRGB = in_rgb; vZ = in_inst.z; vR = in_inst.w;
    vec2 w = in_inst.xy + in_corner * in_inst.w;
    float depth = 1.0 - 2.0 * (in_inst.z - (%f)) / (%f);
    gl_Position = vec4(2.0 * (w.x - rect.x) / (rect.z - rect.x) - 1.0,
                       2.0 * (w.y - rect.y) / (rect.w - rect.y) - 1.0, depth, 1.0);
}
""" % (ZMIN, ZMAX - ZMIN)

FS_TREE = """
#version 330
in vec2 vP; in vec3 vRGB; in float vZ; in float vR;
out vec4 f;
""" + LIGHT + """
void main() {
    float r = dot(vP, vP);
    if (r > 1.0) discard;
    vec3 n = normalize(vec3(vP * 0.9, 0.9));
    // the star of a conifer's boughs, seen from above; the dark comes from the billboard's own colour
    float ang = atan(vP.y, vP.x);
    float bough = 0.55 + 0.45 * sin(ang * 7.0 + vR);
    if (r > 0.45 + 0.55 * bough) discard;
    vec3 c = vRGB * vec3(0.42, 0.50, 0.46);
    f = vec4(light(mix(c, SNOW, winter * 0.34 * (1.0 - r)), n, 0.0), 1.0);
}
"""

FS_LIGHTMARK = """
#version 330
in vec2 vP; in vec3 vRGB;
out vec4 f;
void main() {
    float r = dot(vP, vP);
    if (r > 1.0) discard;
    f = vec4(vRGB * (1.0 - r), 1.0);
}
"""

# Where the LOD has lit windows, a warm pool of light at night.
FS_GLOW = """
#version 330
in vec2 vP; in vec3 vRGB;
out vec4 f;
void main() {
    float r = dot(vP, vP);
    if (r > 1.0) discard;
    float a = (1.0 - r);
    f = vec4(vec3(1.0, 0.68, 0.36) * a * min(vRGB.x * 1.7, 1.0), 1.0);
}
"""


def active_mods():
    out = []
    for line in open(PROFILE, encoding="utf-8", errors="replace"):
        line = line.rstrip("\r\n")
        if line.startswith("+"):
            out.append(line[1:])
    return out


class Textures:
    """Finds the winning loose file for a path, highest priority mod first, from one index of the active mods' files."""

    def __init__(self):
        self.mods = active_mods()
        self.index = None

    def _build(self):
        t0 = time.time()
        self.index = {}
        for m in reversed(self.mods):   # highest priority last, so it overwrites
            root = os.path.join(MODS, m)
            for top in ("meshes", "textures"):
                base = os.path.join(root, top)
                if not os.path.isdir(base):
                    continue
                for dp, _, fs in os.walk(base):
                    rel = os.path.relpath(dp, root).lower()
                    for f in fs:
                        self.index[rel + "\\" + f.lower()] = os.path.join(dp, f)
        print("file index", len(self.index), "files", round(time.time() - t0, 1), "s", flush=True)

    def find(self, path):
        if self.index is None:
            self._build()
        rel = re.sub(r"^data[\\/]", "", path.strip().lower().replace("/", "\\"))
        return self.index.get(rel)

    def archived(self, path):
        """Bytes of a texture only the archives hold; None when they do not have it either."""
        if not hasattr(self, "library"):
            self.library = bsa_mod.Library(MODS, self.mods, markers_mod.STOCK, markers_mod.active_plugins())
        rel = re.sub(r"^data[\\/]", "", path.strip().lower().replace("/", "\\"))
        a = self.library.find(rel)
        return a.read(rel) if a else None

    def save(self):
        pass


SRGB_TO_UNORM = {72: 71, 75: 74, 78: 77, 99: 98, 29: 28, 91: 87, 93: 88}


def open_dds(path):
    """Pillow reads the linear DXGI formats only; the bits are the same, so relabel the sRGB ones."""
    d = bytearray(open(path, "rb").read())
    if d[84:88] == b"DX10":
        f = int.from_bytes(d[128:132], "little")
        if f in SRGB_TO_UNORM:
            d[128:132] = SRGB_TO_UNORM[f].to_bytes(4, "little")
    im = Image.open(io.BytesIO(bytes(d)))
    im.load()
    return im


def open_dds_bytes(raw):
    d = bytearray(raw)
    if d[84:88] == b"DX10":
        f = int.from_bytes(d[128:132], "little")
        if f in SRGB_TO_UNORM:
            d[128:132] = SRGB_TO_UNORM[f].to_bytes(4, "little")
    im = Image.open(io.BytesIO(bytes(d)))
    im.load()
    return im


def mean_colour_lookup(tex):
    """A landscape texture's average colour, from the winning loose file or the archives; darkened if pale, so a road on snow reads."""
    seen = {}

    def lookup(fname):
        key = fname.lower()
        if key in seen:
            return seen[key]
        rel = "textures\\" + key
        out = None
        try:
            f = tex.find(rel)
            im = open_dds(f) if f else None
            if im is None:
                raw = tex.archived(rel)
                im = open_dds_bytes(raw) if raw else None
            if im is not None:
                small = im.convert("RGB").resize((8, 8), Image.BOX)
                out = np.asarray(small, "f4").reshape(-1, 3).mean(0) / 255.0
                lum = float(out.mean())
                # keeps the road's own colour, within a range that reads as a road on snow
                if lum > 0.46:
                    out = out * (0.46 / lum)
                elif lum < 0.30:
                    out = out + (0.30 - lum)
                out = out * 0.75 + float(out.mean()) * 0.25
        except Exception as e:
            print("road texture", rel, e, flush=True)
        seen[key] = out
        return out

    return lookup


def raw_colour_lookup(tex):
    """A landscape texture's average colour as it is, for painting a land from its records."""
    seen = {}

    def lookup(fname):
        key = fname.lower()
        if not key:
            return None
        if key in seen:
            return seen[key]
        rel = "textures\\" + key
        out = None
        try:
            f = tex.find(rel)
            im = open_dds(f) if f else None
            if im is None:
                raw = tex.archived(rel)
                im = open_dds_bytes(raw) if raw else None
            if im is not None:
                out = np.asarray(im.convert("RGB").resize((8, 8), Image.BOX), "f4").reshape(-1, 3).mean(0) / 255.0
        except Exception as e:
            print("land texture", rel, e, flush=True)
        seen[key] = out
        return out

    return lookup


def load_texture(ctx, path, mips=True, limit=0, raw=None, wrap=True):
    im = open_dds_bytes(raw) if raw else open_dds(path)
    if im.mode != "RGBA":
        im = im.convert("RGBA")
    if limit and max(im.size) > limit:
        f = max(im.size) // limit
        im = im.reduce(max(f, 1))
    tex = ctx.texture(im.size, 4, im.tobytes())
    tex.repeat_x = tex.repeat_y = wrap
    if mips:
        tex.build_mipmaps()
        tex.filter = (moderngl.LINEAR_MIPMAP_LINEAR, moderngl.LINEAR)
        tex.anisotropy = 8.0
    return tex


def tree_colours(atlas_path, lst):
    """Mean colour of each billboard's opaque pixels."""
    im = open_dds(atlas_path)
    a = np.asarray(im.convert("RGBA"), dtype=np.float32) / 255.0
    H, W = a.shape[:2]
    out = {}
    for i, (w, h, u0, v0, u1, v1) in lst.items():
        c = a[int(v0 * H):max(int(v1 * H), int(v0 * H) + 1), int(u0 * W):max(int(u1 * W), int(u0 * W) + 1)]
        m = c[..., 3] > 0.5
        out[i] = c[m][:, :3].mean(0) if m.any() else np.array([0.2, 0.25, 0.2])
    return out


def world_tiles(world, rect, lod=4):
    """Level 4 tiles of this land near the rect, from the terrain files on disk; one tile of margin for overhanging objects."""
    x0, y0, x1, y1 = rect
    d = os.path.join(MODS, "xLODGen Output", "meshes", "terrain", world.folder)
    out = set()
    for f in glob.glob(os.path.join(d, "*.4.*.btr")):
        m = re.match(r".*\.4\.(-?\d+)\.(-?\d+)(\.win)?\.btr$", f, re.I)
        if m:
            tx, ty = int(m.group(1)), int(m.group(2))
            if x0 - 2 * lod <= tx <= x1 + lod and y0 - 2 * lod <= ty <= y1 + lod:
                out.add((tx, ty))
    return sorted(out)


def outside_mask(rect, markers):
    """0 within 8 cells of a map marker, 1 from 14 cells out: Skyrim's playable land, by where the list puts things to find.
    Two texels a cell; soft, so nothing has a visible edge."""
    x0, y0, x1, y1 = rect
    w, h = max(2, int((x1 - x0) * 2)), max(2, int((y1 - y0) * 2))
    pts = np.array([(m["x"] / CELL, m["y"] / CELL) for m in markers], "f4")
    xs = x0 + (np.arange(w) + 0.5) / 2
    ys = y0 + (np.arange(h) + 0.5) / 2
    best = np.full((h, w), 1e9, "f4")
    for px, py in pts:
        dx = (xs - px)[None, :]
        dy = (ys - py)[:, None]
        best = np.minimum(best, dx * dx + dy * dy)
    t = np.clip((np.sqrt(best) - 8.0) / 6.0, 0, 1)
    return w, h, (t * t * (3 - 2 * t)).astype("f4")


class Scene:
    def __init__(self, rect_cells, ppc, world, markers=()):
        self.rect_cells = rect_cells
        self.world = world
        self.size = (int(round((rect_cells[2] - rect_cells[0]) * ppc)), int(round((rect_cells[3] - rect_cells[1]) * ppc)))
        self.ctx = moderngl.create_standalone_context()
        c = self.ctx
        self.fbs = []
        for _ in range(2):
            col = c.texture(self.size, 4)
            dep = c.depth_renderbuffer(self.size)
            self.fbs.append((c.framebuffer([col], dep), col))
        self.land = c.program(vertex_shader=VS, fragment_shader=FS_LAND)
        self.landimg_h = c.program(vertex_shader=VS_FULL_EARLY, fragment_shader=FS_LANDIMG_H)
        self.landimg = c.program(vertex_shader=VS_FULL_EARLY, fragment_shader=FS_LANDIMG)
        self.inst = c.program(vertex_shader=VS_INST, fragment_shader=FS_OBJ)
        self.roadmesh = c.program(vertex_shader=VS_INST, fragment_shader=FS_ROADMESH)
        self.struct_mesh = c.program(vertex_shader=VS, fragment_shader=FS_STRUCT)
        self.struct_inst = c.program(vertex_shader=VS_INST, fragment_shader=FS_STRUCT)
        self.outline = c.program(vertex_shader=VS_FULL_EARLY, fragment_shader=FS_OUTLINE)
        self.struct_tex = c.texture(self.size, 1, dtype="f4")
        self.struct_tex.filter = (moderngl.NEAREST, moderngl.NEAREST)
        self.struct_fb = c.framebuffer([self.struct_tex], c.depth_renderbuffer(self.size))
        self.height = c.program(vertex_shader=VS, fragment_shader=FS_HEIGHT)
        self.level = c.program(vertex_shader=VS_LEVEL, fragment_shader=FS_LEVEL)
        self.water = c.program(vertex_shader=VS_FULL, fragment_shader=FS_WATER)
        self.height_tex = c.texture(self.size, 1, dtype="f4")
        self.level_tex = c.texture(self.size, 2, dtype="f4")
        for t in (self.height_tex, self.level_tex):
            t.filter = (moderngl.NEAREST, moderngl.NEAREST)
        self.height_fb = c.framebuffer([self.height_tex], c.depth_renderbuffer(self.size))
        self.level_fb = c.framebuffer([self.level_tex], c.depth_renderbuffer(self.size))
        self.obj = c.program(vertex_shader=VS, fragment_shader=FS_OBJ)
        self.tree = c.program(vertex_shader=VS_TREE, fragment_shader=FS_TREE)
        self.glow = c.program(vertex_shader=VS_TREE, fragment_shader=FS_GLOW)
        self.lightmark = c.program(vertex_shader=VS_TREE, fragment_shader=FS_LIGHTMARK)
        r = tuple(v * CELL for v in rect_cells)
        for p in (self.land, self.obj, self.tree, self.inst, self.landimg):
            p["winter"].value = 1.0 if world.winter else 0.0
        for p in (self.roadmesh, self.lightmark, self.land, self.height, self.level, self.obj, self.tree, self.glow, self.inst, self.landimg, self.struct_mesh, self.struct_inst):
            p["rect"].value = r
        for p in (self.land, self.obj, self.tree, self.inst, self.landimg):
            p["ambientDay"].value = world.ambient
            p["tone"].value = world.tone
        if world.surround and len(markers):
            mw, mh, mask = outside_mask(rect_cells, markers)
        else:
            mw, mh, mask = 2, 2, np.zeros((2, 2), "f4")
        self.mask_tex = c.texture((mw, mh), 1, mask.tobytes(), dtype="f4")
        self.mask_tex.repeat_x = self.mask_tex.repeat_y = False
        self.mask_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
        self.mask_tex.use(5)
        self.height_tex.use(6)
        for p in (self.land, self.obj, self.tree, self.inst, self.landimg):
            if "outsideMask" in p:
                p["outsideMask"].value = 5
        upp = (rect_cells[2] - rect_cells[0]) * CELL / self.size[0]
        for p in (self.land, self.obj, self.inst):
            p["heights"].value = 6
            p["upp"].value = upp
        self.obj["soften"].value = world.soften
        self.obj["snowLo"].value, self.obj["snowHi"].value = 0.62, 0.86
        self.inst["snowLo"].value, self.inst["snowHi"].value = 0.50, 0.78
        self.land["landSoft"].value = world.land_soft
        self.land["landColour"].value = world.land_colour
        self.land["relief"].value = world.relief
        self.land["voidBelow"].value = world.void_below if world.void_below is not None else -2.0e6
        self.obj["ceilingGap"].value = world.ceiling
        self.inst["ceilingGap"].value = world.ceiling
        self.inst["soften"].value = 0.0   # what the references place is real geometry, not coarse LOD
        # roads
        self.road_stats = None
        if world.roads:
            try:
                img, self.road_stats = roads_mod.build(world.id, tuple(int(v) for v in rect_cells), (lambda f: np.array(world.road_paint, "f4")) if world.road_paint else mean_colour_lookup(self.tex_for_roads()), world.road_extra)
                self.road_tex = c.texture((img.shape[1], img.shape[0]), 4, img[::-1].tobytes())
                self.land["roadsOn"].value = 1.0
                self.landimg["roadsOn"].value = 1.0
            except Exception as e:
                print("no road layer:", e, flush=True)
                self.road_tex = c.texture((1, 1), 4, bytes(4))
                self.land["roadsOn"].value = 0.0
                self.landimg["roadsOn"].value = 0.0
        else:
            self.road_tex = c.texture((1, 1), 4, bytes(4))
            self.land["roadsOn"].value = 0.0
            self.landimg["roadsOn"].value = 0.0
        self.road_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
        self.road_tex.use(7)
        self.land["roadmap"].value = 7
        self.landimg["roadmap"].value = 7
        self.atlases = {}
        self.stats = dict(tiles=0, land_tris=0, water_tris=0, obj_tris=0, trees=0, glows=0, missing=set())
        self.tex = self.tex_for_roads()

    def tex_for_roads(self):
        if not hasattr(self, "tex"):
            self.tex = Textures()
        return self.tex

    def clear(self):
        for i, (fb, _) in enumerate(self.fbs):
            fb.use()
            if i == 1 and self.world.void_below is not None:
                fb.clear(0.039, 0.051, 0.078, 1.0, depth=1.0)
            else:
                fb.clear(0.13, 0.16, 0.2, 1.0, depth=1.0)
        self.height_fb.use()
        self.height_fb.clear(-2.0e6, 0.0, 0.0, 1.0, depth=1.0)
        self.level_fb.use()
        self.level_fb.clear(-2.0e6, 0.0, 0.0, 1.0, depth=1.0)
        self.struct_fb.use()
        self.struct_fb.clear(-2.0e6, 0.0, 0.0, 1.0, depth=1.0)

    def draw_both(self, prog, vao, setup=None):
        for i, (fb, _) in enumerate(self.fbs):
            fb.use()
            if "night" in prog:
                prog["night"].value = i
            vao.render(moderngl.TRIANGLES)

    def _mesh(self, prog, verts, uvs, cols, tris, origin):
        c = self.ctx
        n = len(verts)
        data = np.zeros((n, 9), dtype="f4")
        data[:, :3] = verts
        if uvs is not None:
            data[:, 3:5] = uvs
        if cols is not None:
            data[:, 5:9] = cols[:, :4] / 255.0
        else:
            data[:, 5:9] = 1.0
        vb = c.buffer(data.tobytes())
        ib = c.buffer(tris.astype("u4").tobytes())
        wanted = [("in_pos", "3f"), ("in_uv", "2f"), ("in_col", "4f")]
        fmt = []
        names = []
        for name, f in wanted:
            if name in prog:
                fmt.append(f)
                names.append(name)
            else:
                fmt.append("%dx4" % int(f[0]))
        vao = c.vertex_array(prog, [(vb, " ".join(fmt), *names)], ib)
        prog["origin"].value = origin
        return vao, vb, ib

    def draw_terrain(self, tiles):
        c = self.ctx
        wd = self.world
        c.enable(moderngl.DEPTH_TEST)
        parsed = []
        hide = [tuple(v * CELL for v in h) for h in wd.hide]
        for (tx, ty) in tiles:
            base = os.path.join(MODS, "xLODGen Output", "meshes", "terrain", wd.folder, "%s.4.%d.%d" % (wd.lower, tx, ty))
            p = base + ".WIN.btr" if wd.winter else base + ".btr"
            if not os.path.isfile(p):
                p = base + ".btr" if wd.winter else base + ".WIN.btr"
            if not os.path.isfile(p):
                continue
            origin = (tx * CELL, ty * CELL, 0.0)
            self.stats["tiles"] += 1
            for sh in nifread.parse(p):
                if sh.name.lower() != "land" or sh.tris is None or not len(sh.tris):
                    continue
                for fx0, fy0, fx1, fy1, fz in wd.flatten:
                    wx = sh.verts[:, 0] + tx * CELL
                    wy = sh.verts[:, 1] + ty * CELL
                    sh.verts[(wx >= fx0 * CELL) & (wx <= fx1 * CELL) & (wy >= fy0 * CELL) & (wy <= fy1 * CELL), 2] = fz
                if hide:
                    # drop triangles whose middle lies in a hidden rectangle
                    m = sh.verts[sh.tris].mean(1)
                    keep = np.ones(len(m), bool)
                    for x0, y0, x1, y1 in hide:
                        keep &= ~((m[:, 0] + tx * CELL >= x0) & (m[:, 0] + tx * CELL < x1) & (m[:, 1] + ty * CELL >= y0) & (m[:, 1] + ty * CELL < y1))
                    if not keep.any():
                        continue
                    sh.tris = sh.tris[keep]
                vao, vb, ib = self._mesh(self.height, sh.verts, sh.uvs, None, sh.tris, origin)
                self.height_fb.use()
                vao.render(moderngl.TRIANGLES)
                vao.release(); vb.release(); ib.release()
                parsed.append((tx, ty, sh))
        self.mask_tex.use(5)
        self.height_tex.use(6)
        self.road_tex.use(7)
        tdir = os.path.join(MODS, "xLODGen Output", "textures", "terrain", wd.lower)
        for tx, ty, sh in parsed:
            first, second = (".win", "") if wd.winter else ("", ".win")
            dp = os.path.join(tdir, "%s.4.%d.%d%s.dds" % (wd.lower, tx, ty, first))
            if not os.path.isfile(dp):
                dp = os.path.join(tdir, "%s.4.%d.%d%s.dds" % (wd.lower, tx, ty, second))
            np_ = os.path.join(tdir, "%s.4.%d.%d%s_n.dds" % (wd.lower, tx, ty, first))
            if not os.path.isfile(np_):
                np_ = os.path.join(tdir, "%s.4.%d.%d%s_n.dds" % (wd.lower, tx, ty, second))
            if not (os.path.isfile(dp) and os.path.isfile(np_)):
                continue
            t0, t1 = load_texture(c, dp, wrap=False), load_texture(c, np_, wrap=False)
            vao, vb, ib = self._mesh(self.land, sh.verts, sh.uvs, None, sh.tris, (tx * CELL, ty * CELL, 0.0))
            t0.use(0)
            t1.use(1)
            self.land["diffuse"].value = 0
            self.land["normalmap"].value = 1
            self.draw_both(self.land, vao)
            vao.release(); vb.release(); ib.release()
            t0.release(); t1.release()
            self.stats["land_tris"] += len(sh.tris)

    def draw_children(self):
        """Child worlds (a walled city, in its parent's coordinates): their land over the parent's. Their buildings come with draw_placed."""
        kids = land_direct.child_worlds(self.world.id)
        self.stats["children"] = {}
        for k in kids:
            cells = self.draw_terrain_records(k, fill=-50000.0, bias=12.0)
            self.stats["children"][k] = cells

    def draw_terrain_records(self, source=None, fill=None, bias=0.0):
        """Heights and colours from a land's own records: no LOD involved."""
        c = self.ctx
        lookup = raw_colour_lookup(self.tex_for_roads())
        hh, col, cells = land_direct.land_images(source or self.world.id, tuple(int(v) for v in self.rect_cells), lookup, fill, self.world.hide if not source else ())
        if source:
            if not cells:
                return 0
            hh = hh + bias
        else:
            self.stats["land_cells"] = cells
        ht = c.texture((hh.shape[1], hh.shape[0]), 1, hh[::-1].tobytes(), dtype="f4")
        ct = c.texture((col.shape[1], col.shape[0]), 3, col[::-1].astype("f4").tobytes(), dtype="f4")
        for t in (ht, ct):
            t.filter = (moderngl.LINEAR, moderngl.LINEAR)
            t.repeat_x = t.repeat_y = False
        ht.use(8)
        ct.use(9)
        self.mask_tex.use(5)
        self.road_tex.use(7)
        quad = c.buffer(np.array([-1, -1, 1, -1, 1, 1, -1, -1, 1, 1, -1, 1], "f4").tobytes())
        c.enable(moderngl.DEPTH_TEST)
        self.landimg_h["landHeight"].value = 8
        self.landimg_h["canvas"].value = (float(self.size[0]), float(self.size[1]))
        vao = c.vertex_array(self.landimg_h, [(quad, "2f", "in_corner")])
        self.height_fb.use()
        vao.render(moderngl.TRIANGLES)
        vao.release()
        self.landimg["landHeight"].value = 8
        self.landimg["landColour"].value = 9
        self.landimg["canvas"].value = (float(self.size[0]), float(self.size[1]))
        vao = c.vertex_array(self.landimg, [(quad, "2f", "in_corner")])
        for i, (fb, _) in enumerate(self.fbs):
            fb.use()
            self.landimg["night"].value = i
            vao.render(moderngl.TRIANGLES)
        vao.release(); quad.release()
        ht.release(); ct.release()
        return cells

    def _instances(self, refs):
        inst = np.zeros((len(refs), 12), "f4")
        for i, (x, y, z, rx, ry, rz, sc) in enumerate(refs):
            m = land_direct.rotation(rx, ry, rz) * sc
            inst[i, 0:3], inst[i, 3:6], inst[i, 6:9] = m[:, 0], m[:, 1], m[:, 2]
            inst[i, 9:12] = (x, y, z)
        return inst

    def draw_road_meshes(self):
        """A road the land's mesh makes rather than its paint. No depth test: rock and buildings draw over it afterwards, against the land."""
        c = self.ctx
        tex = self.tex_for_roads()
        X0, Y0, X1, Y1 = (v * CELL for v in self.rect_cells)
        self.roadmesh["stone"].value = self.world.road_colour
        c.disable(moderngl.DEPTH_TEST)
        n = 0
        for rel, refs in (self.placed_refs or {}).items():
            if not ROAD_MODEL.search(rel.split("|")[0]):
                continue
            refs = [r for r in refs if X0 - 3000 <= r[0] <= X1 + 3000 and Y0 - 3000 <= r[1] <= Y1 + 3000]
            if not refs:
                continue
            shapes = land_direct.load_model(rel, tex)
            if not shapes:
                continue
            ib_inst = c.buffer(self._instances(refs).tobytes())
            for verts, uvs, cols, tris, *_ in shapes:
                data = np.zeros((len(verts), 9), "f4")
                data[:, :3] = verts
                data[:, 3:5] = uvs
                vb = c.buffer(data.tobytes())
                ib = c.buffer(tris.astype("u4").tobytes())
                vao = c.vertex_array(self.roadmesh, [(vb, "3f 2f 4f", "in_pos", "in_uv", "in_col"), (ib_inst, "3f 3f 3f 3f/i", "in_m0", "in_m1", "in_m2", "in_t")], ib)
                for i, (fb, _) in enumerate(self.fbs):
                    fb.use()
                    self.roadmesh["night"].value = i
                    vao.render(moderngl.TRIANGLES, instances=len(refs))
                vao.release(); vb.release(); ib.release()
            ib_inst.release()
            n += len(refs)
        self.stats["road_meshes"] = n
        c.enable(moderngl.DEPTH_TEST)

    def draw_placed(self):
        """Every large static the land places, from its full model. Slower than LOD, but needs no generator."""
        c = self.ctx
        placed = self.placed_refs if self.placed_refs is not None else land_direct.placed_models(self.world.id)
        if not self.world.direct and self.world.placed != "all":
            # the LOD already has this land's rock; only what it may lack is added
            placed = {k: v for k, v in placed.items() if not ROCK.search(k)}
        X0, Y0, X1, Y1 = (v * CELL for v in self.rect_cells)
        c.enable(moderngl.DEPTH_TEST)
        self.mask_tex.use(5)
        self.height_tex.use(6)
        self.stats["placed_refs"] = 0
        self.stats["placed_models"] = 0
        failed = 0
        tex = self.tex_for_roads()
        for rel, refs in placed.items():
            if self.world.road_meshes and ROAD_MODEL.search(rel.split("|")[0]):
                continue
            refs = [r for r in refs if X0 - 3000 <= r[0] <= X1 + 3000 and Y0 - 3000 <= r[1] <= Y1 + 3000]
            if not refs:
                continue
            try:
                shapes = land_direct.load_model(rel, tex)
            except Exception:
                shapes = None
            if not shapes:
                failed += 1
                continue
            inst = np.zeros((len(refs), 12), "f4")
            for i, (x, y, z, rx, ry, rz, sc) in enumerate(refs):
                m = land_direct.rotation(rx, ry, rz) * sc
                inst[i, 0:3], inst[i, 3:6], inst[i, 6:9] = m[:, 0], m[:, 1], m[:, 2]
                inst[i, 9:12] = (x, y, z)
            ib_inst = c.buffer(inst.tobytes())
            for verts, uvs, cols, tris, texpath, cut in shapes:
                t = self.atlas(texpath if texpath.lower().startswith(("data", "textures")) else "textures\\" + texpath)
                data = np.zeros((len(verts), 9), "f4")
                data[:, :3] = verts
                data[:, 3:5] = uvs
                data[:, 5:9] = cols[:, :4] / 255.0 if cols is not None else 1.0
                vb = c.buffer(data.tobytes())
                ib = c.buffer(tris.astype("u4").tobytes())
                vao = c.vertex_array(self.inst, [(vb, "3f 2f 4f", "in_pos", "in_uv", "in_col"), (ib_inst, "3f 3f 3f 3f/i", "in_m0", "in_m1", "in_m2", "in_t")], ib)
                t.use(0)
                self.mask_tex.use(5)
                self.height_tex.use(6)
                self.inst["diffuse"].value = 0
                self.inst["alphatest"].value = 1 if cut else 0
                for i, (fb, _) in enumerate(self.fbs):
                    fb.use()
                    self.inst["night"].value = i
                    vao.render(moderngl.TRIANGLES, instances=len(refs))
                vao.release()
                if not ROCK.search(rel.split("|")[0]):
                    c.disable(moderngl.BLEND)
                    vao = c.vertex_array(self.struct_inst, [(vb, "3f 2f 4f", "in_pos", "in_uv", "in_col"), (ib_inst, "3f 3f 3f 3f/i", "in_m0", "in_m1", "in_m2", "in_t")], ib)
                    self.struct_inst["diffuse"].value = 0
                    self.struct_inst["alphatest"].value = 1 if cut else 0
                    self.struct_fb.use()
                    vao.render(moderngl.TRIANGLES, instances=len(refs))
                    vao.release()
                    c.enable(moderngl.BLEND)
                vb.release(); ib.release()
                self.stats["obj_tris"] += len(tris) * len(refs)
            ib_inst.release()
            self.stats["placed_refs"] += len(refs)
            self.stats["placed_models"] += 1
        self.stats["placed_failed"] = failed

    def draw_water(self, tiles, sea=None):
        c = self.ctx
        wd = self.world
        grow = CELL
        quads = []
        triangles = []
        for (tx, ty) in tiles:
            base = os.path.join(MODS, "xLODGen Output", "meshes", "terrain", wd.folder, "%s.4.%d.%d" % (wd.lower, tx, ty))
            p = base + ".btr"
            if not os.path.isfile(p):
                p = base + ".WIN.btr"
            if not os.path.isfile(p):
                continue
            ox, oy = tx * CELL, ty * CELL
            for sh in nifread.parse(p):
                if sh.name.lower() == "land" or sh.tris is None or not len(sh.tris):
                    continue
                v = sh.verts
                # One shape holds a tile's water, and need not fill the tile: each triangle stands for itself, or a whole tile floods.
                for tri in v[sh.tris]:
                    quads.append((tri[:, 0].min() + ox, tri[:, 0].max() + ox, tri[:, 1].min() + oy, tri[:, 1].max() + oy, float(tri[:, 2].mean())))
                triangles.append(np.column_stack([v[sh.tris].reshape(-1, 3)[:, 0] + ox, v[sh.tris].reshape(-1, 3)[:, 1] + oy, v[sh.tris].reshape(-1, 3)[:, 2]]).astype("f4"))
                self.stats["water_tris"] += len(sh.tris)
        if not quads and sea is None:
            return

        def batch(rows, g):
            out = []
            for x0, x1, y0, y1, z in rows:
                x0, x1, y0, y1 = x0 - g, x1 + g, y0 - g, y1 + g
                out += [x0, y0, z, x1, y0, z, x1, y1, z, x0, y0, z, x1, y1, z, x0, y1, z]
            return np.array(out, "f4")

        c.enable(moderngl.DEPTH_TEST)
        self.level_fb.use()
        passes = []
        if quads:
            passes += [(0.0, np.concatenate(triangles).ravel()), (0.5, batch(quads, grow))]
        if sea is not None:
            X0, Y0, X1, Y1 = (v * CELL for v in self.rect_cells)
            passes.append((1.0, batch([(X0, X1, Y0, Y1, sea)], 0.0)))
        for band, verts in passes:
            vb = c.buffer(verts.tobytes())
            vao = c.vertex_array(self.level, [(vb, "3f", "in_pos")])
            self.level["band"].value = band
            vao.render(moderngl.TRIANGLES)
            vao.release(); vb.release()
        quad = c.buffer(np.array([-1, -1, 1, -1, 1, 1, -1, -1, 1, 1, -1, 1], "f4").tobytes())
        vao = c.vertex_array(self.water, [(quad, "2f", "in_corner")])
        self.height_tex.use(0)
        self.level_tex.use(1)
        self.water["heights"].value = 0
        self.water["levels"].value = 1
        self.water["sea"].value = sea if sea is not None else -1.0e7
        self.water["shallowDay"].value = wd.water[0]
        self.water["farDay"].value = wd.water[1]
        self.water["shallowNight"].value = wd.water[2]
        self.water["farNight"].value = wd.water[3]
        self.water["waterStyle"].value = wd.water_style
        self.water["useTrue"].value = 0
        self.water["allWater"].value = 1 if wd.all_water else 0
        if wd.true_coast:
            hh, _, cells = land_direct.land_images(wd.id, tuple(int(v) for v in self.rect_cells), lambda f: None, -50000.0)
            self.stats["coast_cells"] = cells
            if cells:
                hh = soften_coast(hh)
                true_tex = c.texture((hh.shape[1], hh.shape[0]), 1, hh[::-1].tobytes(), dtype="f4")
                true_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
                true_tex.repeat_x = true_tex.repeat_y = False
                true_tex.use(11)
                self.water["trueHeight"].value = 11
                self.water["useTrue"].value = 1
        if True:
            self.water["canvas"].value = (float(self.size[0]), float(self.size[1]))
            self.water["worldRect"].value = tuple(v * CELL for v in self.rect_cells)
        self.water["winter"].value = 1.0 if wd.winter else 0.0
        c.enable(moderngl.BLEND)
        c.blend_func = (moderngl.SRC_ALPHA, moderngl.ONE_MINUS_SRC_ALPHA)
        for i, (fb, _) in enumerate(self.fbs):
            fb.use()
            self.water["night"].value = i
            vao.render(moderngl.TRIANGLES)
        vao.release(); quad.release()
        c.disable(moderngl.BLEND)

    def atlas(self, path):
        key = path.lower()
        if key not in self.atlases:
            f = self.tex.find(path)
            big = "dyndolod\\lod" in key
            try:
                self.atlases[key] = load_texture(self.ctx, f, limit=0 if big else 512) if f else None
            except Exception as e:
                print("cannot read", f, e)
                self.atlases[key] = None
                f = None
            if not f:
                raw = None
                try:
                    raw = self.tex.archived(path)
                    if raw:
                        self.atlases[key] = load_texture(self.ctx, None, limit=512, raw=raw)
                except Exception as e:
                    print("cannot read archived", key, e)
                    raw = None
                if not raw:
                    # not even in the archives: a plain stone colour, which the snow then covers
                    self.stats["missing"].add(key)
                    self.atlases[key] = self.plain()
        return self.atlases[key]

    def plain(self):
        if not hasattr(self, "_plain"):
            self._plain = self.ctx.texture((1, 1), 4, bytes([96, 94, 90, 255]))
        return self._plain

    def draw_objects(self, tiles):
        c = self.ctx
        wd = self.world
        c.enable(moderngl.DEPTH_TEST)
        c.enable(moderngl.BLEND)
        c.blend_func = (moderngl.SRC_ALPHA, moderngl.ONE_MINUS_SRC_ALPHA)
        self.mask_tex.use(5)
        self.height_tex.use(6)
        glow = []
        d = os.path.join(MODS, "DynDOLOD Output", "meshes", "Terrain", wd.dyn, "Objects")
        for (tx, ty) in tiles:
            p = os.path.join(d, "%s.4.%d.%d.bto" % (wd.dyn, tx, ty))
            if not os.path.isfile(p):
                continue
            for s in nifread.parse(p):
                if s.tris is None or not len(s.tris) or s.uvs is None or not s.textures or s.name.lower() == "treepassthru":
                    continue
                t = s.textures[0].lower()
                if EFFECT_SHAPE.search(t):
                    continue
                if wd.placed == "all" and "_glow" not in t:
                    continue   # the full models stand here instead
                if "_glow" in t:
                    centres = s.verts[s.tris].mean(1)
                    glow.append(centres)
                tex = self.atlas(s.textures[0])
                if tex is None:
                    continue
                vao, vb, ib = self._mesh(self.obj, s.verts, s.uvs, s.colors, s.tris, (0.0, 0.0, 0.0))
                tex.use(0)
                self.mask_tex.use(5)
                self.height_tex.use(6)
                self.obj["diffuse"].value = 0
                self.obj["alphatest"].value = 1 if "alpha" in t else 0
                self.draw_both(self.obj, vao)
                vao.release(); vb.release(); ib.release()
                if not ROCK.search(t):
                    c.disable(moderngl.BLEND)
                    vao, vb, ib = self._mesh(self.struct_mesh, s.verts, s.uvs, None, s.tris, (0.0, 0.0, 0.0))
                    self.struct_mesh["diffuse"].value = 0
                    self.struct_mesh["alphatest"].value = 1 if "alpha" in t else 0
                    self.struct_fb.use()
                    vao.render(moderngl.TRIANGLES)
                    vao.release(); vb.release(); ib.release()
                    c.enable(moderngl.BLEND)
                self.stats["obj_tris"] += len(s.tris)
        c.disable(moderngl.BLEND)
        return np.concatenate(glow) if glow else np.zeros((0, 3), "f4")

    def _sprites(self, prog, inst, rgb, use):
        c = self.ctx
        if not len(inst):
            return
        quad = c.buffer(np.array([-1, -1, 1, -1, 1, 1, -1, -1, 1, 1, -1, 1], "f4").tobytes())
        ib = c.buffer(inst.astype("f4").tobytes())
        cb = c.buffer(rgb.astype("f4").tobytes())
        content = [(quad, "2f", "in_corner"), (ib, "4f/i", "in_inst")]
        if "in_rgb" in prog:
            content.append((cb, "3f/i", "in_rgb"))
        vao = c.vertex_array(prog, content)
        for i in use:
            self.fbs[i][0].use()
            if "night" in prog:
                prog["night"].value = i
            vao.render(moderngl.TRIANGLES, instances=len(inst))
        vao.release(); quad.release(); ib.release(); cb.release()

    def draw_trees(self, tiles):
        wd = self.world
        d = os.path.join(MODS, "DynDOLOD Output", "meshes", "Terrain", wd.dyn, "Trees")
        lp = os.path.join(d, wd.dyn + ".lst")
        ap = os.path.join(MODS, "DynDOLOD Output", "textures", "Terrain", wd.dyn, "Trees", wd.dyn + "TreeLOD.dds")
        if not (os.path.isfile(lp) and os.path.isfile(ap)):
            return
        lst = nifread.read_lst(lp)
        cols = tree_colours(ap, lst)
        insts, rgbs = [], []
        for (tx, ty) in tiles:
            p = os.path.join(d, "%s.4.%d.%d.btt" % (wd.dyn, tx, ty))
            if not os.path.isfile(p):
                continue
            for t, a in nifread.read_btt(p).items():
                if t not in lst or not len(a):
                    continue
                w, h = lst[t][:2]
                n = len(a)
                i = np.zeros((n, 4), "f4")
                i[:, 0:2] = a[:, 0:2]
                i[:, 2] = a[:, 2] + 0.7 * h * a[:, 4]
                i[:, 3] = np.minimum(0.24 * w * a[:, 4], 380.0)
                insts.append(i)
                rgbs.append(np.tile(cols[t], (n, 1)))
        if insts:
            insts, rgbs = np.concatenate(insts), np.concatenate(rgbs)
            self.stats["trees"] = len(insts)
            self.ctx.enable(moderngl.DEPTH_TEST)
            self._sprites(self.tree, insts, rgbs, (0, 1))

    def draw_glow(self, centres, markers):
        """Lit windows in the LOD, weakly, and a pool at each city, town and settlement the markers name."""
        # the marker's icon type: castles and town halls, towns, then lit single buildings (shops, inns, smiths, houses, temples)
        sizes = {1: (2600.0, 0.6), 2: (1500.0, 0.55), 3: (800.0, 0.4), 0x0F: (700.0, 0.3), 0x1E: (700.0, 0.3), 0x22: (700.0, 0.3),
                 0x23: (2200.0, 0.5), 0x25: (2200.0, 0.5), 0x28: (2200.0, 0.5), 0x2C: (2200.0, 0.5)}
        pools = [(m["x"], m["y"], sizes[m["type"]][0], sizes[m["type"]][1]) for m in markers if m.get("type") in sizes] if MARKER_POOLS else []
        if len(centres):
            pools.extend((c[0], c[1], 420.0, 0.42) for c in centres)
        if not pools:
            return
        self.stats["glows"] = len(pools)
        c = self.ctx
        c.disable(moderngl.DEPTH_TEST)
        c.enable(moderngl.BLEND)
        c.blend_func = (moderngl.ONE, moderngl.ONE)
        c.blend_equation = moderngl.MAX
        a = np.array(pools, "f4")
        inst = np.zeros((len(pools), 4), "f4")
        inst[:, :2] = a[:, :2]
        inst[:, 3] = a[:, 2]
        rgb = np.zeros((len(pools), 3), "f4")
        rgb[:, 0] = a[:, 3]
        self._sprites(self.glow, inst, rgb, (1,))
        c.blend_equation = moderngl.FUNC_ADD
        c.disable(moderngl.BLEND)
        c.blend_func = (moderngl.SRC_ALPHA, moderngl.ONE_MINUS_SRC_ALPHA)

    def draw_lights(self, lights, ppc):
        """A mark at each thing that gives light: a bright core in its own colour, with a soft pool."""
        if not len(lights):
            return
        self.stats["lights"] = len(lights)
        c = self.ctx
        c.disable(moderngl.DEPTH_TEST)
        c.enable(moderngl.BLEND)
        c.blend_func = (moderngl.ONE, moderngl.ONE)
        c.blend_equation = moderngl.MAX
        a = np.array([l[:6] for l in lights], "f4")
        for scale, strength in ((1.0, 0.40), (0.30, 1.0)):
            inst = np.zeros((len(a), 4), "f4")
            inst[:, :2] = a[:, :2]
            inst[:, 3] = np.maximum(a[:, 5] * scale, 1.1 * CELL / ppc)
            self._sprites(self.lightmark, inst, a[:, 2:5] * strength, (1,))
        c.blend_equation = moderngl.FUNC_ADD
        c.disable(moderngl.BLEND)
        c.blend_func = (moderngl.SRC_ALPHA, moderngl.ONE_MINUS_SRC_ALPHA)

    def draw_outlines(self):
        c = self.ctx
        quad = c.buffer(np.array([-1, -1, 1, -1, 1, 1, -1, -1, 1, 1, -1, 1], "f4").tobytes())
        vao = c.vertex_array(self.outline, [(quad, "2f", "in_corner")])
        self.struct_tex.use(10)
        self.height_tex.use(6)
        self.outline["structures"].value = 10
        self.outline["lineScale"].value = self.size[0] / (self.rect_cells[2] - self.rect_cells[0]) / 128.0
        self.outline["terrainLines"].value = 1 if self.world.terrain_lines else 0
        self.outline["heights"].value = 6
        c.disable(moderngl.DEPTH_TEST)
        c.enable(moderngl.BLEND)
        c.blend_func = (moderngl.ZERO, moderngl.SRC_COLOR)
        for fb, _ in self.fbs:
            fb.use()
            vao.render(moderngl.TRIANGLES)
        c.disable(moderngl.BLEND)
        c.blend_func = (moderngl.SRC_ALPHA, moderngl.ONE_MINUS_SRC_ALPHA)
        vao.release(); quad.release()

    def draw_lanterns(self, spots, ppc):
        """A speck at each lantern, a pixel or two at full size, with a faint halo so a road of them still reads zoomed out."""
        if not len(spots):
            return
        self.stats["lanterns"] = len(spots)
        c = self.ctx
        c.disable(moderngl.DEPTH_TEST)
        c.enable(moderngl.BLEND)
        c.blend_func = (moderngl.ONE, moderngl.ONE)
        c.blend_equation = moderngl.MAX
        xy = np.array([(s[0], s[1]) for s in spots], "f4")
        for radius, strength in ((max(120.0, 3.5 * CELL / ppc), 0.32), (max(34.0, 1.1 * CELL / ppc), 1.0)):
            inst = np.zeros((len(xy), 4), "f4")
            inst[:, :2] = xy
            inst[:, 3] = radius
            rgb = np.zeros((len(xy), 3), "f4")
            rgb[:, 0] = strength
            self._sprites(self.glow, inst, rgb, (1,))
        c.blend_equation = moderngl.FUNC_ADD
        c.disable(moderngl.BLEND)
        c.blend_func = (moderngl.SRC_ALPHA, moderngl.ONE_MINUS_SRC_ALPHA)

    def read(self, i):
        fb, col = self.fbs[i]
        raw = col.read()
        a = np.frombuffer(raw, dtype=np.uint8).reshape(self.size[1], self.size[0], 4)[::-1, :, :3]
        return a


def soften_coast(hh, passes=6):
    """Round the shore's contours, leaving cells with no land alone (they hold a very low fill height)."""
    good = hh > -40000.0
    a = np.where(good, hh, 0.0).astype("f4")
    w = good.astype("f4")
    for _ in range(passes):
        for axis in (0, 1):
            a = (np.roll(a, 1, axis) + 2 * a + np.roll(a, -1, axis)) / 4
            w = (np.roll(w, 1, axis) + 2 * w + np.roll(w, -1, axis)) / 4
    return np.where(good, a / np.maximum(w, 1e-3), hh).astype("f4")


def world_from_config(edid, c):
    """One land's settings from worlds.json."""
    kw = {k: c[k] for k in ("winter", "surround", "soften", "roads", "road_extra", "land_soft", "land_colour", "hide", "ceiling", "true_coast", "terrain_lines", "all_water", "relief", "flatten", "void_below", "alt_textures", "road_meshes", "road_colour", "road_paint", "direct", "placed", "ambient", "tone", "water", "water_style") if k in c}
    return World(edid, **kw)


class World:
    def __init__(self, editor_id, dyn=None, folder=None, winter=True, surround=False, soften=0.0, roads=True, road_extra=None, land_soft=0.0, land_colour=(0.0, 0.0, 0.0), hide=(), ceiling=0.0, true_coast=True, terrain_lines=False, all_water=False, relief=1.0, flatten=(), void_below=None, alt_textures=False, road_meshes=False, road_colour=(0.40, 0.38, 0.36), road_paint=None,
                 water=((0.33, 0.44, 0.52), (0.23, 0.33, 0.42), (0.055, 0.085, 0.14), (0.03, 0.05, 0.09)), water_style=0,
                 direct=False, placed=False, ambient=(0.56, 0.62, 0.74), tone=(1.0, 1.0, 1.0)):
        self.water = tuple(tuple(c) for c in water)
        self.water_style = water_style
        self.land_soft = land_soft
        self.land_colour = tuple(land_colour)
        self.true_coast = true_coast
        self.terrain_lines = terrain_lines
        self.all_water = all_water
        self.void_below = void_below
        self.relief = relief
        self.flatten = flatten
        self.alt_textures = alt_textures
        self.road_meshes = road_meshes
        self.road_colour = tuple(road_colour)
        self.road_paint = road_paint
        self.ceiling = ceiling
        self.hide = hide
        self.placed = placed      # add the structures its references place, from their full models, to what its LOD has
        self.direct = direct      # draw terrain and buildings from the land's records instead of its LOD
        self.ambient = tuple(ambient)
        self.tone = tuple(tone)
        self.road_extra = road_extra
        self.soften = soften
        self.roads = roads
        self.winter = winter
        self.surround = surround
        self.id = editor_id
        self.lower = editor_id.lower()
        self.folder = folder or editor_id
        self.dyn = dyn or editor_id


def render(world, rect, ppc, markers=(), sea=None, lanterns=(), lights=()):
    t0 = time.time()
    sc = Scene(rect, ppc, world, markers)
    sc.clear()
    tiles = world_tiles(world, rect)
    if not tiles and not world.direct:
        raise SystemExit("no level 4 terrain for %s in rect %s" % (world.id, rect))
    t1 = time.time()
    sc.placed_refs = None
    if world.placed == "all":
        sc.placed_refs = land_direct.placed_models(world.id, world.alt_textures)
        if not sc.placed_refs:
            world.placed = False
            sc.stats["no_references"] = True
    if world.direct:
        sc.draw_terrain_records()
    else:
        sc.draw_terrain(tiles)
    t2 = time.time()
    sc.draw_water(tiles, sea)
    if world.road_meshes and sc.placed_refs:
        sc.draw_road_meshes()
    t3 = time.time()
    centres = sc.draw_objects(tiles)
    if world.direct or world.placed:
        c = sc.ctx
        c.enable(moderngl.BLEND)
        c.blend_func = (moderngl.SRC_ALPHA, moderngl.ONE_MINUS_SRC_ALPHA)
        sc.draw_placed()
        c.disable(moderngl.BLEND)
    sc.draw_outlines()
    t4 = time.time()
    sc.draw_trees(tiles)
    t5 = time.time()
    sc.draw_glow(centres, markers)
    sc.draw_lanterns(lanterns, ppc)
    sc.draw_lights(lights, ppc)
    sc.tex.save()
    day, night = sc.read(0), sc.read(1)
    sc.ctx.release()
    t6 = time.time()
    sc.stats["missing"] = sorted(sc.stats["missing"])
    if sc.road_stats:
        sc.stats["roads"] = sc.road_stats
    timing = dict(setup=t1 - t0, terrain=t2 - t1, water=t3 - t2, objects=t4 - t3, trees=t5 - t4, finish=t6 - t5, total=t6 - t0)
    return day, night, sc.stats, timing


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--world", default="Tamriel")
    ap.add_argument("--rect", type=float, nargs=4, required=True)
    ap.add_argument("--ppc", type=float, default=64)
    ap.add_argument("--dyn", default=None)
    ap.add_argument("--folder", default=None)
    ap.add_argument("--out-day")
    ap.add_argument("--out-night")
    ap.add_argument("--stats")
    ap.add_argument("--markers")
    ap.add_argument("--surround", action="store_true")
    ap.add_argument("--sea", type=float, default=None, help="the world's default water height")
    args = ap.parse_args()
    w = World(args.world, args.dyn, args.folder, surround=args.surround)
    mk = json.load(open(args.markers, encoding="utf-8")) if args.markers else ()
    day, night, stats, timing = render(w, tuple(args.rect), args.ppc, mk, args.sea, lanterns_mod.collect(args.world))
    if args.out_day:
        Image.fromarray(day).save(args.out_day)
    if args.out_night:
        Image.fromarray(night).save(args.out_night)
    out = dict(stats=stats, timing={k: round(v, 2) for k, v in timing.items()}, size=[day.shape[1], day.shape[0]])
    if args.stats:
        json.dump(out, open(args.stats, "w"), indent=1)
    print(json.dumps(out)[:1500])


if __name__ == "__main__":
    main()
