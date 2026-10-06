# Assembly: Keryx in an Edifier R1100

The first Keryx lives in an Edifier R1100, an active bookshelf speaker: the reSpeaker Flex with the XIAO on top of
the cabinet, the LED ring behind the front grille, the knob on the back panel. The speaker keeps its own amplifier
and is what plays Keryx.

| Part | Where |
|---|---|
| reSpeaker Flex + XIAO ESP32S3 | on top of the cabinet, the microphone board under the metal ring on the top |
| NeoDriver + ring of 24 WS2812 | the ring behind the front grille, around the tweeter |
| Rotary encoder (Adafruit 4991) | on the back panel, on an aluminium plate where the speaker's input was |
| Audio | the Flex's SPEAKER output (amplifier on 12 V) through a 140:140 Ω isolation transformer into the speaker's own input |
| Power | 12 V for the Flex; a DC-DC to 5 V for the ring |

The encoder and the NeoDriver are on their own I2C bus (D0/D3), not the XVF3800's: see
[respeaker-flex-xvf3800.md](respeaker-flex-xvf3800.md#what-is-free-for-our-own-peripherals). The transformer keeps
the speaker's ground apart from the board's: wired straight into the input, the speaker's ground knocked the board
off USB.

## Photos

The finished speaker: the clock on the ring shows through the grille (the hands at 12, the marks at 3, 6 and 9).

![The finished speaker](images/assembly/0.jpg)

The bench: the Flex with the XIAO, the encoder, the NeoDriver and the ring, the DC-DC, and the speaker's amplifier
board with its back panel.

![The bench](images/assembly/1.jpg)

The ring lit blue (listening) next to the Flex and the speaker's amplifier.

![The ring and the Flex](images/assembly/2.jpg)

The Flex on top of the cabinet, wired to the speaker's input and the peripherals' bus.

![The Flex on the cabinet](images/assembly/3.jpg)

The Flex and the speaker's amplifier board, with the DC-DC for the ring.

![The Flex and the amplifier](images/assembly/4.jpg)

The cabinet open: the woofer out, the ring fitted in the front panel, the Flex on top.

![The cabinet open](images/assembly/5.jpg)

Fitting the Flex on the top and the amplifier board back into the cabinet.

![Fitting it in](images/assembly/6.jpg)

The back: the speaker's volume and bass, and the Keryx knob on an aluminium plate.

![The back panel](images/assembly/7.jpg)

Done.

![Done](images/assembly/8.jpg)
