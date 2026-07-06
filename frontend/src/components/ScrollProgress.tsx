import { motion, useScroll, useSpring } from "framer-motion";

/** A thin sage progress bar pinned to the top edge, tracking page scroll. */
export default function ScrollProgress() {
  const { scrollYProgress } = useScroll();
  const scaleX = useSpring(scrollYProgress, { stiffness: 120, damping: 30, mass: 0.3 });

  return (
    <motion.div
      style={{ scaleX }}
      className="fixed top-0 left-0 right-0 h-[3px] origin-left z-[60] bg-brand-gradient shadow-glow"
    />
  );
}
