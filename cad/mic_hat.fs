FeatureScript 3095;
import(path : "onshape/std/common.fs", version : "3095.0");

// A cover over the reSpeaker Flex microphone array: a shallow cup (open side down) with a rounded top edge, radial
// slots over each of the four microphones and a central post that is glued to the board with double-sided tape.
annotation { "Feature Type Name" : "Mic Hat", "Feature Type Description" : "Slotted cover for the 4-microphone array" }
export const micHat = defineFeature(function(context is Context, id is Id, definition is map)
    precondition
    {
        annotation { "Name" : "Diameter" }
        isLength(definition.diameter, { (millimeter) : [10, 80, 500] } as LengthBoundSpec);
        annotation { "Name" : "Height" }
        isLength(definition.height, { (millimeter) : [2, 6, 100] } as LengthBoundSpec);
        annotation { "Name" : "Wall thickness" }
        isLength(definition.wall, { (millimeter) : [0.4, 1, 10] } as LengthBoundSpec);
        annotation { "Name" : "Top edge radius" }
        isLength(definition.edgeRadius, { (millimeter) : [0, 4, 50] } as LengthBoundSpec);
        annotation { "Name" : "Post diameter" }
        isLength(definition.postDiameter, { (millimeter) : [1, 40, 500] } as LengthBoundSpec);
        annotation { "Name" : "Tape thickness (post is this much shorter)" }
        isLength(definition.tape, { (millimeter) : [0, 1, 10] } as LengthBoundSpec);
        annotation { "Name" : "Microphone distance from centre" }
        isLength(definition.micRadius, { (millimeter) : [1, 36, 250] } as LengthBoundSpec);
        annotation { "Name" : "First microphone angle" }
        isAngle(definition.micAngle, { (degree) : [-360, 45, 360] } as AngleBoundSpec);
        annotation { "Name" : "Slots per microphone" }
        isInteger(definition.slotCount, { (unitless) : [1, 5, 50] } as IntegerBoundSpec);
        annotation { "Name" : "Slot length" }
        isLength(definition.slotLength, { (millimeter) : [0, 12, 100] } as LengthBoundSpec);
        annotation { "Name" : "Slot width" }
        isLength(definition.slotWidth, { (millimeter) : [0.2, 1, 20] } as LengthBoundSpec);
        annotation { "Name" : "Slot spacing (at the microphone)" }
        isLength(definition.slotSpacing, { (millimeter) : [0.2, 2, 50] } as LengthBoundSpec);
        annotation { "Name" : "Slot depth (from the top)" }
        isLength(definition.slotDepth, { (millimeter) : [0.1, 1.5, 100] } as LengthBoundSpec);
        annotation { "Name" : "Label", "Default" : "Keryx" }
        definition.label is string;
        annotation { "Name" : "Label height" }
        isLength(definition.labelHeight, { (millimeter) : [1, 8, 100] } as LengthBoundSpec);
        annotation { "Name" : "Ring diameter" }
        isLength(definition.ringDiameter, { (millimeter) : [0, 34, 500] } as LengthBoundSpec);
        annotation { "Name" : "Ring width" }
        isLength(definition.ringWidth, { (millimeter) : [0.2, 1, 20] } as LengthBoundSpec);
        annotation { "Name" : "Engrave label and ring", "Default" : true }
        definition.engrave is boolean;
        annotation { "Name" : "Label and ring depth / height" }
        isLength(definition.raise, { (millimeter) : [0.1, 0.6, 10] } as LengthBoundSpec);
    }
    {
        const r = definition.diameter / 2;
        const h = definition.height;
        const t = definition.wall;
        const rc = definition.edgeRadius;

        if (2 * t >= h || t >= r)
            throw regenError("Wall is too thick for this size", ["wall"]);
        if (rc > h - t || rc >= r)
            throw regenError("Top edge radius is too large", ["edgeRadius"]);
        if (h - t - definition.tape <= 0 * millimeter)
            throw regenError("Tape is thicker than the space under the top", ["tape"]);
        if (definition.postDiameter / 2 >= r - t)
            throw regenError("Post does not fit inside", ["postDiameter"]);
        if (definition.engrave && definition.raise >= t)
            throw regenError("Engraving must be shallower than the wall", ["raise"]);

        // Cup: an outer cylinder minus an inner one, both with the top edge rounded, so the wall is t everywhere
        fCylinder(context, id + "outer", {
                    "bottomCenter" : vector(0, 0, 0) * millimeter,
                    "topCenter" : vector(0, 0, 1) * h,
                    "radius" : r });
        const outerBody = qCreatedBy(id + "outer", EntityType.BODY);
        if (rc > 0 * millimeter)
        {
            opFillet(context, id + "outerRound", {
                        "entities" : qContainsPoint(qOwnedByBody(outerBody, EntityType.EDGE), vector(r, 0 * millimeter, h)),
                        "radius" : rc });
        }

        fCylinder(context, id + "inner", {
                    "bottomCenter" : vector(0, 0, -1) * millimeter,
                    "topCenter" : vector(0, 0, 1) * (h - t),
                    "radius" : r - t });
        const innerBody = qCreatedBy(id + "inner", EntityType.BODY);
        if (rc - t > 0 * millimeter)
        {
            opFillet(context, id + "innerRound", {
                        "entities" : qContainsPoint(qOwnedByBody(innerBody, EntityType.EDGE), vector(r - t, 0 * millimeter, h - t)),
                        "radius" : rc - t });
        }

        opBoolean(context, id + "hollow", {
                    "tools" : innerBody,
                    "targets" : outerBody,
                    "operationType" : BooleanOperationType.SUBTRACTION });

        // Post: from the tape gap up into the top plate
        fCylinder(context, id + "post", {
                    "bottomCenter" : vector(0, 0, 1) * definition.tape,
                    "topCenter" : vector(0, 0, 1) * (h - t / 2),
                    "radius" : definition.postDiameter / 2 });
        opBoolean(context, id + "join", {
                    "tools" : qUnion([outerBody, qCreatedBy(id + "post", EntityType.BODY)]),
                    "operationType" : BooleanOperationType.UNION });

        // Slots: rounded radial slots fanned around each microphone, cut from the top down to the slot depth (through
        // the top plate, and down the side wall where they run over the edge)
        const sketchZ = h - definition.slotDepth;
        const sketch = newSketchOnPlane(context, id + "slotSketch", {
                    "sketchPlane" : plane(vector(0, 0, 1) * sketchZ, vector(0, 0, 1), vector(1, 0, 0)) });
        const n = definition.slotCount;
        const half = definition.slotLength / 2;
        const w = definition.slotWidth;
        const step = definition.slotSpacing / definition.micRadius * radian;
        for (var m = 0; m < 4; m += 1)
        {
            for (var j = 0; j < n; j += 1)
            {
                const a = definition.micAngle + m * 90 * degree + (j - (n - 1) / 2) * step;
                const along = vector(cos(a), sin(a));
                const across = vector(-sin(a), cos(a));
                const p1 = along * (definition.micRadius - half);
                const p2 = along * (definition.micRadius + half);
                const name = "s" ~ m ~ "_" ~ j;
                if (half > 0 * millimeter)
                {
                    skPolyline(sketch, name ~ "r", {
                                "points" : [p1 + across * w / 2, p2 + across * w / 2, p2 - across * w / 2,
                                    p1 - across * w / 2, p1 + across * w / 2] });
                }
                skCircle(sketch, name ~ "a", { "center" : p1, "radius" : w / 2 });
                skCircle(sketch, name ~ "b", { "center" : p2, "radius" : w / 2 });
            }
        }
        skSolve(sketch);

        opExtrude(context, id + "slots", {
                    "entities" : qSketchRegion(id + "slotSketch"),
                    "direction" : vector(0, 0, 1),
                    "endBound" : BoundingType.BLIND,
                    "endDepth" : definition.slotDepth + 1 * millimeter });
        opBoolean(context, id + "cut", {
                    "tools" : qCreatedBy(id + "slots", EntityType.BODY),
                    "targets" : outerBody,
                    "operationType" : BooleanOperationType.SUBTRACTION });

        // Label and ring: engraved into the top plate, or raised above it (then they start 0.2 mm inside the plate so
        // the union is solid). Both are built as bodies between zBase and zTop.
        const zBase = definition.engrave ? h - definition.raise : h - 0.2 * millimeter;
        const zTop = definition.engrave ? h + 1 * millimeter : h + definition.raise;
        var marks = [];
        if (definition.label != "")
        {
            // Sketch text once to measure it, then again scaled to the label height and centred on the axis
            const probe = newSketchOnPlane(context, id + "labelProbe", {
                        "sketchPlane" : plane(vector(0, 0, 1) * zBase, vector(0, 0, 1), vector(1, 0, 0)) });
            skText(probe, "text", {
                        "text" : definition.label,
                        "fontName" : "OpenSans-Bold.ttf",
                        "firstCorner" : vector(0, 0) * millimeter,
                        "secondCorner" : vector(10, 10) * millimeter });
            skSolve(probe);
            const probeBox = evBox3d(context, { "topology" : qCreatedBy(id + "labelProbe", EntityType.BODY) });
            const k = definition.labelHeight / (probeBox.maxCorner[1] - probeBox.minCorner[1]);
            const centre = (probeBox.minCorner + probeBox.maxCorner) / 2;
            opDeleteBodies(context, id + "deleteProbe", { "entities" : qCreatedBy(id + "labelProbe", EntityType.BODY) });

            const labelSketch = newSketchOnPlane(context, id + "labelSketch", {
                        "sketchPlane" : plane(vector(0, 0, 1) * zBase, vector(0, 0, 1), vector(1, 0, 0)) });
            skText(labelSketch, "text", {
                        "text" : definition.label,
                        "fontName" : "OpenSans-Bold.ttf",
                        "firstCorner" : vector(-centre[0], -centre[1]) * k,
                        "secondCorner" : vector(10 * millimeter - centre[0], 10 * millimeter - centre[1]) * k });
            skSolve(labelSketch);
            opExtrude(context, id + "labelBody", {
                        "entities" : qSketchRegion(id + "labelSketch"),
                        "direction" : vector(0, 0, 1),
                        "endBound" : BoundingType.BLIND,
                        "endDepth" : zTop - zBase });
            marks = append(marks, qCreatedBy(id + "labelBody", EntityType.BODY));
        }
        if (definition.ringDiameter > 2 * definition.ringWidth)
        {
            fCylinder(context, id + "ringOuter", {
                        "bottomCenter" : vector(0, 0, 1) * zBase,
                        "topCenter" : vector(0, 0, 1) * zTop,
                        "radius" : definition.ringDiameter / 2 });
            fCylinder(context, id + "ringInner", {
                        "bottomCenter" : vector(0, 0, 1) * (zBase - 1 * millimeter),
                        "topCenter" : vector(0, 0, 1) * (zTop + 1 * millimeter),
                        "radius" : definition.ringDiameter / 2 - definition.ringWidth });
            opBoolean(context, id + "ringCut", {
                        "tools" : qCreatedBy(id + "ringInner", EntityType.BODY),
                        "targets" : qCreatedBy(id + "ringOuter", EntityType.BODY),
                        "operationType" : BooleanOperationType.SUBTRACTION });
            marks = append(marks, qCreatedBy(id + "ringOuter", EntityType.BODY));
        }
        if (size(marks) > 0 && definition.engrave)
        {
            opBoolean(context, id + "engrave", {
                        "tools" : qUnion(marks),
                        "targets" : outerBody,
                        "operationType" : BooleanOperationType.SUBTRACTION });
        }
        else if (size(marks) > 0)
        {
            opBoolean(context, id + "raise", {
                        "tools" : qUnion(concatenateArrays([[outerBody], marks])),
                        "operationType" : BooleanOperationType.UNION });
        }

        opDeleteBodies(context, id + "cleanup", {
                    "entities" : qUnion([qCreatedBy(id + "slotSketch", EntityType.BODY),
                                qCreatedBy(id + "labelSketch", EntityType.BODY)]) });
    }, {
        diameter : 80 * millimeter,
        height : 6 * millimeter,
        wall : 1 * millimeter,
        edgeRadius : 4 * millimeter,
        postDiameter : 40 * millimeter,
        tape : 1 * millimeter,
        micRadius : 36 * millimeter,
        micAngle : 45 * degree,
        slotCount : 5,
        slotLength : 12 * millimeter,
        slotWidth : 1 * millimeter,
        slotSpacing : 2 * millimeter,
        slotDepth : 1.5 * millimeter,
        label : "Keryx",
        labelHeight : 8 * millimeter,
        ringDiameter : 34 * millimeter,
        ringWidth : 1 * millimeter,
        engrave : true,
        raise : 0.6 * millimeter
    });
