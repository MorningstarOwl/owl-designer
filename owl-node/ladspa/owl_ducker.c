/* owl_ducker: a LADSPA ducker with a stereo sidechain, for the owl-node dsp role's `ducker` block (D2).
 *
 * The swh/LSP compressors duck by *ratio* (the dip depends on how loud the sidechain is), which makes an announcement
 * duck music by an amount nobody can set. A Q-SYS-style ducker dips the program by a fixed DEPTH whenever the
 * sidechain is above THRESHOLD, with ATTACK (time to reach the full depth), HOLD (time the dip stays after the
 * sidechain falls silent) and RELEASE (time back to unity). Attack and release are linear ramps in dB, so the times
 * are exactly the times a meter will measure. MIX adds the sidechain itself to the output (announcement over music).
 *
 * Ports: Threshold (dB), Depth (dB), Attack (ms), Hold (ms), Release (ms), Mix sidechain (0/1), Gain reduction (dB,
 * output control), Program L, Program R, Sidechain L, Sidechain R, Out L, Out R.
 *
 * Built in the owl-node image (Dockerfile, gstbuild stage) into /opt/owl-node/ladspa/owl_ducker.so; label "owl_ducker".
 * gcc -O2 -fPIC -shared -o owl_ducker.so owl_ducker.c -lm
 */
#include <ladspa.h>
#include <math.h>
#include <stdlib.h>
#include <string.h>

enum { P_THRESH, P_DEPTH, P_ATTACK, P_HOLD, P_RELEASE, P_MIX, P_GR, P_IN_L, P_IN_R, P_SC_L, P_SC_R, P_OUT_L, P_OUT_R, N_PORTS };

typedef struct {
    LADSPA_Data *port[N_PORTS];
    float rate;
    float gain_db;      /* current gain, 0 = unity, -depth = fully ducked */
    float env;          /* sidechain peak envelope (linear) */
    long hold_left;     /* samples of hold remaining */
} Ducker;

static LADSPA_Handle instantiate(const LADSPA_Descriptor *d, unsigned long rate) {
    (void)d;
    Ducker *s = calloc(1, sizeof(Ducker));
    if (s) s->rate = (float)rate;
    return s;
}

static void connect_port(LADSPA_Handle h, unsigned long p, LADSPA_Data *data) {
    if (p < N_PORTS) ((Ducker *)h)->port[p] = data;
}

static void activate(LADSPA_Handle h) {
    Ducker *s = h;
    s->gain_db = 0; s->env = 0; s->hold_left = 0;
}

static inline float clampf(float v, float lo, float hi) { return v < lo ? lo : v > hi ? hi : v; }

static void run(LADSPA_Handle h, unsigned long n) {
    Ducker *s = h;
    LADSPA_Data **P = s->port;
    const float thresh = powf(10.0f, clampf(*P[P_THRESH], -90.0f, 0.0f) / 20.0f);
    const float depth = clampf(*P[P_DEPTH], 0.0f, 90.0f);
    const float attack_ms = clampf(*P[P_ATTACK], 0.1f, 10000.0f);
    const float hold_ms = clampf(*P[P_HOLD], 0.0f, 60000.0f);
    const float release_ms = clampf(*P[P_RELEASE], 0.1f, 60000.0f);
    const int mix = *P[P_MIX] > 0.5f;
    /* dB per sample for the two ramps (the full depth in attack_ms / release_ms) */
    const float down = depth / (attack_ms * 0.001f * s->rate);
    const float up = depth / (release_ms * 0.001f * s->rate);
    const long hold_n = (long)(hold_ms * 0.001f * s->rate);
    /* detector: instant attack, 20 ms release peak follower on the louder sidechain channel */
    const float det_rel = expf(-1.0f / (0.020f * s->rate));
    const LADSPA_Data *inl = P[P_IN_L], *inr = P[P_IN_R], *scl = P[P_SC_L], *scr = P[P_SC_R];
    LADSPA_Data *outl = P[P_OUT_L], *outr = P[P_OUT_R];
    float g = s->gain_db, env = s->env;
    long hold = s->hold_left;
    for (unsigned long i = 0; i < n; i++) {
        float a = fabsf(scl[i]), b = fabsf(scr[i]);
        float pk = a > b ? a : b;
        env = pk > env ? pk : env * det_rel;
        if (env >= thresh) {
            hold = hold_n;
            g -= down;
            if (g < -depth) g = -depth;
        } else if (hold > 0) {
            hold--;
            if (g < -depth) g = -depth;         /* depth lowered while held */
        } else {
            g += up;
            if (g > 0.0f) g = 0.0f;
        }
        float lin = powf(10.0f, g / 20.0f);
        outl[i] = inl[i] * lin + (mix ? scl[i] : 0.0f);
        outr[i] = inr[i] * lin + (mix ? scr[i] : 0.0f);
    }
    s->gain_db = g; s->env = env; s->hold_left = hold;
    if (P[P_GR]) *P[P_GR] = g;
}

static void cleanup(LADSPA_Handle h) { free(h); }

static const char *names[N_PORTS] = {
    "Threshold (dB)", "Depth (dB)", "Attack (ms)", "Hold (ms)", "Release (ms)", "Mix sidechain",
    "Gain reduction (dB)", "Program L", "Program R", "Sidechain L", "Sidechain R", "Out L", "Out R"};

static const LADSPA_PortDescriptor descs[N_PORTS] = {
    LADSPA_PORT_INPUT | LADSPA_PORT_CONTROL, LADSPA_PORT_INPUT | LADSPA_PORT_CONTROL, LADSPA_PORT_INPUT | LADSPA_PORT_CONTROL,
    LADSPA_PORT_INPUT | LADSPA_PORT_CONTROL, LADSPA_PORT_INPUT | LADSPA_PORT_CONTROL, LADSPA_PORT_INPUT | LADSPA_PORT_CONTROL,
    LADSPA_PORT_OUTPUT | LADSPA_PORT_CONTROL,
    LADSPA_PORT_INPUT | LADSPA_PORT_AUDIO, LADSPA_PORT_INPUT | LADSPA_PORT_AUDIO, LADSPA_PORT_INPUT | LADSPA_PORT_AUDIO,
    LADSPA_PORT_INPUT | LADSPA_PORT_AUDIO, LADSPA_PORT_OUTPUT | LADSPA_PORT_AUDIO, LADSPA_PORT_OUTPUT | LADSPA_PORT_AUDIO};

#define B LADSPA_HINT_BOUNDED_BELOW | LADSPA_HINT_BOUNDED_ABOVE
static const LADSPA_PortRangeHint hints[N_PORTS] = {
    {B | LADSPA_HINT_DEFAULT_MIDDLE, -90, 0},                 /* threshold -45 default; the block sets its own */
    {B | LADSPA_HINT_DEFAULT_MIDDLE, 0, 60},                  /* depth */
    {B | LADSPA_HINT_DEFAULT_LOW, 1, 5000},                   /* attack ms */
    {B | LADSPA_HINT_DEFAULT_LOW, 0, 10000},                  /* hold ms */
    {B | LADSPA_HINT_DEFAULT_LOW, 10, 10000},                 /* release ms */
    {B | LADSPA_HINT_TOGGLED | LADSPA_HINT_DEFAULT_1, 0, 1},  /* mix */
    {B, -90, 0},
    {0, 0, 0}, {0, 0, 0}, {0, 0, 0}, {0, 0, 0}, {0, 0, 0}, {0, 0, 0}};

static const LADSPA_Descriptor desc = {
    .UniqueID = 5007301,            /* unregistered; nothing else in the image uses it */
    .Label = "owl_ducker",
    .Properties = LADSPA_PROPERTY_HARD_RT_CAPABLE,
    .Name = "Owl Ducker (stereo, stereo sidechain)",
    .Maker = "owl-node",
    .Copyright = "MIT",
    .PortCount = N_PORTS,
    .PortDescriptors = descs,
    .PortNames = names,
    .PortRangeHints = hints,
    .instantiate = instantiate,
    .connect_port = connect_port,
    .activate = activate,
    .run = run,
    .cleanup = cleanup,
};

const LADSPA_Descriptor *ladspa_descriptor(unsigned long index) { return index == 0 ? &desc : NULL; }
