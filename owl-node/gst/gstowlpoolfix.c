/* owlpoolfix: a passthrough element that lets upstream GStreamer's v4l2slh265dec (Raspberry Pi 5, rpi-hevc-dec)
 * negotiate its SAND128 output. Part of owl-node (the design notes, "N3 as built").
 *
 * The Pi 5's stateless HEVC decoder only writes NV12 in 128-byte columns (V4L2 'NC12'), which GStreamer 1.28 exposes
 * as video/x-raw(memory:DMABuf), format=DMA_DRM, drm-format=NV12:0x0700000000000004. v4l2slh265dec then requires
 * downstream to support GstVideoMeta, and GstVideoDecoder's default decide_allocation configures a pool with those
 * caps; GstVideoBufferPool rejects them (no GstVideoFormat for SAND128), and kmssink offers no pool for non-linear
 * modifiers, so negotiation fails with every sink ("Failed to configure the buffer pool").
 *
 * Placed right after the decoder, owlpoolfix forwards the allocation query downstream, then adds GstVideoMeta and,
 * when nobody offered a pool for DMA_DRM caps, a plain GstBufferPool (which accepts any caps). The decoder keeps
 * using its own V4L2 buffers (zero copy); the plain pool is only its never-used fallback. Buffers pass unchanged.
 *
 * SPDX-License-Identifier: LGPL-2.1-or-later
 */
#ifdef HAVE_CONFIG_H
#include "config.h"
#endif
#include <gst/gst.h>
#include <gst/base/gstbasetransform.h>
#include <gst/video/video.h>

#define PACKAGE "owl-node"
#define VERSION "0.3.0"

typedef struct { GstBaseTransform parent; } GstOwlPoolFix;
typedef struct { GstBaseTransformClass parent_class; } GstOwlPoolFixClass;

GType gst_owl_pool_fix_get_type (void);
G_DEFINE_TYPE (GstOwlPoolFix, gst_owl_pool_fix, GST_TYPE_BASE_TRANSFORM);

static GstStaticPadTemplate sink_t = GST_STATIC_PAD_TEMPLATE ("sink", GST_PAD_SINK, GST_PAD_ALWAYS, GST_STATIC_CAPS_ANY);
static GstStaticPadTemplate src_t = GST_STATIC_PAD_TEMPLATE ("src", GST_PAD_SRC, GST_PAD_ALWAYS, GST_STATIC_CAPS_ANY);

static gboolean
gst_owl_pool_fix_propose_allocation (GstBaseTransform * trans, GstQuery * decide_query, GstQuery * query)
{
  GstCaps *caps = NULL;
  gboolean need_pool = FALSE;

  /* passthrough: downstream (kmssink, appsink, ...) answers first */
  gst_pad_peer_query (GST_BASE_TRANSFORM_SRC_PAD (trans), query);

  gst_query_parse_allocation (query, &caps, &need_pool);
  if (!gst_query_find_allocation_meta (query, GST_VIDEO_META_API_TYPE, NULL))
    gst_query_add_allocation_meta (query, GST_VIDEO_META_API_TYPE, NULL);

  if (caps && gst_video_is_dma_drm_caps (caps) && gst_query_get_n_allocation_pools (query) == 0) {
    GstVideoInfoDmaDrm drm;
    guint size = 0;
    GstBufferPool *pool = gst_buffer_pool_new ();

    if (gst_video_info_dma_drm_from_caps (&drm, caps))
      size = GST_VIDEO_INFO_SIZE (&drm.vinfo);
    if (size == 0)
      size = 4096;
    gst_query_add_allocation_pool (query, pool, size, 2, 0);
    gst_object_unref (pool);
    GST_DEBUG_OBJECT (trans, "added a plain fallback pool and GstVideoMeta for %" GST_PTR_FORMAT, caps);
  }
  return TRUE;
}

static void
gst_owl_pool_fix_class_init (GstOwlPoolFixClass * klass)
{
  GstElementClass *ec = GST_ELEMENT_CLASS (klass);
  GstBaseTransformClass *bt = GST_BASE_TRANSFORM_CLASS (klass);

  gst_element_class_set_static_metadata (ec, "owl-node allocation fix", "Filter/Video",
      "Passthrough that answers allocation queries with GstVideoMeta and a plain pool, so v4l2slh265dec's SAND128 "
      "DMA_DRM output (Raspberry Pi 5) negotiates", "owl-node");
  gst_element_class_add_static_pad_template (ec, &sink_t);
  gst_element_class_add_static_pad_template (ec, &src_t);
  bt->passthrough_on_same_caps = TRUE;
  bt->propose_allocation = GST_DEBUG_FUNCPTR (gst_owl_pool_fix_propose_allocation);
}

static void
gst_owl_pool_fix_init (GstOwlPoolFix * self)
{
  gst_base_transform_set_passthrough (GST_BASE_TRANSFORM (self), TRUE);
}

static gboolean
plugin_init (GstPlugin * plugin)
{
  return gst_element_register (plugin, "owlpoolfix", GST_RANK_NONE, gst_owl_pool_fix_get_type ());
}

GST_PLUGIN_DEFINE (GST_VERSION_MAJOR, GST_VERSION_MINOR, owlpoolfix, "owl-node helpers", plugin_init, VERSION,
    "LGPL", PACKAGE, "https://github.com/MorningstarOwl")
