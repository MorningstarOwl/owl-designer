/* owlrtpstat: a passthrough element that counts an RTP stream on the wire side of a receiver: packets, bytes, lost
 * packets (sequence-number gaps), late/duplicate packets and the largest arrival gap. Part of owl-node
 * (the design notes, "N4 as built").
 *
 * N4's JPEG XS receivers run RTP over plain UDP at 16-60 k packets/s. A Python pad probe per packet costs a large
 * share of a core at that rate, and gst-plugins-rs' depayloaders report no loss counters, so this C element sits
 * between udpsrc and rtpjxsvdepay. Read its "stats" property (a GstStructure) as often as you like; it never blocks
 * the streaming thread for more than a mutex. Buffers pass unchanged (passthrough: no copy; it reads 12 header bytes).
 *
 * SPDX-License-Identifier: LGPL-2.1-or-later
 */
#include <gst/gst.h>
#include <gst/base/gstbasetransform.h>

#define PACKAGE "owl-node"
#define VERSION "0.4.0"

typedef struct {
  GstBaseTransform parent;
  GMutex lock;
  guint64 packets, bytes, lost, late, resets, gaps;
  guint32 ssrc;
  gint last_seq;                /* -1 = none yet */
  GstClockTime last_arrival, max_gap;
} GstOwlRtpStat;
typedef struct { GstBaseTransformClass parent_class; } GstOwlRtpStatClass;

GType gst_owl_rtp_stat_get_type (void);
G_DEFINE_TYPE (GstOwlRtpStat, gst_owl_rtp_stat, GST_TYPE_BASE_TRANSFORM);

enum { PROP_0, PROP_STATS };

static GstStaticPadTemplate sink_t = GST_STATIC_PAD_TEMPLATE ("sink", GST_PAD_SINK, GST_PAD_ALWAYS, GST_STATIC_CAPS_ANY);
static GstStaticPadTemplate src_t = GST_STATIC_PAD_TEMPLATE ("src", GST_PAD_SRC, GST_PAD_ALWAYS, GST_STATIC_CAPS_ANY);

static GstFlowReturn
gst_owl_rtp_stat_transform_ip (GstBaseTransform * trans, GstBuffer * buf)
{
  GstOwlRtpStat *s = (GstOwlRtpStat *) trans;
  guint8 h[12];
  gsize size = gst_buffer_get_size (buf);
  GstClockTime now = g_get_monotonic_time () * 1000;

  if (size < 12 || gst_buffer_extract (buf, 0, h, 12) != 12 || (h[0] >> 6) != 2)
    return GST_FLOW_OK;         /* not RTP v2: pass it on, do not count it */

  guint seq = (h[2] << 8) | h[3];
  guint32 ssrc = ((guint32) h[8] << 24) | (h[9] << 16) | (h[10] << 8) | h[11];

  g_mutex_lock (&s->lock);
  s->packets++;
  s->bytes += size;
  if (s->last_arrival != GST_CLOCK_TIME_NONE && now - s->last_arrival > s->max_gap)
    s->max_gap = now - s->last_arrival;
  s->last_arrival = now;
  if (s->last_seq < 0 || ssrc != s->ssrc) {
    if (s->last_seq >= 0)
      s->resets++;              /* a new sender (or a restarted one): start counting afresh */
    s->ssrc = ssrc;
    s->last_seq = seq;
  } else {
    guint d = (seq - (guint) s->last_seq) & 0xffff;
    if (d == 0 || d > 0x8000) {
      s->late++;                /* duplicate or older than the newest one */
    } else {
      if (d > 1) {
        s->lost += d - 1;
        s->gaps++;
      }
      s->last_seq = seq;
    }
  }
  g_mutex_unlock (&s->lock);
  return GST_FLOW_OK;
}

static void
gst_owl_rtp_stat_get_property (GObject * obj, guint id, GValue * v, GParamSpec * ps)
{
  GstOwlRtpStat *s = (GstOwlRtpStat *) obj;
  if (id != PROP_STATS) {
    G_OBJECT_WARN_INVALID_PROPERTY_ID (obj, id, ps);
    return;
  }
  g_mutex_lock (&s->lock);
  GstStructure *st = gst_structure_new ("application/x-owl-rtp-stats",
      "packets", G_TYPE_UINT64, s->packets, "bytes", G_TYPE_UINT64, s->bytes,
      "lost", G_TYPE_UINT64, s->lost, "gaps", G_TYPE_UINT64, s->gaps, "late", G_TYPE_UINT64, s->late,
      "ssrc-changes", G_TYPE_UINT64, s->resets, "ssrc", G_TYPE_UINT, s->ssrc,
      "max-gap-us", G_TYPE_UINT64, s->max_gap / 1000, NULL);
  s->max_gap = 0;               /* per read: the largest arrival gap since the last read */
  g_mutex_unlock (&s->lock);
  g_value_take_boxed (v, st);
}

static gboolean
gst_owl_rtp_stat_start (GstBaseTransform * trans)
{
  GstOwlRtpStat *s = (GstOwlRtpStat *) trans;
  g_mutex_lock (&s->lock);
  s->packets = s->bytes = s->lost = s->late = s->resets = s->gaps = 0;
  s->last_seq = -1;
  s->last_arrival = GST_CLOCK_TIME_NONE;
  s->max_gap = 0;
  g_mutex_unlock (&s->lock);
  return TRUE;
}

static void
gst_owl_rtp_stat_finalize (GObject * obj)
{
  g_mutex_clear (&((GstOwlRtpStat *) obj)->lock);
  G_OBJECT_CLASS (gst_owl_rtp_stat_parent_class)->finalize (obj);
}

static void
gst_owl_rtp_stat_class_init (GstOwlRtpStatClass * klass)
{
  GObjectClass *oc = G_OBJECT_CLASS (klass);
  GstElementClass *ec = GST_ELEMENT_CLASS (klass);
  GstBaseTransformClass *bc = GST_BASE_TRANSFORM_CLASS (klass);

  oc->get_property = gst_owl_rtp_stat_get_property;
  oc->finalize = gst_owl_rtp_stat_finalize;
  g_object_class_install_property (oc, PROP_STATS, g_param_spec_boxed ("stats", "Statistics",
          "packets, bytes, lost (sequence gaps), gaps, late, ssrc-changes, max-gap-us (since the last read)",
          GST_TYPE_STRUCTURE, G_PARAM_READABLE | G_PARAM_STATIC_STRINGS));
  gst_element_class_add_static_pad_template (ec, &sink_t);
  gst_element_class_add_static_pad_template (ec, &src_t);
  gst_element_class_set_static_metadata (ec, "owl-node RTP counter", "Filter/Network/RTP",
      "Counts RTP packets, sequence gaps and arrival gaps (passthrough)", "owl-node");
  bc->transform_ip = gst_owl_rtp_stat_transform_ip;
  bc->start = gst_owl_rtp_stat_start;
  bc->passthrough_on_same_caps = TRUE;
  bc->transform_ip_on_passthrough = TRUE;   /* passthrough: the buffer is never made writable (no copies) */
}

static void
gst_owl_rtp_stat_init (GstOwlRtpStat * s)
{
  g_mutex_init (&s->lock);
  s->last_seq = -1;
  s->last_arrival = GST_CLOCK_TIME_NONE;
  gst_base_transform_set_passthrough (GST_BASE_TRANSFORM (s), TRUE);
}

static gboolean
plugin_init (GstPlugin * plugin)
{
  return gst_element_register (plugin, "owlrtpstat", GST_RANK_NONE, gst_owl_rtp_stat_get_type ());
}

GST_PLUGIN_DEFINE (GST_VERSION_MAJOR, GST_VERSION_MINOR, owlrtpstat, "owl-node RTP stream counter", plugin_init,
    VERSION, "LGPL", PACKAGE, "https://github.com/MorningstarOwl")
