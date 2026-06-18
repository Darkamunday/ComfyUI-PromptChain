// Three.js bundle for PromptChain Pose Studio.
// Built to js/three.bundle.js as an IIFE exposing window.PromptChainThree,
// lazy-loaded only when a Pose Studio node mounts (Three is ~600KB — it must
// stay out of the main extension bundle). Mirrors codemirror.bundle.js.

import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { TransformControls } from "three/addons/controls/TransformControls.js";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { OBJLoader } from "three/addons/loaders/OBJLoader.js";

export { THREE, OrbitControls, TransformControls, GLTFLoader, OBJLoader };
