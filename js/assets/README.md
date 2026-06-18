# Pose Studio figure asset

Drop a rigged humanoid mesh here named **`pose-figure.glb`**:

    js/assets/pose-figure.glb

On load, Pose Studio uses it as the posable figure (auto-scaled to ~1.8 units
tall and grounded). If the file is absent or has no skinned/rigged mesh, it
falls back to the built-in capsule mannequin.

Requirements:
- **GLB** (binary glTF), **uncompressed** (no Draco/meshopt — the loader has no
  decompressor wired up yet).
- A **skinned mesh** with a **humanoid skeleton**. Bone names are matched
  heuristically (Mixamo / Unity-humanoid / Blender-Rigify / MakeHuman styles);
  the discovered bone list and the joint mapping are logged to the browser
  console as `[PoseStudio] GLB bones:` / `mapped joints:` so an unusual rig can
  be diagnosed.

Current default: "Human Male/Female Basemesh Rigged" (CC-BY — keep attribution
in the repo). MakeHuman CC0 exports are the planned long-term default.
