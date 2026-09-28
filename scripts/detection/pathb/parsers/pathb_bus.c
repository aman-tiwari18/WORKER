/*
 * Bus filter for the Plan B pipelines (loaded with ctypes by pathb/ds_pipeline.py).
 *
 * With keyframes-only decoding (nvurisrcbin dec-skip-frames=2) every skipped frame
 * makes the decoder post a QoS message: ~24 per camera per second at 25 fps. They
 * are forwarded up through nvurisrcbin/uridecodebin/decodebin to the pipeline bus
 * and queued for the Python bus watch. Measured on DeepStream 7.1: 100 cameras =
 * ~2,700 messages/s, and within 1-3 minutes the decoder threads spin in
 * gst_bus_post and the whole pipeline stops producing frames.
 *
 * This sync handler drops QoS messages before they are queued, in C, so no Python
 * runs per message. Everything else (errors, EOS, stream-eos, warnings) passes.
 */
#include <gst/gst.h>

static GstBusSyncReply drop_qos(GstBus *bus, GstMessage *msg, gpointer data)
{
    (void)bus; (void)data;
    return GST_MESSAGE_TYPE(msg) == GST_MESSAGE_QOS ? GST_BUS_DROP : GST_BUS_PASS;
}

void pathb_drop_qos_messages(GstBus *bus)
{
    gst_bus_set_sync_handler(bus, drop_qos, NULL, NULL);
}
