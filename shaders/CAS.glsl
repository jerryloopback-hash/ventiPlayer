//!HOOK MAIN
//!BIND HOOKED
//!DESC AMD FidelityFX Contrast Adaptive Sharpening (CAS)

// Based on AMD FidelityFX CAS v1.0
// Attempt to keep quality while having a fast sharpener.
// Attempt to reduce halos around strong edges.

// Sharpening amount: 0.0 = maximum sharpening, 1.0 = no sharpening
#define SHARPNESS 0.4

vec4 hook() {
    vec2 pt = HOOKED_pt;
    vec2 pos = HOOKED_pos;

    // Fetch 3x3 neighborhood
    vec3 a = HOOKED_texOff(vec2(-1, -1)).rgb;
    vec3 b = HOOKED_texOff(vec2( 0, -1)).rgb;
    vec3 c = HOOKED_texOff(vec2( 1, -1)).rgb;
    vec3 d = HOOKED_texOff(vec2(-1,  0)).rgb;
    vec3 e = HOOKED_texOff(vec2( 0,  0)).rgb;
    vec3 f = HOOKED_texOff(vec2( 1,  0)).rgb;
    vec3 g = HOOKED_texOff(vec2(-1,  1)).rgb;
    vec3 h = HOOKED_texOff(vec2( 0,  1)).rgb;
    vec3 i = HOOKED_texOff(vec2( 1,  1)).rgb;

    // Soft min and max (per channel)
    vec3 mnRGB = min(min(min(d, e), min(f, b)), h);
    vec3 mnRGB2 = min(min(min(mnRGB, a), min(g, c)), i);
    mnRGB += mnRGB2;

    vec3 mxRGB = max(max(max(d, e), max(f, b)), h);
    vec3 mxRGB2 = max(max(max(mxRGB, a), max(g, c)), i);
    mxRGB += mxRGB2;

    // Smooth minimum distance to signal limit divided by smooth max
    vec3 rcpMxRGB = vec3(1.0) / mxRGB;
    vec3 ampRGB = clamp(min(mnRGB, 2.0 - mxRGB) * rcpMxRGB, 0.0, 1.0);

    // Shaping amount of sharpening
    ampRGB = inversesqrt(ampRGB);

    float peak = 8.0 - 3.0 * SHARPNESS;
    vec3 wRGB = -(vec3(1.0) / (ampRGB * peak));

    vec3 rcpWeightRGB = vec3(1.0) / (1.0 + 4.0 * wRGB);

    // Filter shape:
    //  0 w 0
    //  w 1 w
    //  0 w 0
    vec3 outColor = ((b * wRGB + d * wRGB + f * wRGB + h * wRGB + e) * rcpWeightRGB);

    return vec4(clamp(outColor, 0.0, 1.0), HOOKED_texOff(vec2(0,0)).a);
}
