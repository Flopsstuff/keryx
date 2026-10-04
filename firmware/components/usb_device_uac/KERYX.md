# Vendored `espressif/usb_device_uac`

Copied from the ESP Component Registry, version 1.3.1
([source](https://github.com/espressif/esp-iot-solution/tree/master/components/usb/usb_device_uac)), Apache-2.0
(see `license.txt`). Registry metadata (`.component_hash`, `CHECKSUMS.json`) and `test_apps/` were dropped.

## Local changes

- `tusb/usb_descriptors.c`: string descriptor 4, the audio control interface, returns `CONFIG_UAC_TUSB_PRODUCT`
  instead of the hard-coded `"usb uac"`. macOS names the sound card after this string.
- `Kconfig`, `tusb_uac/tusb_config_uac.h`, `tusb_uac/uac_descriptors.h`, `tusb/usb_descriptors.c`: option
  `CONFIG_UAC_CDC` adds a CDC ACM interface (endpoints 0x83 notification, 0x04/0x84 data; string "<product> console"),
  making a composite device. The ESP32-S3 allows 5 IN endpoints including EP0, so this takes the last two. The
  application uses it through TinyUSB's `tud_cdc_*` (see `components/keryx_console`).
- `tusb/usb_descriptors.c`: the serial number string comes from `uac_serial_number()`, a weak function returning
  `CONFIG_UAC_TUSB_SERIAL_NUM` that the application can override.

- `Kconfig.uac`: the USB task priorities may go up to 24 (upstream: 15), so that USB audio can run above lwIP's
  task (18) and Wi-Fi traffic does not starve it.

When updating to a newer upstream version, copy it over this directory and re-apply the changes above.
