"""USB control client for the XMOS XVF3800 on the reSpeaker Flex.

Parameters are read and written with vendor control transfers: wIndex is the resource id, wValue the command id
(with 0x80 set for reads), and the first byte of every read response is a status code. The parameter table below
is a subset of `python_control/xvf_host.py` from https://github.com/respeaker/reSpeaker_Flex.
"""

import struct
import threading
import time

import usb.core
import usb.util

SEEED_VID = 0x2886

STATUS_OK = 0
STATUS_RETRY = 64

# name: (resid, cmdid, count, type)
PARAMETERS = {
    "VERSION": (48, 0, 3, "uint8"),
    "BLD_MSG": (48, 1, 50, "char"),
    "USB_BIT_DEPTH": (48, 8, 2, "uint8"),
    "AEC_NUM_MICS": (33, 71, 1, "int32"),
    "AEC_MIC_ARRAY_TYPE": (33, 73, 1, "int32"),
    "AEC_MIC_ARRAY_GEO": (33, 74, 12, "float"),
    "AEC_AZIMUTH_VALUES": (33, 75, 4, "float"),
    "AEC_SPENERGY_VALUES": (33, 80, 4, "float"),
    "AEC_AECCONVERGED": (33, 3, 1, "int32"),
    "AEC_ASROUTONOFF": (33, 35, 1, "int32"),
    "AEC_FIXEDBEAMSONOFF": (33, 37, 1, "int32"),
    "AUDIO_MGR_MIC_GAIN": (35, 0, 1, "float"),
    "AUDIO_MGR_REF_GAIN": (35, 1, 1, "float"),
    "AUDIO_MGR_SELECTED_AZIMUTHS": (35, 11, 2, "float"),
    "AUDIO_MGR_OP_L": (35, 15, 2, "uint8"),
    "AUDIO_MGR_OP_R": (35, 19, 2, "uint8"),
    "AUDIO_MGR_SYS_DELAY": (35, 26, 1, "int32"),
    "AUDIO_MGR_OP_CH3": (35, 28, 2, "uint8"),
    "AUDIO_MGR_OP_CH4": (35, 29, 2, "uint8"),
    "AUDIO_MGR_OP_CH5": (35, 30, 2, "uint8"),
    "AUDIO_MGR_OP_CH6": (35, 31, 2, "uint8"),
    "PP_AGCONOFF": (17, 10, 1, "int32"),
    "PP_AGCGAIN": (17, 13, 1, "float"),
    "LED_EFFECT": (20, 12, 1, "uint8"),
    "DOA_VALUE": (20, 18, 2, "uint16"),
}

FORMATS = {"uint8": "B", "char": "B", "uint16": "H", "int32": "i", "uint32": "I", "float": "f"}

# USB input channel (0-based) -> the mux command that feeds it
CHANNEL_MUX = [
    "AUDIO_MGR_OP_L",
    "AUDIO_MGR_OP_R",
    "AUDIO_MGR_OP_CH3",
    "AUDIO_MGR_OP_CH4",
    "AUDIO_MGR_OP_CH5",
    "AUDIO_MGR_OP_CH6",
]


class XVFError(RuntimeError):
    pass


class XVF:
    TIMEOUT_MS = 1000

    def __init__(self, vid=SEEED_VID, pid=None):
        kwargs = {"idVendor": vid}
        if pid is not None:
            kwargs["idProduct"] = pid
        self.dev = usb.core.find(**kwargs)
        if self.dev is None:
            raise XVFError(f"no XVF3800 found on USB (VID 0x{vid:04x})")
        self.lock = threading.Lock()

    @property
    def product(self):
        return usb.util.get_string(self.dev, self.dev.iProduct)

    def read(self, name):
        resid, cmdid, count, kind = PARAMETERS[name]
        fmt = "<" + FORMATS[kind] * count
        length = struct.calcsize(fmt) + 1
        with self.lock:
            for _ in range(100):
                response = self.dev.ctrl_transfer(
                    usb.util.CTRL_IN | usb.util.CTRL_TYPE_VENDOR | usb.util.CTRL_RECIPIENT_DEVICE,
                    0, 0x80 | cmdid, resid, length, self.TIMEOUT_MS)
                if response[0] == STATUS_OK:
                    break
                if response[0] != STATUS_RETRY:
                    raise XVFError(f"{name}: status {response[0]}")
                time.sleep(0.002)
            else:
                raise XVFError(f"{name}: device kept asking to retry")
        payload = bytes(response[1:])
        if kind == "char":
            return payload.rstrip(b"\x00").decode("ascii", errors="replace")
        return struct.unpack(fmt, payload)

    def write(self, name, values):
        resid, cmdid, count, kind = PARAMETERS[name]
        if len(values) != count:
            raise XVFError(f"{name} takes {count} values, got {len(values)}")
        payload = struct.pack("<" + FORMATS[kind] * count, *values)
        with self.lock:
            self.dev.ctrl_transfer(
                usb.util.CTRL_OUT | usb.util.CTRL_TYPE_VENDOR | usb.util.CTRL_RECIPIENT_DEVICE,
                0, cmdid, resid, payload, self.TIMEOUT_MS)

    def mic_geometry(self):
        """Microphone XY positions in metres, one (x, y) pair per mic."""
        geo = self.read("AEC_MIC_ARRAY_GEO")
        return [(geo[i], geo[i + 1]) for i in range(0, len(geo), 3)]

    def channel_routing(self):
        """(category, source) feeding each USB input channel."""
        return [tuple(self.read(name)) for name in CHANNEL_MUX]

    def close(self):
        usb.util.dispose_resources(self.dev)
