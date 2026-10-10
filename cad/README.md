# CAD

Printed parts for the speaker build ([assembly](../docs/assembly.md)). Each part is an Onshape custom feature
written in FeatureScript: the `.fs` file is the source of the feature, the `.stl` next to it is the part as printed
(exported from Onshape in millimetres, Git LFS). Every size is a parameter of the feature, so a part is changed in
its dialog in Onshape rather than in a sketch; the STL holds the values it was exported with, which may differ from
the defaults in the `.fs`.

| Part | Files | What it is |
|---|---|---|
| Mic hat | `mic_hat.fs`, `mic_hat.stl` | the cover over the reSpeaker Flex's microphone array |
| Encoder knob | `encoder_knob.fs`, `encoder_knob.stl` | the knob for the rotary encoder on the back panel |
| Bottle cap | `bottle_cap.fs`, `bottle_cap.stl` | a test piece, the first part made this way: a threaded screw cap |

## Mic hat

A shallow cup, 80 × 6 mm, open side down, with a 1 mm wall and a 4 mm round on the top edge. Over each of the four
microphones (on the diagonals of the 44 mm square, see
[the board notes](../docs/respeaker-flex-xvf3800.md#hardware)) a fan of radial slots lets the sound in; the slot
depth can take them down the side wall where they run over the edge. A post in the middle stops 1 mm short of the
rim and is glued to the board with 1 mm double-sided tape. "Keryx" and a ring around it are engraved into the top
(or raised: a checkbox).

Print it top down: the engraved label is on the bed.

## Encoder knob

For the EC11-type encoder on the Adafruit 4991 board: a 6 mm knurled split shaft, an M7 bushing, a nut and washer.
Heights are measured from the panel. The knob is fluted, tapers slightly, has a rounded top with a finger dish, and
sits a gap above the panel so the encoder's push switch keeps its travel. Inside:

- a skirt over the nut and washer, with chamfers on its inner and outer bottom edges;
- above it a single cone, at the overhang angle (45° from horizontal by default), down to the bore. Its start is
  computed so it clears the nut and the bushing; there are no flat ceilings, so the knob prints skirt-down without
  supports (the only bridge is the ceiling over the shaft's end);
- a bore looser than the shaft, held by thin crush ribs that reach just inside the shaft's radius, with a lead-in
  chamfer at their lower ends so the knob presses on easily. "Rib interference" sets how tight it is.

## Editing the parts

The features live in the Onshape document "keryx", one Feature Studio each (Bottle Cap, Mic Hat, Encoder Knob),
each inserted into a Part Studio. To change a feature's code, edit the `.fs` here and paste it into its Feature
Studio, or write it there with Onshape's [FeatureScript MCP server][fs-mcp] (`.mcp.json` in the repository's root
connects it; every call spends the account's Onshape API allocation, 2,500 requests a year on the account used so
far). Things learned while writing these:

[fs-mcp]: https://www.onshape.com/en/blog/featurescript-mcp-server-enables-text-code-cad

- `box` is a reserved word: a variable called `box` fails to parse.
- `fSphere` takes its centre as a vertex query, not a vector: make a point with `opPoint` first (`sphereAt` in
  `encoder_knob.fs`).
- Writing a Feature Studio through the MCP server reports no errors even when the code does not parse, and
  `test_feature` then only says "no features found": the real errors are in the FeatureScript notices panel in
  Onshape.
