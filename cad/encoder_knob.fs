FeatureScript 3095;
import(path : "onshape/std/common.fs", version : "3095.0");

// A knob for the panel's rotary encoder (EC11 type: 6 mm knurled split shaft, M7 bushing, nut and washer). Heights
// are measured from the panel the encoder is mounted on; the knob's bottom sits a gap above it so the push switch
// still has travel. A skirt hides the nut and washer; above it a single cone narrows to the bore, steep enough to print
// skirt-down without supports while clearing the nut and the bushing. The shaft is held by thin crush ribs in a looser
// bore, with a lead-in chamfer at their bottom so the knob presses on easily.
annotation { "Feature Type Name" : "Encoder Knob", "Feature Type Description" : "Fluted knob for a knurled 6 mm encoder shaft" }
export const encoderKnob = defineFeature(function(context is Context, id is Id, definition is map)
    precondition
    {
        annotation { "Group Name" : "Knob", "Collapsed By Default" : false }
        {
            annotation { "Name" : "Diameter" }
            isLength(definition.diameter, { (millimeter) : [8, 20, 200] } as LengthBoundSpec);
            annotation { "Name" : "Taper (radius lost at the top)" }
            isLength(definition.taper, { (millimeter) : [0, 0.8, 20] } as LengthBoundSpec);
            annotation { "Name" : "Top thickness above the shaft" }
            isLength(definition.topThickness, { (millimeter) : [0.5, 2.5, 50] } as LengthBoundSpec);
            annotation { "Name" : "Top edge radius" }
            isLength(definition.topRound, { (millimeter) : [0, 2, 20] } as LengthBoundSpec);
            annotation { "Name" : "Skirt outer chamfer" }
            isLength(definition.skirtOuterChamfer, { (millimeter) : [0, 1, 5] } as LengthBoundSpec);
            annotation { "Name" : "Skirt inner chamfer" }
            isLength(definition.skirtInnerChamfer, { (millimeter) : [0, 1, 5] } as LengthBoundSpec);
            annotation { "Name" : "Finger dish depth" }
            isLength(definition.dishDepth, { (millimeter) : [0, 0.6, 5] } as LengthBoundSpec);
            annotation { "Name" : "Flutes" }
            isInteger(definition.fluteCount, { (unitless) : [0, 20, 200] } as IntegerBoundSpec);
            annotation { "Name" : "Flute radius" }
            isLength(definition.fluteRadius, { (millimeter) : [0.2, 1.2, 20] } as LengthBoundSpec);
            annotation { "Name" : "Flute depth" }
            isLength(definition.fluteDepth, { (millimeter) : [0.05, 0.6, 10] } as LengthBoundSpec);
        }
        annotation { "Group Name" : "Encoder", "Collapsed By Default" : false }
        {
            annotation { "Name" : "Shaft length above the panel" }
            isLength(definition.shaftLength, { (millimeter) : [2, 16, 100] } as LengthBoundSpec);
            annotation { "Name" : "Gap above the panel" }
            isLength(definition.gap, { (millimeter) : [0, 1, 20] } as LengthBoundSpec);
            annotation { "Name" : "Nut and washer diameter" }
            isLength(definition.nutDiameter, { (millimeter) : [1, 12, 100] } as LengthBoundSpec);
            annotation { "Name" : "Nut and washer height" }
            isLength(definition.nutHeight, { (millimeter) : [0, 2, 50] } as LengthBoundSpec);
            annotation { "Name" : "Bushing diameter" }
            isLength(definition.bushingDiameter, { (millimeter) : [1, 7, 50] } as LengthBoundSpec);
            annotation { "Name" : "Bushing height above the panel (0: none)" }
            isLength(definition.bushingHeight, { (millimeter) : [0, 5, 50] } as LengthBoundSpec);
            annotation { "Name" : "Overhang angle of the inner cone (from horizontal)" }
            isAngle(definition.overhangAngle, { (degree) : [20, 45, 89] } as AngleBoundSpec);
            annotation { "Name" : "Clearance around nut and bushing" }
            isLength(definition.clearance, { (millimeter) : [0, 0.5, 5] } as LengthBoundSpec);
            annotation { "Name" : "Shaft diameter" }
            isLength(definition.shaftDiameter, { (millimeter) : [1, 6, 50] } as LengthBoundSpec);
            annotation { "Name" : "Gap around the shaft (bore radius minus shaft radius)" }
            isLength(definition.boreGap, { (millimeter) : [0.1, 0.6, 5] } as LengthBoundSpec);
            annotation { "Name" : "Grip ribs (0: plain bore)" }
            isInteger(definition.ribCount, { (unitless) : [0, 8, 64] } as IntegerBoundSpec);
            annotation { "Name" : "Rib width" }
            isLength(definition.ribWidth, { (millimeter) : [0.2, 0.6, 5] } as LengthBoundSpec);
            annotation { "Name" : "Rib interference (into the shaft radius)" }
            isLength(definition.ribInterference, { (millimeter) : [-1, 0.1, 1] } as LengthBoundSpec);
            annotation { "Name" : "Rib lead-in chamfer height" }
            isLength(definition.ribLeadIn, { (millimeter) : [0, 1.5, 20] } as LengthBoundSpec);
        }
    }
    {
        // Knob coordinates: z = 0 at the knob's bottom, which is `gap` above the panel
        const g = definition.gap;
        const rb = definition.diameter / 2;
        const rt = rb - definition.taper;
        const boreTop = definition.shaftLength - g + 0.3 * millimeter;
        const h = boreTop + definition.topThickness;
        const rNut = definition.nutDiameter / 2 + definition.clearance;
        const nutTop = definition.nutHeight + 0.3 * millimeter - g;
        const rShaft = definition.shaftDiameter / 2;
        const rBore = rShaft + definition.boreGap;
        const rTip = rShaft - definition.ribInterference;
        const bushingTop = definition.bushingHeight + 0.3 * millimeter - g;
        const rBushing = definition.bushingDiameter / 2 + definition.clearance;
        // The cone narrows by `slope` per unit of height. It starts at the nut pocket's radius as low as it can while
        // still clearing the bushing's top corner, and ends at the bore, where the ribs begin.
        const slope = 1 / tan(definition.overhangAngle);
        var coneStart = nutTop;
        if (definition.bushingHeight > 0 * millimeter)
            coneStart = max(coneStart, bushingTop - (rNut - rBushing) / slope);
        const coneEnd = coneStart + (rNut - rBore) / slope;
        const ribStart = coneEnd;

        if (rt <= rNut + 0.8 * millimeter)
            throw regenError("Knob is too narrow for the nut skirt", ["diameter"]);
        if (definition.topRound >= h / 2 || definition.topRound >= rt)
            throw regenError("Top edge radius is too large", ["topRound"]);
        if (nutTop >= boreTop)
            throw regenError("Nut is taller than the shaft", ["nutHeight"]);
        if (definition.skirtInnerChamfer >= coneStart)
            throw regenError("Skirt inner chamfer is deeper than the nut pocket", ["skirtInnerChamfer"]);
        if (rNut + definition.skirtInnerChamfer + definition.skirtOuterChamfer >= rb - definition.fluteDepth - 0.4 * millimeter)
            throw regenError("Skirt chamfers leave no bottom face", ["skirtInnerChamfer"]);
        if (rBushing >= rNut && definition.bushingHeight > 0 * millimeter)
            throw regenError("Bushing is wider than the nut pocket", ["bushingDiameter"]);
        if (rBore >= rNut)
            throw regenError("Bore is wider than the nut pocket", ["boreGap"]);
        if (coneEnd >= boreTop - 1 * millimeter)
            throw regenError("The inner cone leaves no room for the shaft: lower the overhang angle", ["overhangAngle"]);
        if (rTip >= rBore)
            throw regenError("Ribs do not reach into the bore", ["ribInterference"]);

        // Body: a gently tapered cylinder with a rounded top and a chamfered bottom outer edge
        fCone(context, id + "body", {
                    "bottomCenter" : vector(0, 0, 0) * millimeter,
                    "topCenter" : vector(0, 0, 1) * h,
                    "bottomRadius" : rb,
                    "topRadius" : rt });
        const knob = qCreatedBy(id + "body", EntityType.BODY);
        if (definition.topRound > 0 * millimeter)
        {
            opFillet(context, id + "topRound", {
                        "entities" : qContainsPoint(qOwnedByBody(knob, EntityType.EDGE), vector(rt, 0 * millimeter, h)),
                        "radius" : definition.topRound });
        }
        if (definition.skirtOuterChamfer > 0 * millimeter)
        {
            opChamfer(context, id + "skirtOuterChamfer", {
                        "entities" : qContainsPoint(qOwnedByBody(knob, EntityType.EDGE), vector(rb, 0 * millimeter, 0 * millimeter)),
                        "chamferType" : ChamferType.EQUAL_OFFSETS,
                        "width" : definition.skirtOuterChamfer });
        }

        // Flutes: capsule-ended cutters along the tapered side, open at the bottom, stopping under the top round
        const fr = definition.fluteRadius;
        const zFluteTop = h - definition.topRound - fr;
        var cutters = [];
        for (var i = 0; i < definition.fluteCount; i += 1)
        {
            const a = i * 360 / definition.fluteCount * degree;
            // The cutter's axis runs parallel to the tapered side, fluteDepth inside it
            const radial = vector(cos(a), sin(a), 0);
            const zLow = -1 * millimeter;
            const flLow = radial * (rb - definition.taper * (zLow / h) + fr - definition.fluteDepth) + vector(0, 0, 1) * zLow;
            const flHigh = radial * (rb - definition.taper * (zFluteTop / h) + fr - definition.fluteDepth) +
                vector(0, 0, 1) * zFluteTop;
            fCylinder(context, id + ("flute" ~ i), { "bottomCenter" : flLow, "topCenter" : flHigh, "radius" : fr });
            sphereAt(context, id + ("fluteEnd" ~ i), flHigh, fr);
            cutters = append(cutters, qCreatedBy(id + ("flute" ~ i), EntityType.BODY));
            cutters = append(cutters, qCreatedBy(id + ("fluteEnd" ~ i), EntityType.BODY));
        }

        // Finger dish: a shallow spherical cap in the flat part of the top
        if (definition.dishDepth > 0 * millimeter)
        {
            const dishR = rt - definition.topRound - 0.5 * millimeter;
            const d = definition.dishDepth;
            const rs = (dishR * dishR + d * d) / (2 * d);
            sphereAt(context, id + "dish", vector(0, 0, 1) * (h - d + rs), rs);
            cutters = append(cutters, qCreatedBy(id + "dish", EntityType.BODY));
        }

        // Underside: the skirt's straight pocket for the nut and washer, then one cone up to the bore. No horizontal
        // ceilings, so it prints skirt-down without supports.
        fCylinder(context, id + "nutPocket", {
                    "bottomCenter" : vector(0, 0, -1) * millimeter,
                    "topCenter" : vector(0, 0, 1) * coneStart,
                    "radius" : rNut });
        cutters = append(cutters, qCreatedBy(id + "nutPocket", EntityType.BODY));
        const overlap = 0.05 * millimeter;
        fCone(context, id + "innerCone", {
                    "bottomCenter" : vector(0, 0, 1) * (coneStart - overlap),
                    "topCenter" : vector(0, 0, 1) * coneEnd,
                    "bottomRadius" : rNut + overlap * slope,
                    "topRadius" : rBore });
        cutters = append(cutters, qCreatedBy(id + "innerCone", EntityType.BODY));

        // Shaft bore: round and looser than the shaft; the ribs below do the gripping
        fCylinder(context, id + "bore", {
                    "bottomCenter" : vector(0, 0, -1) * millimeter,
                    "topCenter" : vector(0, 0, 1) * boreTop,
                    "radius" : rBore });
        cutters = append(cutters, qCreatedBy(id + "bore", EntityType.BODY));

        opBoolean(context, id + "cut", {
                    "tools" : qUnion(cutters),
                    "targets" : knob,
                    "operationType" : BooleanOperationType.SUBTRACTION });

        // The skirt's inner bottom edge, at the mouth of the nut pocket
        if (definition.skirtInnerChamfer > 0 * millimeter)
        {
            opChamfer(context, id + "skirtInnerChamfer", {
                        "entities" : qContainsPoint(qOwnedByBody(knob, EntityType.EDGE), vector(rNut, 0 * millimeter, 0 * millimeter)),
                        "chamferType" : ChamferType.EQUAL_OFFSETS,
                        "width" : definition.skirtInnerChamfer });
        }

        // Crush ribs from the bore wall towards the axis, from the top of the cone up to the bore's top. A cone cuts a
        // lead-in chamfer into their lower ends so the shaft spreads them gradually.
        if (definition.ribCount > 0)
        {
            const ribSketch = newSketchOnPlane(context, id + "ribSketch", {
                        "sketchPlane" : plane(vector(0, 0, 1) * ribStart, vector(0, 0, 1), vector(1, 0, 0)) });
            const halfWidth = definition.ribWidth / 2;
            for (var k = 0; k < definition.ribCount; k += 1)
            {
                const ra = k * 360 / definition.ribCount * degree;
                const along = vector(cos(ra), sin(ra));
                const across = vector(-sin(ra), cos(ra));
                const ribIn = along * rTip;
                const ribOut = along * (rBore + 0.2 * millimeter);
                skPolyline(ribSketch, "rib" ~ k, {
                            "points" : [ribIn + across * halfWidth, ribOut + across * halfWidth, ribOut - across * halfWidth,
                                ribIn - across * halfWidth, ribIn + across * halfWidth] });
            }
            skSolve(ribSketch);
            opExtrude(context, id + "ribs", {
                        "entities" : qSketchRegion(id + "ribSketch"),
                        "direction" : vector(0, 0, 1),
                        "endBound" : BoundingType.BLIND,
                        "endDepth" : boreTop - ribStart + 0.2 * millimeter });
            const ribs = qCreatedBy(id + "ribs", EntityType.BODY);
            if (definition.ribLeadIn > 0 * millimeter)
            {
                fCone(context, id + "leadIn", {
                            "bottomCenter" : vector(0, 0, 1) * (ribStart - 0.1 * millimeter),
                            "topCenter" : vector(0, 0, 1) * (ribStart + definition.ribLeadIn),
                            "bottomRadius" : rBore + 0.1 * millimeter,
                            "topRadius" : rTip });
                opBoolean(context, id + "chamferRibs", {
                            "tools" : qCreatedBy(id + "leadIn", EntityType.BODY),
                            "targets" : ribs,
                            "operationType" : BooleanOperationType.SUBTRACTION });
            }
            opBoolean(context, id + "addRibs", {
                        "tools" : qUnion([knob, ribs]),
                        "operationType" : BooleanOperationType.UNION });
        }

        opDeleteBodies(context, id + "cleanup", { "entities" : qCreatedBy(id + "ribSketch", EntityType.BODY) });
    }, {
        diameter : 20 * millimeter,
        taper : 0.8 * millimeter,
        topThickness : 2.5 * millimeter,
        topRound : 2 * millimeter,
        skirtOuterChamfer : 1 * millimeter,
        skirtInnerChamfer : 1 * millimeter,
        dishDepth : 0.6 * millimeter,
        fluteCount : 20,
        fluteRadius : 1.2 * millimeter,
        fluteDepth : 0.6 * millimeter,
        shaftLength : 16 * millimeter,
        gap : 1 * millimeter,
        nutDiameter : 12 * millimeter,
        nutHeight : 2 * millimeter,
        bushingDiameter : 7 * millimeter,
        bushingHeight : 5 * millimeter,
        overhangAngle : 45 * degree,
        clearance : 0.5 * millimeter,
        shaftDiameter : 6 * millimeter,
        boreGap : 0.6 * millimeter,
        ribCount : 8,
        ribWidth : 0.6 * millimeter,
        ribInterference : 0.1 * millimeter,
        ribLeadIn : 1.5 * millimeter
    });

// fSphere takes its centre as a vertex query, so make a point there first and delete it afterwards
function sphereAt(context is Context, id is Id, centre is Vector, radius is ValueWithUnits)
{
    opPoint(context, id + "centre", { "point" : centre });
    fSphere(context, id + "ball", { "center" : qCreatedBy(id + "centre", EntityType.VERTEX), "radius" : radius });
    opDeleteBodies(context, id + "deleteCentre", { "entities" : qCreatedBy(id + "centre", EntityType.BODY) });
}
