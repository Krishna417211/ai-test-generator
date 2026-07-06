import confetti from "canvas-confetti";

const BRAND = ["#e6f3ec", "#cbe6d5", "#a1d1b1", "#8ac6a0", "#6fb188", "#c9e6d4", "#57956f"];

/** A satisfying two-sided burst for success moments. */
export function celebrate() {
  const end = Date.now() + 900;
  const frame = () => {
    confetti({
      particleCount: 4,
      angle: 60,
      spread: 65,
      origin: { x: 0, y: 0.7 },
      colors: BRAND,
      scalar: 0.9,
    });
    confetti({
      particleCount: 4,
      angle: 120,
      spread: 65,
      origin: { x: 1, y: 0.7 },
      colors: BRAND,
      scalar: 0.9,
    });
    if (Date.now() < end) requestAnimationFrame(frame);
  };
  frame();
  confetti({ particleCount: 90, spread: 100, origin: { y: 0.6 }, colors: BRAND, startVelocity: 42 });
}
