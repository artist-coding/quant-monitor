import { useEffect, useRef } from 'react';

interface Particle {
  x: number;
  y: number;
  r: number;
  speed: number;
  drift: number;
  alpha: number;
  hue: 'gold' | 'blue' | 'purple';
}

const HUES = {
  gold: '245, 185, 66',
  blue: '79, 142, 247',
  purple: '157, 107, 245',
} as const;

/** 全局面布背景：上升微粒 + 底部缓慢漂移的资金曲线剪影 */
export default function BackgroundFX() {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    let raf = 0;
    let w = 0;
    let h = 0;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const particles: Particle[] = [];

    // 底部资金曲线剪影
    const LINE_POINTS = 90;
    const line: number[] = [];
    let lineValue = 0.5;

    const resize = () => {
      w = window.innerWidth;
      h = window.innerHeight;
      canvas.width = w * dpr;
      canvas.height = h * dpr;
      canvas.style.width = `${w}px`;
      canvas.style.height = `${h}px`;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    };

    const spawn = (initial: boolean): Particle => ({
      x: Math.random() * w,
      y: initial ? Math.random() * h : h + 10,
      r: 0.6 + Math.random() * 1.8,
      speed: 0.12 + Math.random() * 0.35,
      drift: (Math.random() - 0.5) * 0.15,
      alpha: 0.12 + Math.random() * 0.3,
      hue: Math.random() < 0.6 ? 'gold' : Math.random() < 0.75 ? 'blue' : 'purple',
    });

    const init = () => {
      resize();
      particles.length = 0;
      const count = Math.min(80, Math.floor((w * h) / 26000));
      for (let i = 0; i < count; i++) particles.push(spawn(true));
      line.length = 0;
      for (let i = 0; i < LINE_POINTS; i++) {
        lineValue += (Math.random() - 0.48) * 0.06;
        lineValue = Math.max(0.15, Math.min(0.85, lineValue));
        line.push(lineValue);
      }
    };

    let tick = 0;
    const draw = () => {
      raf = requestAnimationFrame(draw);
      if (document.hidden) return;
      tick++;
      ctx.clearRect(0, 0, w, h);

      // 资金曲线剪影（每 40 帧推进一步）
      if (tick % 40 === 0) {
        lineValue = line[line.length - 1] + (Math.random() - 0.48) * 0.06;
        lineValue = Math.max(0.15, Math.min(0.85, lineValue));
        line.push(lineValue);
        line.shift();
      }
      const baseY = h * 0.92;
      const amp = h * 0.16;
      const stepX = w / (LINE_POINTS - 1);
      ctx.beginPath();
      line.forEach((v, i) => {
        const x = i * stepX;
        const y = baseY - v * amp;
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      });
      ctx.strokeStyle = `rgba(${HUES.gold}, 0.10)`;
      ctx.lineWidth = 1.5;
      ctx.stroke();
      // 曲线下方淡淡渐变
      const grad = ctx.createLinearGradient(0, baseY - amp, 0, h);
      grad.addColorStop(0, `rgba(${HUES.gold}, 0.05)`);
      grad.addColorStop(1, `rgba(${HUES.gold}, 0)`);
      ctx.lineTo(w, h);
      ctx.lineTo(0, h);
      ctx.closePath();
      ctx.fillStyle = grad;
      ctx.fill();

      // 微粒
      for (let i = 0; i < particles.length; i++) {
        const p = particles[i];
        p.y -= p.speed;
        p.x += p.drift;
        if (p.y < -10 || p.x < -10 || p.x > w + 10) {
          particles[i] = spawn(false);
          continue;
        }
        const twinkle = 0.75 + 0.25 * Math.sin((tick + i * 17) / 45);
        ctx.beginPath();
        ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
        ctx.fillStyle = `rgba(${HUES[p.hue]}, ${(p.alpha * twinkle).toFixed(3)})`;
        ctx.fill();
      }
    };

    init();
    draw();
    window.addEventListener('resize', init);
    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener('resize', init);
    };
  }, []);

  return (
    <>
      <canvas
        ref={canvasRef}
        aria-hidden
        className="pointer-events-none fixed inset-0 -z-10"
      />
      {/* 电影颗粒噪点 */}
      <div aria-hidden className="pointer-events-none fixed inset-0 -z-10 bg-noise opacity-[0.035]" />
    </>
  );
}
