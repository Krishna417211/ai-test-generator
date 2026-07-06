import { Suspense, useRef } from "react";
import { Canvas, useFrame } from "@react-three/fiber";
import { Float, MeshDistortMaterial, Sparkles } from "@react-three/drei";
import type { Group, Mesh } from "three";

/**
 * The hero centrepiece: a distorted, glowing sage "core" wrapped in a slowly
 * counter-rotating wireframe shell, surrounded by drifting particles. The whole
 * group eases toward the pointer for a parallax feel. Pure lights (no external
 * HDR) so it works fully offline.
 */
function Core() {
  const group = useRef<Group>(null);
  const shell = useRef<Mesh>(null);

  useFrame((state, delta) => {
    if (group.current) {
      const targetX = state.pointer.y * 0.35;
      const targetY = state.pointer.x * 0.55;
      group.current.rotation.x += (targetX - group.current.rotation.x) * 0.045;
      group.current.rotation.y += (targetY - group.current.rotation.y) * 0.045;
    }
    if (shell.current) {
      shell.current.rotation.y += delta * 0.22;
      shell.current.rotation.x -= delta * 0.1;
    }
  });

  return (
    <group ref={group} position={[0, -1.55, 0]}>
      <Float speed={1.5} rotationIntensity={0.5} floatIntensity={1.15}>
        {/* Solid, wobbling core */}
        <mesh>
          <icosahedronGeometry args={[1.12, 14]} />
          <MeshDistortMaterial
            color="#8ac6a0"
            emissive="#1f5540"
            emissiveIntensity={0.16}
            roughness={0.3}
            metalness={0.24}
            distort={0.42}
            speed={1.7}
          />
        </mesh>
        {/* Counter-rotating wireframe shell */}
        <mesh ref={shell} scale={1.5}>
          <icosahedronGeometry args={[1.12, 2]} />
          <meshBasicMaterial color="#cbe6d5" wireframe transparent opacity={0.14} />
        </mesh>
      </Float>
      <Sparkles count={60} scale={[9, 8, 9]} size={2.2} speed={0.32} color="#c9e6d4" opacity={0.6} />
    </group>
  );
}

/** CSS fallback for reduced-motion users or browsers without WebGL. */
function Fallback() {
  return (
    <div className="w-full h-full flex items-center justify-center">
      <div
        className="w-56 h-56 rounded-full blur-2xl opacity-70 animate-float"
        style={{ background: "radial-gradient(circle at 35% 30%, #cbe6d5, #8ac6a0 55%, #57956f)" }}
      />
    </div>
  );
}

export default function Hero3D() {
  const reduce =
    typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const hasWebGL = typeof window !== "undefined" && !!window.WebGLRenderingContext;

  if (reduce || !hasWebGL) return <Fallback />;

  return (
    <Canvas
      camera={{ position: [0, 0, 4.6], fov: 45 }}
      dpr={[1, 2]}
      gl={{ antialias: true, alpha: true, powerPreference: "high-performance" }}
    >
      <ambientLight intensity={0.85} />
      <directionalLight position={[4, 5, 5]} intensity={2.4} color="#ffffff" />
      <directionalLight position={[-5, -2, -3]} intensity={1.2} color="#8ac6a0" />
      <directionalLight position={[0, -4, 4]} intensity={0.7} color="#cbe6d5" />
      <Suspense fallback={null}>
        <Core />
      </Suspense>
    </Canvas>
  );
}
