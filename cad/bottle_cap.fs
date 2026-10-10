FeatureScript 3095;
import(path : "onshape/std/common.fs", version : "3095.0");

annotation { "Feature Type Name" : "Bottle Cap", "Feature Type Description" : "Screw cap with an internal thread and chamfers" }
export const bottleCap = defineFeature(function(context is Context, id is Id, definition is map)
    precondition
    {
        annotation { "Name" : "Outer diameter" }
        isLength(definition.outerDiameter, { (millimeter) : [5, 31, 500] } as LengthBoundSpec);
        annotation { "Name" : "Height" }
        isLength(definition.height, { (millimeter) : [2, 16, 500] } as LengthBoundSpec);
        annotation { "Name" : "Top thickness" }
        isLength(definition.topThickness, { (millimeter) : [0.2, 1.5, 50] } as LengthBoundSpec);
        annotation { "Name" : "Thread major diameter" }
        isLength(definition.majorDiameter, { (millimeter) : [2, 27.6, 500] } as LengthBoundSpec);
        annotation { "Name" : "Thread minor diameter" }
        isLength(definition.minorDiameter, { (millimeter) : [1, 25.4, 500] } as LengthBoundSpec);
        annotation { "Name" : "Pitch" }
        isLength(definition.pitch, { (millimeter) : [0.5, 2.7, 50] } as LengthBoundSpec);
        annotation { "Name" : "Chamfer" }
        isLength(definition.chamfer, { (millimeter) : [0.1, 0.8, 10] } as LengthBoundSpec);
    }
    {
        const rOuter = definition.outerDiameter / 2;
        const rMajor = definition.majorDiameter / 2;
        const rMinor = definition.minorDiameter / 2;
        const height = definition.height;
        const pitch = definition.pitch;
        const cavity = height - definition.topThickness;

        if (rMinor >= rMajor)
            throw regenError("Minor diameter must be smaller than major diameter", ["minorDiameter"]);
        if (rMajor >= rOuter)
            throw regenError("Major diameter must be smaller than outer diameter", ["majorDiameter"]);
        if (cavity <= 0 * millimeter)
            throw regenError("Top thickness must be smaller than height", ["topThickness"]);

        // Body: a cylinder with the open end at z = 0 and the closed top at z = height
        fCylinder(context, id + "outer", {
                    "bottomCenter" : vector(0, 0, 0) * millimeter,
                    "topCenter" : vector(0, 0, 1) * height,
                    "radius" : rOuter });
        fCylinder(context, id + "bore", {
                    "bottomCenter" : vector(0, 0, -1) * millimeter,
                    "topCenter" : vector(0, 0, 1) * cavity,
                    "radius" : rMajor });
        opBoolean(context, id + "hollow", {
                    "tools" : qCreatedBy(id + "bore", EntityType.BODY),
                    "targets" : qCreatedBy(id + "outer", EntityType.BODY),
                    "operationType" : BooleanOperationType.SUBTRACTION });

        // Thread ridge: a trapezoid in the XZ plane, sunk 0.2 mm into the wall so the union is solid
        const baseWidth = pitch * 0.6;
        const crestWidth = pitch * 0.2;
        const zStart = definition.chamfer + 0.3 * millimeter;
        const threadLength = cavity - zStart - baseWidth - 0.5 * millimeter;
        if (threadLength <= 0 * millimeter)
            throw regenError("Cap is too short for the thread", ["height"]);
        const turns = threadLength / pitch;

        const sketch = newSketchOnPlane(context, id + "profile", {
                    "sketchPlane" : plane(vector(0, 0, 0) * millimeter, vector(0, -1, 0), vector(1, 0, 0)) });
        const zMid = zStart + baseWidth / 2;
        skPolyline(sketch, "ridge", {
                    "points" : [
                        vector(rMajor + 0.2 * millimeter, zMid - baseWidth / 2),
                        vector(rMajor, zMid - baseWidth / 2),
                        vector(rMinor, zMid - crestWidth / 2),
                        vector(rMinor, zMid + crestWidth / 2),
                        vector(rMajor, zMid + baseWidth / 2),
                        vector(rMajor + 0.2 * millimeter, zMid + baseWidth / 2),
                        vector(rMajor + 0.2 * millimeter, zMid - baseWidth / 2)
                    ] });
        skSolve(sketch);

        opHelix(context, id + "helix", {
                    "direction" : vector(0, 0, 1),
                    "axisStart" : vector(0, 0, 0) * millimeter,
                    "startPoint" : vector(rMajor, 0 * millimeter, zMid),
                    "interval" : [0, turns],
                    "clockwise" : false,
                    "helicalPitch" : pitch,
                    "spiralPitch" : 0 * millimeter });

        opSweep(context, id + "thread", {
                    "profiles" : qSketchRegion(id + "profile"),
                    "path" : qCreatedBy(id + "helix", EntityType.EDGE) });

        opBoolean(context, id + "join", {
                    "tools" : qUnion([qCreatedBy(id + "outer", EntityType.BODY), qCreatedBy(id + "thread", EntityType.BODY)]),
                    "operationType" : BooleanOperationType.UNION });

        opDeleteBodies(context, id + "cleanup", {
                    "entities" : qUnion([qCreatedBy(id + "profile", EntityType.BODY), qCreatedBy(id + "helix", EntityType.BODY)]) });

        // Chamfers: outer top and bottom rims, and a lead-in on the open end of the bore
        const capEdges = qOwnedByBody(qCreatedBy(id + "outer", EntityType.BODY), EntityType.EDGE);
        const edges = qUnion([
                    qContainsPoint(capEdges, vector(rOuter, 0 * millimeter, 0 * millimeter)),
                    qContainsPoint(capEdges, vector(rOuter, 0 * millimeter, height)),
                    qContainsPoint(capEdges, vector(rMajor, 0 * millimeter, 0 * millimeter))
                ]);
        opChamfer(context, id + "chamfer", {
                    "entities" : edges,
                    "chamferType" : ChamferType.EQUAL_OFFSETS,
                    "width" : definition.chamfer });
    }, {
        outerDiameter : 31 * millimeter,
        height : 16 * millimeter,
        topThickness : 1.5 * millimeter,
        majorDiameter : 27.6 * millimeter,
        minorDiameter : 25.4 * millimeter,
        pitch : 2.7 * millimeter,
        chamfer : 0.8 * millimeter
    });
