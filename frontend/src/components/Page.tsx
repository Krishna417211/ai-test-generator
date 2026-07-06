import { motion } from "framer-motion";
import type { ReactNode } from "react";

/** Standard animated page wrapper — soft fade + rise on route change. */
export default function Page({ children, className = "" }: { children: ReactNode; className?: string }) {
  return (
    <motion.main
      initial={{ opacity: 0, y: 14 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: -10 }}
      transition={{ duration: 0.45, ease: [0.22, 1, 0.36, 1] as [number, number, number, number] }}
      className={`relative z-10 mx-auto w-full max-w-6xl px-4 sm:px-6 ${className}`}
    >
      {children}
    </motion.main>
  );
}
