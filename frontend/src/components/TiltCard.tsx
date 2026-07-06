import { useRef, type ReactNode } from "react";
import { motion, useMotionValue, useSpring, useTransform } from "framer-motion";

/**
 * A 3D tilt-on-hover wrapper. Tracks the pointer over the element and rotates it
 * in perspective, with a soft light glare that follows the cursor. Springs give
 * it a weighty, premium feel; it settles flat on mouse-leave.
 */
export default function TiltCard({
  children,
  className,
  max = 9,
}: {
  children: ReactNode;
  className?: string;
  max?: number;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const px = useMotionValue(0.5);
  const py = useMotionValue(0.5);

  const spring = { stiffness: 180, damping: 16, mass: 0.4 };
  const rotateX = useSpring(useTransform(py, [0, 1], [max, -max]), spring);
  const rotateY = useSpring(useTransform(px, [0, 1], [-max, max]), spring);
  const glareX = useTransform(px, [0, 1], ["0%", "100%"]);
  const glareY = useTransform(py, [0, 1], ["0%", "100%"]);

  function handleMove(e: React.MouseEvent) {
    const rect = ref.current?.getBoundingClientRect();
    if (!rect) return;
    px.set((e.clientX - rect.left) / rect.width);
    py.set((e.clientY - rect.top) / rect.height);
  }
  function handleLeave() {
    px.set(0.5);
    py.set(0.5);
  }

  return (
    <motion.div
      ref={ref}
      onMouseMove={handleMove}
      onMouseLeave={handleLeave}
      style={{ rotateX, rotateY, transformStyle: "preserve-3d", transformPerspective: 1000 }}
      className={`relative ${className ?? ""}`}
    >
      {children}
      <motion.div
        aria-hidden
        className="pointer-events-none absolute inset-0 rounded-[inherit]"
        style={{
          background: useTransform(
            [glareX, glareY] as unknown as [typeof glareX, typeof glareY],
            ([x, y]: string[]) =>
              `radial-gradient(240px circle at ${x} ${y}, rgba(203,230,213,0.14), transparent 55%)`
          ),
        }}
      />
    </motion.div>
  );
}
