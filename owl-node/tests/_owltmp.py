"""Point owl-node's state and run directories at a temporary folder for one test. The real ones are /var/lib/owl-node
and /run/owl-node: a test must never write there (as root it silently would; on a CI runner it fails). Setting
OWL_NODE_STATE in a test module is not enough: owlnode.config reads it once, when the first test module imports it."""
import os, tempfile
from unittest import mock

from owlnode import config


def isolate(testcase):
    d = tempfile.mkdtemp(prefix="owl-test-")
    st, run = os.path.join(d, "state"), os.path.join(d, "run")
    p = mock.patch.multiple(config, STATE_DIR=st, RUN_DIR=run, STATE=os.path.join(st, "state.json"),
                            HUB_FILE=os.path.join(st, "hub.json"), AUDIO_STATUS=os.path.join(run, "audio.json"),
                            VIDEO_STATUS=os.path.join(run, "video-{}.json"), PTP_STATUS=os.path.join(run, "ptp.json"))
    p.start()
    testcase.addCleanup(p.stop)
    return d
