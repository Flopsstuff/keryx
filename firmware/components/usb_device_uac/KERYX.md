# Vendored `espressif/usb_device_uac`

Copied from the ESP Component Registry, version 1.3.1
([source](https://github.com/espressif/esp-iot-solution/tree/master/components/usb/usb_device_uac)), Apache-2.0
(see `license.txt`). Registry metadata (`.component_hash`, `CHECKSUMS.json`) and `test_apps/` were dropped.

## Local changes

- `tusb/usb_descriptors.c`: string descriptor 4, the audio control interface, returns `CONFIG_UAC_TUSB_PRODUCT`
  instead of the hard-coded `"usb uac"`. macOS names the sound card after this string.

When updating to a newer upstream version, copy it over this directory and re-apply the changes above.
